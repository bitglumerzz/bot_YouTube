# bot_YouTube

Telegram-бот для скачивания видео и аудио с YouTube и Instagram.
На базе `aiogram 3` + `yt-dlp` + `ffmpeg`. С Local Bot API server'ом
поддерживает отправку файлов до **2 ГБ** одним куском.

## Структура

```
bot_YouTube/
├── bot.py                # код бота
├── requirements.txt      # python-зависимости
├── .env.example          # шаблон конфигурации (скопируйте в .env)
├── Dockerfile            # образ контейнера с ботом
├── docker-compose.yml    # запуск бота + Local Bot API server
├── .dockerignore
├── .gitignore
├── setup.sh              # установка локально (macOS, без Docker)
└── run.sh                # запуск локально
```

## Запуск через Docker (рекомендуется)

### Шаг 1. Получить токен бота

Откройте `@BotFather` в Telegram → `/newbot` → задайте имя → получите токен
вида `123456789:AAEcMa...`

### Шаг 2. Получить api_id и api_hash для Local Bot API server

Это нужно один раз, чтобы поднять локальный API сервер и снять лимит 50 МБ.

1. Открыть https://my.telegram.org/auth
2. Войти по своему номеру (придёт код в Telegram)
3. Зайти в **API development tools**
4. **Create new application**: название `MyBot`, остальное любое
5. Скопировать **api_id** (число) и **api_hash** (32 символа)

### Шаг 3. Заполнить .env и запустить

```bash
cd ~/bot_YouTube
cp .env.example .env
nano .env                       # впишите BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH
docker compose up -d --build    # сборка и запуск (бот + bot-api сервер)
docker compose logs -f          # смотреть логи
```

При первом запуске локальный API сервер инициализируется ~30 секунд. В логах
бота должна появиться строка:

```
Bot API: локальный сервер http://bot-api:8081 (лимит 2 ГБ)
```

После этого пишите боту в Telegram `/start`.

### Управление

```bash
docker compose down                    # остановить
docker compose up -d --build           # перезапустить с обновлением
docker compose logs -f bot             # логи только бота
docker compose logs -f bot-api         # логи только API сервера
```

Скачанные файлы появляются в `~/bot_YouTube/downloads/`.

### Как работать без Local Bot API server

Если не хотите получать api_id/api_hash — закомментируйте строку
`LOCAL_BOT_API_URL=...` в `docker-compose.yml`. Бот будет работать через
официальный Bot API с лимитом 50 МБ; файлы больше будут резаться на части.

## Запуск локально без Docker (macOS)

```bash
cd ~/bot_YouTube
bash setup.sh             # ставит Homebrew, ffmpeg, python venv, зависимости
nano .env                 # впишите BOT_TOKEN
bash run.sh
```

В этом режиме Local Bot API server не запускается, лимит — 50 МБ.

## Команды бота

`/start`, `/help` — справка.
`/check` — проверить видео и выбрать качество кнопками.
`/normal`, `/audio`, `/compress`, `/split`, `/ios` — режимы скачивания.
`/q1080`, `/q720`, `/q480`, `/q360` — фиксированное качество.
`/test_cookies` — проверка извлечения куки из браузеров.
`/stats` — состояние очереди.

Можно просто кинуть боту ссылку — он сам покажет inline-меню выбора качества.

## Что бот делает с видео

1. Скачивает с YouTube/Instagram через `yt-dlp` (с прогресс-баром)
2. Применяет `ffmpeg -movflags +faststart` — moov atom переезжает в начало,
   плеер Telegram сразу начинает воспроизведение без буферизации в конец
3. Через `ffprobe` берёт реальные `width`/`height`/`duration` — Telegram
   рисует плеер в правильном aspect ratio (без растяжения)
4. Отправляет с прогресс-индикатором времени и размера

## Траблшутинг

**Бот падает с `BOT_TOKEN не задан`** — забыли создать `.env` или вписать токен.

**`ffmpeg: not found` в native-режиме** — `brew install ffmpeg` (в Docker уже есть).

**Local Bot API контейнер падает с `api_id is empty`** — забыли заполнить
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` в `.env`. См. шаг 2 выше.

**Instagram возвращает ошибку (только в native-режиме)** — закройте все окна
Chrome/Firefox перед скачиванием. В Docker используйте `cookies.txt`.

**Файл всё равно >2 ГБ** — сильно длинное 4K-видео. Используйте качество ниже.

**Видео приходит, но плеер не начинает играть сразу** — проверьте логи на
строку `+faststart применён`. Если её нет, ffmpeg-перепаковка не сработала.

**Бот молчит после нажатия кнопки качества** — посмотрите `docker compose logs -f bot`,
скорее всего yt-dlp упал на конкретной ссылке.
