"""Telegram-бот, который отвечает на обращения по имени/цитаты и рисует картинки.

Основной API: PlusVibe (https://plusvibeapi.ru) — чат, генерация картинок, фактчек и поиск.
Вторая чат-модель по ключевому слову в сообщении: GigaChat (api.giga.chat, OAuth Сбера).
Ключи и настройки — в .env (см. .env.example).
"""

import asyncio
import base64
import html
import io
import logging
import logging.handlers
import os
import random
import re
import ssl
import time
import uuid
from collections import defaultdict, deque

import httpx
from dotenv import load_dotenv
from telegram import InputMediaPhoto, ReplyParameters, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PLUSVIBE_API_KEY = os.getenv("PLUSVIBE_API_KEY", "").strip()
PLUSVIBE_BASE = os.getenv("PLUSVIBE_BASE", "https://plusvibeapi.ru").rstrip("/")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-6-luna")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2:network")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — дружелюбный ИИ-ассистент в Telegram-чате. Отвечай по делу, живо и с юмором.",
).strip()
BOT_NAME = os.getenv("BOT_NAME", "").strip()  # доп. имя для обращений (необязательно)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
# Лимит токенов на ответ основной модели; 0 = не ограничивать (параметр не отправляется)
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "0"))

# Вторая чат-модель на отдельном API (GigaChat, Сбер); пустая CHAT_MODEL2 = отключено.
# Вызывается ключевым словом из MODEL2_KEYWORDS в сообщении пользователя.
CHAT_MODEL2 = os.getenv("CHAT_MODEL2", "").strip()
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
GIGACHAT_OAUTH_URL = os.getenv(
    "GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
).strip()
GIGACHAT_BASE = os.getenv("GIGACHAT_BASE", "https://api.giga.chat").rstrip("/")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
# false — отключить проверку TLS (если сертификат Минцифры не установлен в систему)
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
MODEL2_KEYWORDS = [
    w.strip()
    for w in os.getenv("MODEL2_KEYWORDS", "гигачат,гига,gigachat,giga").split(",")
    if w.strip()
]

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
# Асинхронная генерация: интервал опроса джобы и общий таймаут ожидания, секунды
IMAGE_POLL_INTERVAL = float(os.getenv("IMAGE_POLL_INTERVAL", "5"))
IMAGE_JOB_TIMEOUT = int(os.getenv("IMAGE_JOB_TIMEOUT", "300"))
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
# Момент последней успешной генерации (time.monotonic) по скоупу:
# личка - ("user", user_id), группа - ("chat", chat_id)
_last_image_at: dict[tuple[str, int], float] = {}

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
    "на достоверность, опираясь на надежные источники. Отвечай структурированно:\n"
    "1) Статус проверки (Правда / Ложь / Вводяще в заблуждение / Не подтверждено) и краткое объяснение.\n"
    "2) В объяснении ссылайся на источники номерами в квадратных скобках: [1], [2] и т.д. "
    "Нумерация - строго по порядку первого упоминания, без пропусков.\n"
    "3) В конце обязательный блок «Источники:» с НУМЕРОВАННЫМ списком: "
    "«1. [название](https://реальный-url)». Номер в списке должен точно совпадать с номером "
    "в тексте: каждому [N] в тексте - ровно одна ссылка N в списке. "
    "Ссылки давай только полными URL в формате Markdown.",
).strip()

# Поиск информации через ту же модель (FACTCHECK_MODEL), но без статуса достоверности
SEARCH_SYSTEM_PROMPT = os.getenv(
    "SEARCH_SYSTEM_PROMPT",
    "Ты - поисковый ассистент. Твоя задача - найти актуальную информацию по запросу "
    "пользователя и изложить её структурированно, опираясь на надежные источники. "
    "Отвечай по делу и без оценки достоверности - просто информация по запросу.\n"
    "В тексте ссылайся на источники номерами в квадратных скобках: [1], [2] и т.д. "
    "Нумерация - строго по порядку первого упоминания, без пропусков.\n"
    "В конце обязательный блок «Источники:» с НУМЕРОВАННЫМ списком: "
    "«1. [название](https://реальный-url)». Номер в списке должен точно совпадать с номером "
    "в тексте: каждому [N] в тексте - ровно одна ссылка N в списке. "
    "Ссылки давай только полными URL в формате Markdown.",
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

# Просьбы найти информацию (рус/англ) — поиск без оценки достоверности
SEARCH_TRIGGER_RE = re.compile(
    r"(?i)"
    r"\b(найди(те)?\s+(информацию|инфу|данные|сведения|про)|"
    r"поищи(те)?(\s+(информацию|инфу|про))?|поиск\s+(информации|по\s+запросу)|"
    r"погугли(те)?|загугли(те)?|"
    r"search(\s+for)?|find\s+(info|information)|google)\b"
)

# Ключевые слова правки изображения (работают с процитированной картинкой)
IMAGE_EDIT_RE = re.compile(
    r"(?i)"
    r"\b(добавь(те)?|замени(те)?|поменяй(те)?|убери(те)?|удали(те)?|исправь(те)?|"
    r"измени(те)?|переделай(те)?|перекрась(те)?|дорисуй(те)?|поправь(те)?|"
    r"сделай\s+(его|е[ёе]|ярче|темнее|ч[её]рно|цветн)|в\s+стиле|"
    r"edit|replace|remove|change|redraw|make\s+it)\b"
)

# Ключевые слова выбора второй чат-модели (MODEL2_KEYWORDS из .env)
MODEL2_RE = (
    re.compile(r"(?i)\b(" + "|".join(re.escape(w) for w in MODEL2_KEYWORDS) + r")\b")
    if MODEL2_KEYWORDS
    else None
)


def strip_model2_keyword(text: str) -> str:
    """Вырезаем слово-триггер второй модели вместе с осиротевшими запятыми."""
    text = MODEL2_RE.sub("", text)
    text = re.sub(r",(\s*,)+", ",", text)
    text = re.sub(r"^[\s,.!?;:]+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


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


def strip_bot_mention(text: str, username: str, first_name: str) -> str:
    """Убираем обращение к боту (@username / имя) из текста запроса к генератору."""
    text = re.sub(rf"(?i)@{re.escape(username)}\b[,.!?]?", "", text)
    for name in {first_name, BOT_NAME} - {"", username}:
        if name:
            text = re.sub(rf"(?i)\b{re.escape(name)}\b[,.!?]?", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def extract_image(data) -> tuple[str, str] | None:
    """Ищем в ответе медиа-API ('url'/'resultUrls' или 'b64_json'/'base64') рекурсивно."""
    if isinstance(data, dict):
        for key in ("url", "image_url", "b64_json", "base64"):
            value = data.get(key)
            if isinstance(value, str) and value:
                kind = "url" if key in ("url", "image_url") else "b64"
                return kind, value
        for key in ("resultUrls", "urls", "result_url"):
            value = data.get(key)
            if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
                return "url", value[0]
            if isinstance(value, str) and value:
                return "url", value
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


def md_to_tg_html(text: str) -> str:
    """Минимальная конвертация Markdown из ответа модели в HTML Telegram."""
    # модель часто склеивает ссылки: "слово[a](u1)[b](u2)" -> "слово [a](u1) [b](u2)"
    text = re.sub(r"\]\((https?://[^)\s]+)\)(?=\[)", r"](\1) ", text)
    text = re.sub(r"(\S)\[([^\]]+)\]\((https?://)", r"\1 [\2](\3", text)

    out = html.escape(text, quote=False)
    # спрятать блоки кода: их разметку (решётки комментариев и т.п.) не трогаем
    blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\x00{len(blocks) - 1}\x00"

    out = re.sub(r"```\w*\n(.*?)\n?```", _stash, out, flags=re.S)
    # URL допускаем сбалансированные скобки: ru.wikipedia.org/wiki/Что-то_(фильм)
    out = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s()]+(?:\([^)\s]*\)[^\s()]*)*)\)",
        r'<a href="\2">\1</a>',
        out,
    )
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"__(.+?)__", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<![\w<])\*([^*\n]+)\*(?![\w>])", r"<i>\1</i>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*#*\s*$", r"<b>\1</b>", out)
    out = re.sub(r"(?im)^\s*(источники:?)\s*$", r"<b>\1</b>", out)
    out = re.sub(r"\x00(\d+)\x00", lambda m: "<pre>" + blocks[int(m.group(1))] + "</pre>", out)
    return out


def safe_truncate_html(text: str, limit: int = 3900) -> str:
    """Обрезка HTML лимиту Telegram без разрыва тега."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 2]
    last_lt, last_gt = cut.rfind("<"), cut.rfind(">")
    if last_lt > last_gt:  # обрезали посередине тега — отбрасываем его начало
        cut = cut[:last_lt]
    return cut.rstrip() + " …"


def md_to_plain(text: str) -> str:
    """Запасной вариант для обычного текста: убираем markdown-разметку."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    return text.replace("**", "").replace("__", "")


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


# Кэш OAuth-токена GigaChat: живёт ~30 минут, обновляем по expires_at с минутным запасом
_gigachat_token: str | None = None
_gigachat_token_deadline = 0.0  # time.monotonic


async def get_gigachat_token(client: httpx.AsyncClient, force_refresh: bool = False) -> str:
    global _gigachat_token, _gigachat_token_deadline
    if not force_refresh and _gigachat_token and time.monotonic() < _gigachat_token_deadline:
        return _gigachat_token
    resp = await client.post(
        GIGACHAT_OAUTH_URL,
        headers={
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Bearer {GIGACHAT_AUTH_KEY}",
        },
        data={"scope": GIGACHAT_SCOPE},
    )
    resp.raise_for_status()
    data = resp.json()
    _gigachat_token = data["access_token"]
    ttl = float(data.get("expires_at", 0)) / 1000 - time.time() - 60
    _gigachat_token_deadline = time.monotonic() + max(60.0, ttl)
    log.info("Получен токен GigaChat, действует %.0f сек", max(60.0, ttl))
    return _gigachat_token


async def ask_gigachat(messages: list[dict]) -> str:
    payload = {"model": CHAT_MODEL2, "messages": messages}
    if MAX_TOKENS > 0:
        payload["max_tokens"] = MAX_TOKENS
    # verify=True в httpx использует бандл certifi, а не системное хранилище CA,
    # куда устанавливается сертификат Минцифры, — поэтому контекст собираем сами
    verify = ssl.create_default_context() if GIGACHAT_VERIFY_SSL else False
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), verify=verify) as client:
        token = await get_gigachat_token(client)
        for attempt in range(2):  # при 401 обновляем токен и повторяем один раз
            resp = await client.post(
                f"{GIGACHAT_BASE}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 401 and attempt == 0:
                token = await get_gigachat_token(client, force_refresh=True)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    raise RuntimeError("GigaChat: не удалось выполнить запрос")


async def ask_sonar(system_prompt: str, text: str) -> str:
    """Запрос к Sonar (фактчек или поиск) — различаются только системным промптом."""
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
                    {"role": "user", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


# Джоба генерации считается завершённой/ failed по этим статусам
JOB_DONE_STATUSES = {"completed", "complete", "done", "success", "succeeded"}
JOB_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


def _extract_job_id(resp) -> str | None:
    """jobId из тела ответа или заголовка x-pv-media-job-id."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        job_id = data.get("jobId") or data.get("id")
        if job_id:
            return str(job_id)
    header_id = resp.headers.get("x-pv-media-job-id")
    return str(header_id) if header_id else None


async def _poll_job(client, job_id: str, on_tick) -> tuple[str, str] | None:
    """Опрос джобы до завершения; on_tick - колбэк для индикатора в чате."""
    headers = {"Authorization": f"Bearer {PLUSVIBE_API_KEY}"}
    deadline = time.monotonic() + IMAGE_JOB_TIMEOUT
    while True:
        await asyncio.sleep(IMAGE_POLL_INTERVAL)
        if on_tick is not None:
            await on_tick()
        poll = await client.get(f"{PLUSVIBE_BASE}/api/media/jobs/{job_id}", headers=headers)
        poll.raise_for_status()
        try:
            job = poll.json()
        except ValueError:
            log.error("Джоб-API вернул не-JSON (HTTP %s): %.1000s", poll.status_code, poll.text)
            return None
        status = str(job.get("status") or job.get("stage") or "").lower()
        if status in JOB_DONE_STATUSES:
            image = extract_image(job)
            if image is None:
                log.error("Джоба %s завершена, но результат не распознан: %.1000s", job_id, poll.text)
            return image
        if status in JOB_FAILED_STATUSES:
            log.error("Джоба %s завершилась со статусом %r: %.1000s", job_id, status, poll.text)
            return None
        if time.monotonic() > deadline:
            log.error("Таймаут ожидания джобы %s (%s сек)", job_id, IMAGE_JOB_TIMEOUT)
            return None


async def generate_image(prompt: str, on_tick=None) -> tuple[str, str] | None:
    """Генерация картинки: POST /api/media/generate, затем опрос джобы."""
    headers = {"Authorization": f"Bearer {PLUSVIBE_API_KEY}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            f"{PLUSVIBE_BASE}/api/media/generate",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "opts": {"aspect_ratio": "1:1", "resolution": "1K", "mode": "text-to-image"},
            },
        )
        resp.raise_for_status()
        job_id = _extract_job_id(resp)
        if job_id:
            return await _poll_job(client, job_id, on_tick)
        # вдруг API вернул готовый результат без джобы
        try:
            data = resp.json()
        except ValueError:
            log.error("Медиа-API вернул не-JSON ответ (HTTP %s): %.1000s", resp.status_code, resp.text)
            return None
        image = extract_image(data)
        if image is None:
            log.error("Не распознан формат ответа медиа-API: %.1000s", resp.text)
        return image


async def edit_image(prompt: str, image_bytes: bytes, on_tick=None) -> tuple[str, str] | None:
    """Правка исходного изображения: multipart в POST /v1/images/edits, затем опрос джобы."""
    headers = {"Authorization": f"Bearer {PLUSVIBE_API_KEY}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            f"{PLUSVIBE_BASE}/v1/images/edits",
            headers=headers,
            files={"image": ("image.png", image_bytes, "image/png")},
            data={"prompt": prompt, "model": IMAGE_MODEL},
        )
        resp.raise_for_status()
        job_id = _extract_job_id(resp)
        if job_id:
            return await _poll_job(client, job_id, on_tick)
        try:
            data = resp.json()
        except ValueError:
            log.error("Edit-API вернул не-JSON (HTTP %s): %.1000s", resp.status_code, resp.text)
            return None
        image = extract_image(data)
        if image is None:
            log.error("Не распознан формат ответа edit-API: %.1000s", resp.text)
        return image


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    # Ключевое слово второй модели: вырезаем его из текста и запоминаем выбор
    use_model2 = bool(CHAT_MODEL2 and MODEL2_RE and MODEL2_RE.search(text))
    if use_model2:
        text = strip_model2_keyword(text) or text
        if not GIGACHAT_AUTH_KEY:
            use_model2 = False
            await message.reply_text("GigaChat не настроен (нет GIGACHAT_AUTH_KEY), отвечу основной моделью.")
    prompt = build_prompt(text, message)
    ack = None  # заглушка, отправленная сразу; по завершении редактируется в результат

    try:
        # правка картинки: к запросу процитировано сообщение с фото + слова правки
        edit_photo = None
        if message.reply_to_message is not None and IMAGE_EDIT_RE.search(text):
            if message.reply_to_message.photo:
                edit_photo = message.reply_to_message.photo[-1]

        if IMAGE_TRIGGER_RE.search(text) or edit_photo is not None:
            # лимит на генерацию свой для каждого пользователя в личке
            # и для каждой группы, если бот в группе
            scope = ("user", user.id) if chat.type == ChatType.PRIVATE else ("chat", chat.id)
            now = time.monotonic()
            last = _last_image_at.get(scope)
            if last is not None and now - last < IMAGE_COOLDOWN:
                remaining = IMAGE_COOLDOWN - (now - last)
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

            async def _tick():  # держим индикатор «загружает фото» на каждом цикле опроса
                await message.chat.send_action(ChatAction.UPLOAD_PHOTO)

            if edit_photo is not None:
                caption = f"✏️ Отредактировано по запросу от {user.full_name}: {text}"
                note = f"[отредактировал картинку по запросу: {text}]"
                tg_file = await context.bot.get_file(edit_photo.file_id)
                async with httpx.AsyncClient(timeout=60.0) as dl:
                    photo_bytes = (await dl.get(tg_file.file_path)).content
                image = await edit_image(strip_bot_mention(prompt, me.username, me.first_name), photo_bytes, on_tick=_tick)
            else:
                caption = f"🎨 Запрос от {user.full_name}: {text}"
                note = f"[нарисовал картинку по запросу: {text}]"
                image = await generate_image(strip_bot_mention(prompt, me.username, me.first_name), on_tick=_tick)
            if image is None:
                await ack.edit_text("Не смог разобрать ответ генератора картинок 😔")
                return
            kind, value = image
            if kind == "url":
                async with httpx.AsyncClient(timeout=120.0) as client:
                    img_bytes = (
                        await client.get(
                            value,
                            headers={"Authorization": f"Bearer {PLUSVIBE_API_KEY}"},
                        )
                    ).content
            else:
                img_bytes = base64.b64decode(value)
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
            _last_image_at[scope] = time.monotonic()  # фиксируем только успешную генерацию
            _history[chat.id].append({"role": "user", "content": prompt})
            _history[chat.id].append({"role": "assistant", "content": note})
            return

        # Фактчек и поиск идут через одну модель (Sonar), различаются системным промптом
        sonar_system = None
        if FACTCHECK_TRIGGER_RE.search(text):
            sonar_system = FACTCHECK_SYSTEM_PROMPT
        elif SEARCH_TRIGGER_RE.search(text):
            sonar_system = SEARCH_SYSTEM_PROMPT
        if sonar_system is not None:
            ack = await message.reply_text(
                random.choice(ACK_PHRASES),
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
            await message.chat.send_action(ChatAction.TYPING)
            verdict = await ask_sonar(sonar_system, prompt)
            formatted = md_to_tg_html(verdict)
            try:
                await ack.edit_text(safe_truncate_html(formatted), parse_mode=ParseMode.HTML)
            except TelegramError:  # не разобрали markdown — шлём упрощённый текст
                await ack.edit_text(md_to_plain(verdict)[:4000])
            _history[chat.id].append({"role": "user", "content": prompt})
            _history[chat.id].append({"role": "assistant", "content": verdict[:4000]})
            return

        await message.chat.send_action(ChatAction.TYPING)
        history = list(_history[chat.id])
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": prompt}]
        if use_model2:
            # перед ответом неосновной модели предупреждаем об этом отдельным сообщением
            await message.reply_text(
                f"🔀 Отвечает {CHAT_MODEL2} — не основная модель.",
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
            answer = await ask_gigachat(messages)
        else:
            answer = await ask_llm(messages)
        if not answer:
            answer = "…"
        await send_formatted(
            message,
            answer,
            reply_parameters=ReplyParameters(message_id=message.message_id),
        )
        _history[chat.id].append({"role": "user", "content": prompt})
        _history[chat.id].append({"role": "assistant", "content": answer})
    except httpx.HTTPStatusError as e:
        log.error("HTTP %s от API: %s", e.response.status_code, e.response.text[:500])
        await _reply_error(message, ack, "Упс, у меня не получилось выполнить запрос. Попробуй ещё раз чуть позже.")
    except Exception as e:  # заглушка не должна остаться без ответа
        log.error("Ошибка: %s", e)
        await _reply_error(message, ack, "Что-то пошло не так 🤖")


async def _reply_error(message, ack, text: str) -> None:
    """Ошибку пишем в заглушку, если она есть, чтобы не плодить сообщения."""
    if ack is not None:
        await ack.edit_text(text)
    else:
        await message.reply_text(text)


async def send_formatted(message, text: str, **kwargs) -> None:
    """Отправка ответа: Markdown модели -> HTML Telegram; при ошибке парсинга - plain text."""
    try:
        await message.reply_text(
            safe_truncate_html(md_to_tg_html(text)), parse_mode=ParseMode.HTML, **kwargs
        )
    except TelegramError:
        await message.reply_text(md_to_plain(text)[:4000], **kwargs)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        me = await context.bot.get_me()
        minutes = max(1, IMAGE_COOLDOWN // 60)
        model2_hint = (
            f"• Отвечу другой моделью ({CHAT_MODEL2}), если добавить в сообщение слово «{MODEL2_KEYWORDS[0]}».\n"
            if CHAT_MODEL2 and MODEL2_RE
            else ""
        )
        await update.effective_message.reply_text(
            f"Привет! Я {me.first_name} — ИИ-ассистент в этом чате.\n\n"
            "Что я умею:\n"
            "• В личке отвечаю на любое сообщение;\n"
            f"• В группах — когда обратятся по имени (@{me.username} или просто «{me.first_name}») "
            "или процитируют моё сообщение;\n"
            "• Помнить контекст нашего разговора в этом чате;\n"
            f"• Рисовать картинки по запросу «нарисуй …» — но не чаще раза в {minutes} мин;\n"
            "• Проверять утверждения на достоверность: спроси «правда ли, что …» — найду источники;\n"
            "• Искать информацию по запросу: «найди информацию про …» — расскажу и дам источники.\n"
            f"{model2_hint}"
            f"\nПопробуй: «@{me.username}, привет!» или «@{me.username}, нарисуй котика»"
        )


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в .env")
    if not PLUSVIBE_API_KEY:
        raise SystemExit("Не задан PLUSVIBE_API_KEY в .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    # UpdateType.MESSAGE — только новые сообщения: без правок и реакций (лайков)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_message))

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
