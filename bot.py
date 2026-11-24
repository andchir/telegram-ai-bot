"""Telegram-бот, который отвечает на обращения по имени/цитаты и рисует картинки.

API: PlusVibe (https://plusvibeapi.ru) — чат gpt-6.1-sol, генерация gpt-image-2:white.
Ключи и настройки — в .env (см. .env.example).
"""

import asyncio
import base64
import io
import logging
import logging.handlers
import os
import random
import re
import time
from collections import defaultdict, deque

import httpx
from dotenv import load_dotenv
from telegram import InputMediaPhoto, ReplyParameters, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PLUSVIBE_API_KEY = os.getenv("PLUSVIBE_API_KEY", "").strip()
PLUSVIBE_BASE = os.getenv("PLUSVIBE_BASE", "https://plusvibeapi.ru").rstrip("/")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-6-luna")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2:white")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — дружелюбный ИИ-ассистент в Telegram-чате. Отвечай по делу, живо и с юмором.",
).strip()
BOT_NAME = os.getenv("BOT_NAME", "").strip()  # доп. имя для обращений (необязательно)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
# Лимит токенов на ответ основной модели; 0 = не ограничивать (параметр не отправляется)
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "0"))

# Логирование: файл с ротацией по размеру; пустой LOG_FILE = только консоль
LOG_FILE = os.getenv("LOG_FILE", "bot.log").strip()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", "3"))

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
_last_image_at: float | None = None  # момент последней успешной генерации (time.monotonic)

# Мгновенные заглушки, пока бот выполняет долгую операцию (генерация картинки)
DEFAULT_ACK_PHRASES = (
    "Уже берусь за дело!",
    "Принято, работаю над этим…",
    "Секундочку, творю!",
    "Хорошо, щас всё будет.",
    "Задача принята, начинаю.",
)
ACK_PHRASES = [
    p.strip()
    for p in os.getenv("ACK_PHRASES", "|".join(DEFAULT_ACK_PHRASES)).split("|")
    if p.strip()
] or list(DEFAULT_ACK_PHRASES)

# Fact-checking через PlusVibe (модель с доступом к поиску)
FACTCHECK_MODEL = os.getenv("FACTCHECK_MODEL", "perplexity/sonar")
FACTCHECK_SYSTEM_PROMPT = os.getenv(
    "FACTCHECK_SYSTEM_PROMPT",
    "Ты - профессиональный факт-чекер. Твоя задача - проверять новостные утверждения "
    "на достоверность, опираясь на надежные источники. Отвечай структурированно: указывай "
    "статус проверки (Правда / Ложь / Вводяще в заблуждение / Не подтверждено), приводи "
    "краткое объяснение и ссылки на источники.",
).strip()

def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if LOG_FILE:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
            )
        except OSError as e:
            root.warning("Не удалось открыть файл лога %s: %s", LOG_FILE, e)
            return
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # иначе каждый HTTP-запрос (httpx) — отдельная строка в логе
    logging.getLogger("httpx").setLevel(logging.WARNING)


setup_logging()
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

# Просьбы проверить факт (рус/англ)
FACTCHECK_TRIGGER_RE = re.compile(
    r"(?i)"
    r"\b(правда\s+ли|действительно\s+ли|это\s+правда|"
    r"факт[\s-]?чек|проверь\s+(факт|информацию|утверждение|новость|статью|на\s+достоверность)|"
    r"fact[\s-]?check|is\s+it\s+true)\b"
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
    payload = {"model": CHAT_MODEL, "messages": messages}
    if MAX_TOKENS > 0:
        payload["max_tokens"] = MAX_TOKENS
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{PLUSVIBE_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {PLUSVIBE_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


async def fact_check(text: str) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{PLUSVIBE_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {PLUSVIBE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": FACTCHECK_MODEL,
                "messages": [
                    {"role": "user", "content": FACTCHECK_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
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
        try:
            data = resp.json()
        except ValueError:
            log.error("Медиа-API вернул не-JSON ответ (HTTP %s): %.1000s", resp.status_code, resp.text)
            return None
        image = extract_image(data)
        if image is None:
            log.error("Не распознан формат ответа медиа-API: %.1000s", resp.text)
        return image


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_image_at
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not message.text or not chat or not user:
        return

    me = await context.bot.get_me()
    # В личке отвечаем на любое сообщение; в группах — только по имени или цитате
    triggered = (
        chat.type == ChatType.PRIVATE
        or is_reply_to_bot(message, me.id)
        or mentions_bot(message.text, me.username, me.first_name)
    )
    if not triggered:
        return

    text = message.text
    prompt = build_prompt(text, message)
    ack = None  # заглушка, отправленная сразу; по завершении редактируется в результат

    try:
        if IMAGE_TRIGGER_RE.search(text):
            now = time.monotonic()
            if _last_image_at is not None and now - _last_image_at < IMAGE_COOLDOWN:
                remaining = IMAGE_COOLDOWN - (now - _last_image_at)
                phrase = random.choice(IMAGE_COOLDOWN_PHRASES)
                await message.reply_text(
                    f"{phrase} Следующую картинку нарисую через {format_remaining(remaining)}.",
                    reply_parameters=ReplyParameters(message_id=message.message_id),
                )
                return
            ack = await message.reply_text(
                random.choice(ACK_PHRASES),
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
            await message.chat.send_action(ChatAction.UPLOAD_PHOTO)
            image = await generate_image(prompt)
            if image is None:
                await ack.edit_text("Не смог разобрать ответ генератора картинок 😔")
                return
            kind, value = image
            if kind == "url":
                async with httpx.AsyncClient(timeout=120.0) as client:
                    img_bytes = (await client.get(value)).content
            else:
                img_bytes = base64.b64decode(value)
            caption = f"🎨 Запрос от {user.full_name}: {text}"
            try:
                # заглушка превращается в картинку — цитата запроса сохраняется
                await ack.edit_media(media=InputMediaPhoto(io.BytesIO(img_bytes), caption=caption[:1024]))
            except TelegramError:
                await ack.delete()
                await message.reply_photo(
                    photo=io.BytesIO(img_bytes),
                    caption=caption[:1024],
                    reply_parameters=ReplyParameters(message_id=message.message_id),
                )
            _last_image_at = time.monotonic()  # фиксируем только успешную генерацию
            _history[chat.id].append({"role": "user", "content": prompt})
            _history[chat.id].append({"role": "assistant", "content": f"[нарисовал картинку по запросу: {text}]"})
            return

        if FACTCHECK_TRIGGER_RE.search(text):
            ack = await message.reply_text(
                random.choice(ACK_PHRASES),
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
            await message.chat.send_action(ChatAction.TYPING)
            verdict = await fact_check(prompt)
            await ack.edit_text(verdict[:4000])  # лимит Telegram — 4096 символов
            _history[chat.id].append({"role": "user", "content": prompt})
            _history[chat.id].append({"role": "assistant", "content": verdict[:4000]})
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
        await _reply_error(message, ack, "Упс, API вернул ошибку. Попробуй ещё раз чуть позже.")
    except Exception as e:  # заглушка не должна остаться без ответа
        log.error("Ошибка: %s", e)
        await _reply_error(message, ack, "Что-то пошло не так 🤖")


async def _reply_error(message, ack, text: str) -> None:
    """Ошибку пишем в заглушку, если она есть, чтобы не плодить сообщения."""
    if ack is not None:
        await ack.edit_text(text)
    else:
        await message.reply_text(text)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        me = await context.bot.get_me()
        minutes = max(1, IMAGE_COOLDOWN // 60)
        await update.effective_message.reply_text(
            f"Привет! Я {me.first_name} — ИИ-ассистент в этом чате.\n\n"
            "Что я умею:\n"
            "• В личке отвечаю на любое сообщение;\n"
            f"• В группах — когда обратятся по имени (@{me.username} или просто «{me.first_name}») "
            "или процитируют моё сообщение;\n"
            "• Помнить контекст нашего разговора в этом чате;\n"
            f"• Рисовать картинки по запросу «нарисуй …» — но не чаще раза в {minutes} мин;\n"
            "• Проверять утверждения на достоверность: спроси «правда ли, что …» — найду источники.\n\n"
            f"Попробуй: «@{me.username}, привет!» или «@{me.username}, нарисуй котика»"
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
