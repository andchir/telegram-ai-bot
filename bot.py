"""Telegram-бот, который отвечает на обращения по имени/цитаты и рисует картинки.

API: PlusVibe (https://plusvibeapi.ru) — чат gpt-6.1-sol, генерация gpt-image-2:white.
Ключи и настройки — в .env (см. .env.example).
"""

import asyncio
import base64
import io
import logging
import os
import random
import re
import time
from collections import defaultdict, deque

import httpx
from dotenv import load_dotenv
from telegram import ReplyParameters, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PLUSVIBE_API_KEY = os.getenv("PLUSVIBE_API_KEY", "").strip()
PLUSVIBE_BASE = os.getenv("PLUSVIBE_BASE", "https://plusvibeapi.ru").rstrip("/")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-6.1-sol")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2:white")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — дружелюбный ИИ-ассистент в Telegram-чате. Отвечай по делу, живо и с юмором.",
).strip()
BOT_NAME = os.getenv("BOT_NAME", "").strip()  # доп. имя для обращений (необязательно)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))

# Режим запуска: polling (по умолчанию, для локального запуска) или webhook
BOT_MODE = os.getenv("BOT_MODE", "polling").strip().lower()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook").strip("/")
WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "0.0.0.0").strip()
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip() or None

# Антиспам генерации картинок: минимальный интервал между генерациями, секунды
IMAGE_COOLDOWN = int(os.getenv("IMAGE_COOLDOWN_SECONDS", "600"))
DEFAULT_COOLDOWN_PHRASES = (
    "Кисти отдыхают — я не волшебник, я просто бот.",
    "Холст ещё сохнет, дай мне передышку.",
    "Муза ушла за кофе, скоро вернётся.",
    "Слишком много искусства за раз — даже у меня есть лимиты.",
    "Рисую не чаще, чем завариваю чай, а это дело неспешное.",
)
IMAGE_COOLDOWN_PHRASES = [
    p.strip()
    for p in os.getenv("IMAGE_COOLDOWN_PHRASES", "|".join(DEFAULT_COOLDOWN_PHRASES)).split("|")
    if p.strip()
] or list(DEFAULT_COOLDOWN_PHRASES)
_last_image_at = 0.0  # момент последней успешной генерации (time.monotonic)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("telegram-ai-bot")

# Короткая память диалога на чат: chat_id -> очередь сообщений {role, content}
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

# Просьбы нарисовать (рус/англ) — регулярка для распознавания запроса на картинку
IMAGE_TRIGGER_RE = re.compile(
    r"(?i)"
    r"\b(нарисуй(те)?|нарисовать|рисуй|покажи\s+картинку|сгенерируй\s+(картинку|изображение|арт)|"
    r"сделай\s+(картинку|изображение|арт)|создай\s+(картинку|изображение|арт)|"
    r"draw|paint|generate\s+(an?\s+)?(image|picture|art))\b"
)


def mentions_bot(text: str, username: str, first_name: str) -> bool:
    """Обращение по имени: @username, username или имя бота как слово."""
    if re.search(rf"(?i)@{re.escape(username)}\b", text):
        return True
    for name in {first_name, BOT_NAME} - {"", username}:
        if name and re.search(rf"(?i)\b{re.escape(name)}\b", text):
            return True
    return False


def is_reply_to_bot(message, bot_id: int) -> bool:
    reply = message.reply_to_message
    return reply is not None and reply.from_user is not None and reply.from_user.id == bot_id


def build_prompt(text: str, message) -> str:
    """Подставляем процитированное сообщение как контекст, если оно не от бота."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.text and not reply.from_user.is_bot:
        author = reply.from_user.full_name
        return f"[Контекст: цитируется сообщение от {author}: «{reply.text}»]\n\n{text}"
    return text


def extract_image(data) -> tuple[str, str] | None:
    """Ищем в ответе медиа-API ('url' или 'b64_json'/'base64') рекурсивно."""
    if isinstance(data, dict):
        for key in ("url", "image_url", "b64_json", "base64"):
            value = data.get(key)
            if isinstance(value, str) and value:
                kind = "url" if key in ("url", "image_url") else "b64"
                return kind, value
        for value in data.values():
            found = extract_image(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = extract_image(item)
            if found:
                return found
    return None


def format_remaining(seconds: float) -> str:
    total = int(seconds) + 1
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes} мин {secs:02d} сек"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} сек"


async def ask_llm(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{PLUSVIBE_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {PLUSVIBE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": CHAT_MODEL, "messages": messages},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


async def generate_image(prompt: str) -> tuple[str, str] | None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        resp = await client.post(
            f"{PLUSVIBE_BASE}/api/media/generate",
            headers={
                "Authorization": f"Bearer {PLUSVIBE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "opts": {"aspect_ratio": "1:1", "resolution": "1K", "mode": "text-to-image"},
            },
        )
        resp.raise_for_status()
        return extract_image(resp.json())


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_image_at
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not message.text or not chat or not user:
        return

    me = await context.bot.get_me()
    triggered = is_reply_to_bot(message, me.id) or mentions_bot(message.text, me.username, me.first_name)
    if not triggered:
        return

    text = message.text
    prompt = build_prompt(text, message)

    try:
        if IMAGE_TRIGGER_RE.search(text):
            now = time.monotonic()
            if now - _last_image_at < IMAGE_COOLDOWN:
                remaining = IMAGE_COOLDOWN - (now - _last_image_at)
                phrase = random.choice(IMAGE_COOLDOWN_PHRASES)
                await message.reply_text(
                    f"{phrase} Следующую картинку нарисую через {format_remaining(remaining)}.",
                    reply_parameters=ReplyParameters(message_id=message.message_id),
                )
                return
            await message.chat.send_action(ChatAction.UPLOAD_PHOTO)
            image = await generate_image(prompt)
            if image is None:
                await message.reply_text("Не смог разобрать ответ генератора картинок 😔")
                return
            kind, value = image
            if kind == "url":
                async with httpx.AsyncClient(timeout=120.0) as client:
                    img_bytes = (await client.get(value)).content
            else:
                img_bytes = base64.b64decode(value)
            caption = f"🎨 Запрос от {user.full_name}: {text}"
            await message.reply_photo(
                photo=io.BytesIO(img_bytes),
                caption=caption[:1024],
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
            _last_image_at = time.monotonic()  # фиксируем только успешную генерацию
            _history[chat.id].append({"role": "user", "content": prompt})
            _history[chat.id].append({"role": "assistant", "content": f"[нарисовал картинку по запросу: {text}]"})
            return

        await message.chat.send_action(ChatAction.TYPING)
        history = list(_history[chat.id])
        answer = await ask_llm([{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": prompt}])
        if not answer:
            answer = "…"
        await message.reply_text(answer, reply_parameters=ReplyParameters(message_id=message.message_id))
        _history[chat.id].append({"role": "user", "content": prompt})
        _history[chat.id].append({"role": "assistant", "content": answer})
    except httpx.HTTPStatusError as e:
        log.error("HTTP %s от API: %s", e.response.status_code, e.response.text[:500])
        await message.reply_text("Упс, API вернул ошибку. Попробуй ещё раз чуть позже.")
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.error("Ошибка: %s", e)
        await message.reply_text("Что-то пошло не так 🤖")


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        me = await context.bot.get_me()
        await update.effective_message.reply_text(
            f"Привет! Я {me.first_name}. Обратись ко мне по имени (@{me.username}) "
            "или процитируй моё сообщение — отвечу. Попроси «нарисуй …» — сгенерирую картинку."
        )


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в .env")
    if not PLUSVIBE_API_KEY:
        raise SystemExit("Не задан PLUSVIBE_API_KEY в .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if BOT_MODE == "webhook":
        if not WEBHOOK_URL:
            raise SystemExit("В режиме webhook нужно задать WEBHOOK_URL (публичный HTTPS-адрес бота)")
        log.info("Бот запущен в режиме webhook: %s/%s", WEBHOOK_URL, WEBHOOK_PATH)
        app.run_webhook(
            listen=WEBHOOK_LISTEN,
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET_TOKEN,
        )
    else:
        log.info("Бот запущен в режиме polling")
        app.run_polling()


if __name__ == "__main__":
    main()
