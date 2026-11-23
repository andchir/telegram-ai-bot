# Деплой telegram-ai-bot на продакшн

Руководство по развёртыванию бота на собственном сервере (VPS) под управлением systemd.

## Что понадобится

- Сервер с Ubuntu 22.04/24.04 и доступом по SSH.
- Токен Telegram (`TELEGRAM_BOT_TOKEN`) от @BotFather.
- Ключ PlusVibe (`PLUSVIBE_API_KEY`).
- Домен (только для режима webhook).

## Кратко: два варианта

| | Polling | Webhook |
|---|---|---|
| Что делает бот | Сам опрашивает Telegram | Telegram шлёт запросы на ваш сервер |
| Открытые порты | Не нужны | 80/443 (HTTPS) |
| Нужен домен и TLS | Нет | Да |
| Когда выбирать | Простейший старт, NAT, домашний сервер | Классический продакшн, меньше лишнего трафика |

Оба варианта ниже: установка одинаковая, отличается только `.env` и (для webhook) nginx.

## 1. Установка на сервере

```bash
# системные пакеты
sudo apt update && sudo apt install -y python3 python3-venv git

# пользователь для бота (не root)
sudo useradd -r -m -d /opt/telegram-ai-bot bot
sudo -iu bot bash

# код
cd /opt/telegram-ai-bot
git clone https://github.com/<you>/telegram-ai-bot.git app
cd app

# окружение
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# конфигурация
cp .env.example .env
nano .env   # TELEGRAM_BOT_TOKEN, PLUSVIBE_API_KEY, SYSTEM_PROMPT и т.д.
```

Права на `.env` должны быть только у пользователя `bot` (`.env` уже в `.gitignore` и не попадёт в git).

## 2. Вариант A — polling

В `.env` ничего дополнительно менять не нужно (`BOT_MODE=polling` по умолчанию). Переходите сразу к systemd (шаг 3).

## 3. Вариант B — webhook

В `.env`:

```bash
BOT_MODE=webhook
WEBHOOK_URL=https://bot.example.com
WEBHOOK_PORT=8080          # локальный порт, наружу не публикуем
WEBHOOK_SECRET_TOKEN=<длинная-случайная-строка>
```

### 3.1. DNS

`bot.example.com` → A-запись на IP сервера.

### 3.2. nginx как reverse proxy с TLS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/telegram-ai-bot`:

```nginx
server {
    listen 80;
    server_name bot.example.com;

    location /webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/telegram-ai-bot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# бесплатный сертификат (автоматически настроит HTTPS и редирект 80→443)
sudo certbot --nginx -d bot.example.com
```

Файрвол:

```bash
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

Порт 8080 наружу открывать не нужно — nginx проксирует на `127.0.0.1`.

## 4. systemd-сервис (обязательно для обоих вариантов)

`/etc/systemd/system/telegram-ai-bot.service`:

```ini
[Unit]
Description=telegram-ai-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bot
Group=bot
WorkingDirectory=/opt/telegram-ai-bot/app
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/telegram-ai-bot/app/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-ai-bot
sudo systemctl status telegram-ai-bot
```

## 5. Проверка и логи

```bash
# логи бота
journalctl -u telegram-ai-bot -f

# в логе должно быть: «Бот запущен в режиме polling» или «... webhook: https://bot.example.com/webhook»
```

Проверка webhook вручную:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
# в ответе должно быть "url":"https://bot.example.com/webhook" и пустой "last_error_message"
```

Напишите боту `@<имя_бота> привет` или процитируйте его сообщение — должен ответить. Затем: `@<имя_бота> нарисуй кота` — через несколько десятков секунд придёт картинка.

## 6. Обновление

```bash
sudo -iu bot bash
cd /opt/telegram-ai-bot/app
git pull
.venv/bin/pip install -r requirements.txt
exit
sudo systemctl restart telegram-ai-bot
```

## 7. Эксплуатация

- **Лимит генерации картинок** сбрасывается при перезапуске (память в процессе) — перезапуск между генерациями обнуляет таймер.
- **Мониторинг**: `systemctl status telegram-ai-bot` + `journalctl`; при желании добавьте уведомления о падении юнита (например, через `OnFailure=` в unit-файле).
- **Секреты**: `.env` лежит на сервере и не коммитится; при компрометации ключа перевыпустите токен у @BotFather и ключ PlusVibe.
- **Несколько экземпляров**: не запускайте два процесса с одним токеном — в polling они будут дублировать ответы, в webhook конфликтуют вебхуки.

## 8. Docker (необязательно)

Если сервер работает на Docker, минимальный образ:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
```

```bash
docker build -t telegram-ai-bot .
docker run -d --name telegram-ai-bot --restart always --env-file .env telegram-ai-bot
```

Для webhook пробросьте порт: `-p 127.0.0.1:8080:8080` (nginx на хосте проксирует в контейнер).
