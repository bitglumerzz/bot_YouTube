# bot_YouTube

Telegram-бот для скачивания видео и аудио с YouTube и Instagram.
На базе `aiogram 3` + `yt-dlp` + `ffmpeg`.

## Структура

```
bot_YouTube/
├── bot.py                # код бота
├── requirements.txt      # python-зависимости
├── .env.example          # шаблон конфигурации (скопируйте в .env)
├── Dockerfile            # образ контейнера
├── docker-compose.yml    # запуск через docker compose
├── .dockerignore
├── .gitignore
├── setup.sh              # установка локально (macOS)
└── run.sh                # запуск локально
```

## Вариант 1. Запуск через Docker (рекомендуется)

Требуется только Docker Desktop. Внутри контейнера уже стоят Python и ffmpeg.

```bash
cd ~/bot_YouTube
cp .env.example .env
# впишите ваш BOT_TOKEN от @BotFather в .env
docker compose up -d --build
```

Логи: `docker compose logs -f`
Остановка: `docker compose down`
Пересборка после правок кода: `docker compose up -d --build`

Скачанные файлы появятся в `./downloads` (примонтировано из контейнера).

### Ограничения Docker-варианта

В контейнере нет браузеров, поэтому `cookiesfrombrowser` (автоматическое
извлечение куки из Chrome/Firefox/etc) не работает. Это влияет на:
- приватные Instagram-видео,
- ролики YouTube с возрастным ограничением.

Публичные видео работают как обычно. Если нужны куки — экспортируйте
`cookies.txt` из браузерного расширения и положите рядом с `bot.py`,
тогда добавьте в `bot.py` параметр `'cookiefile': 'cookies.txt'`.

## Вариант 2. Локальный запуск без Docker (macOS)

```bash
cd ~/bot_YouTube
bash setup.sh             # ставит Homebrew, ffmpeg, python venv, зависимости
# впишите BOT_TOKEN в созданный .env
bash run.sh
```

Здесь куки из браузеров работают автоматически (через `yt-dlp`).

## Получение BOT_TOKEN

1. Откройте Telegram, напишите `@BotFather`.
2. `/newbot` → задайте имя и юзернейм.
3. Скопируйте токен вида `123456789:AAEcMa...` в `.env`.

## Команды бота

`/start`, `/help` — справка.
`/check` — проверить видео и выбрать качество кнопками.
`/normal`, `/audio`, `/compress`, `/split`, `/ios` — режимы скачивания.
`/q1080`, `/q720`, `/q480`, `/q360` — фиксированное качество.
`/test_cookies` — проверка извлечения куки из браузеров.
`/stats` — состояние очереди.

## Траблшутинг

**Бот падает с `BOT_TOKEN не задан`** — забыли создать `.env` или вписать токен.

**`ffmpeg: not found`** — установите: `brew install ffmpeg` (или используйте Docker).

**Instagram возвращает ошибку** — закройте все окна Chrome/Firefox перед скачиванием
(они блокируют файл базы куки). В Docker-варианте используйте `cookies.txt`.

**Видео не отправляется в Telegram** — лимит обычного бота 50 МБ. Используйте
`/split` (бот сам разрежет на части) или `/compress`.

**H.265 кодирование очень медленное** — это нормально, libx265 нагружает CPU.
Если не нужен H.265, удалите блок `postprocessor_args` для соответствующего
режима в `bot.py`.
