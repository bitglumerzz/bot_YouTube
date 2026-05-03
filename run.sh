#!/usr/bin/env bash
# Запуск бота локально (без Docker).
set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "venv не найден. Сначала запустите: bash setup.sh"
  exit 1
fi

if [ ! -f ".env" ]; then
  echo ".env не найден. Скопируйте .env.example в .env и впишите BOT_TOKEN."
  exit 1
fi

# Подгружаем .env (только строки KEY=VALUE без комментариев)
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# shellcheck disable=SC1091
source venv/bin/activate
exec python bot.py
