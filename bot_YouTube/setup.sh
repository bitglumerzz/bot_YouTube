#!/usr/bin/env bash
# Локальная установка бота на macOS (без Docker).
# Запуск:  bash setup.sh

set -e

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

echo "=========================================="
echo "  Установка YouTube/Instagram бота"
echo "=========================================="
echo "Проект: $PROJECT_DIR"
echo

# 1) Homebrew
if ! command -v brew >/dev/null 2>&1; then
  echo "[1/5] Homebrew не найден. Устанавливаю..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
  echo "[1/5] Homebrew уже установлен: $(brew --version | head -1)"
fi

# 2) ffmpeg — обязателен для постпроцессинга
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[2/5] Устанавливаю ffmpeg..."
  brew install ffmpeg
else
  echo "[2/5] ffmpeg уже установлен: $(ffmpeg -version | head -1)"
fi

# 3) Python 3
if ! command -v python3 >/dev/null 2>&1; then
  echo "[3/5] Устанавливаю Python..."
  brew install python
else
  echo "[3/5] Python уже установлен: $(python3 --version)"
fi

# 4) Виртуальное окружение
if [ ! -d "venv" ]; then
  echo "[4/5] Создаю venv..."
  python3 -m venv venv
else
  echo "[4/5] venv уже существует"
fi

# 5) Зависимости
echo "[5/5] Устанавливаю Python-пакеты..."
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

# .env
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo
  echo "📝 Создан файл .env — впишите туда BOT_TOKEN от @BotFather"
fi

echo
echo "=========================================="
echo "  Готово!"
echo "=========================================="
echo
echo "Дальше:"
echo "  1. Откройте .env и подставьте BOT_TOKEN от @BotFather."
echo "  2. Запуск:  bash run.sh"
echo
