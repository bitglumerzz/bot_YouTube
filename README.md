# bot_YouTube

Telegram-бот для скачивания видео и аудио с YouTube и Instagram.
На базе `aiogram 3` + `yt-dlp` + `ffmpeg`. С Local Bot API server'ом
поддерживает отправку файлов до **2 ГБ** одним куском.

## Поведение

Пришлите боту ссылку YouTube или Instagram → появится меню из двух кнопок:

- **🎬 Видео** — скачивает в mp4 и присылает в Telegram
- **🎵 MP3 (только звук)** — извлекает аудиодорожку и присылает как MP3

Во время работы бот шлёт прогресс-сообщения: процент скачивания, скорость, размер, оставшееся время и таймер отправки в Telegram.

## Запуск через Docker (рекомендуется)

### Шаг 1. Получить токен бота

Откройте `@BotFather` в Telegram → `/newbot` → задайте имя → скопируйте токен вида `123456789:AAEcMa...`

### Шаг 2. Скопировать `.env.example` в `.env`

```bash
cd ~/bot_YouTube
cp .env.example .env
```

### Шаг 3. Вписать BOT_TOKEN в `.env`

**Вариант A — через редактор `nano`** (если привычнее визуально):

```bash
nano .env
```

Внутри редактора замените значение после `BOT_TOKEN=` на свой токен. Сохранение: `Ctrl+O`, `Enter`. Выход: `Ctrl+X`.

**Вариант B — одной командой в терминале** (быстрее, без открытия редактора). Замените `ВАШ_ТОКЕН` на токен от BotFather и вставьте всё одним блоком:

```bash
echo "BOT_TOKEN=ВАШ_ТОКЕН" > .env && chmod 600 .env
```

Файл `.env` будет полностью перезаписан с одной строкой `BOT_TOKEN=...`. Стрелка `>` (одна) перезаписывает, двойная `>>` бы добавила в конец.

**Вариант C — заменить только токен в существующем `.env`** (если в нём другие строки помимо `BOT_TOKEN=`):

```bash
sed -i '' 's|^BOT_TOKEN=.*|BOT_TOKEN=ВАШ_НОВЫЙ_ТОКЕН|' .env
```

(на macOS обязательно `-i ''` с пустыми кавычками, в Linux/WSL — просто `-i`)

**Проверка** (без раскрытия токена в выводе):

```bash
awk -F= '{print $1 "=" length($2) " символов"}' .env
```

Должно вывести `BOT_TOKEN=46 символов` — стандартная длина токена от BotFather.

### Шаг 4. Запустить контейнеры

```bash
docker compose up -d --build
docker compose logs -f
```

В логах через ~30 секунд должна появиться строка:

```
INFO - Bot API: локальный сервер http://bot-api:8081
INFO - Run polling for bot @ваш_бот
```

После этого пишите боту в Telegram `/start`.

## Управление

```bash
docker compose down                    # остановить
docker compose up -d --force-recreate  # перезапустить с подхватом нового .env
docker compose logs -f                 # логи в реальном времени
docker compose logs -f bot             # только логи бота
docker compose logs -f bot-api         # только логи API сервера
```

**Важно:** после изменения `.env` нужен `--force-recreate`, обычный `restart` не перечитывает env-файл.

## Команды бота

`/start`, `/help` — справка.
`/check` — проверить ссылку и показать меню кнопками.
`/stats` — состояние очереди скачивания.

Можно просто кинуть боту ссылку без команды — меню появится автоматически.

## Запуск локально без Docker (macOS)

```bash
cd ~/bot_YouTube
bash setup.sh             # ставит Homebrew, ffmpeg, Python venv, зависимости
echo "BOT_TOKEN=ВАШ_ТОКЕН" > .env
chmod 600 .env
bash run.sh
```

В этом режиме Local Bot API server не запускается — лимит файла 50 МБ (официальный API Telegram).

## Папка downloads

```
~/bot_YouTube/downloads/
```

Сюда yt-dlp кладёт скачиваемые файлы. После успешной отправки в Telegram файлы автоматически удаляются. Если папка разрастается — почистите вручную:

```bash
rm ~/bot_YouTube/downloads/*
```

## Бэкап проекта

```bash
cd ~ && tar \
  --exclude='bot_YouTube/downloads' \
  --exclude='bot_YouTube/bot-api-data' \
  --exclude='bot_YouTube/venv' \
  --exclude='bot_YouTube/__pycache__' \
  --exclude='bot_YouTube/*.log' \
  -czf "bot_YouTube_backup_$(date +%Y%m%d_%H%M%S).tar.gz" bot_YouTube
```

Создаст архив с датой/временем в `~`. Восстановление: `tar -xzf bot_YouTube_backup_ВРЕМЯ.tar.gz`.

## Траблшутинг

**Бот падает с `BOT_TOKEN не задан`** — `.env` пустой или нет файла. Сделайте Шаг 3.

**`TelegramUnauthorizedError: Unauthorized`** — токен невалиден. Зайдите в `@BotFather` → `/mybots` → выбрать бота → API Token → Revoke current token → получите новый и обновите `.env`.

**Бот не отвечает на сообщения** — проверьте, что вы пишете именно тому боту, чей username в логах: `docker compose logs --tail 5 bot | grep "Run polling"`. Может быть несколько ботов от BotFather'а.

**Local Bot API не стартует** — посмотрите `docker compose logs bot-api`. Чаще всего проблема с правами на папку `bot-api-data/`. Удалите её и пересоздайте: `rm -rf bot-api-data && docker compose up -d --force-recreate`.

**Instagram возвращает ошибку** — закройте все окна Chrome/Firefox перед скачиванием (они блокируют файл базы куки).

**Видео приходит, но плеер Telegram замирает** — это значит yt-dlp скачал в кодеке (VP9 / AV1), который Telegram не варит. Откройте файл напрямую с диска: `open ~/bot_YouTube/downloads/`.
