# Минимальный образ для Telegram-бота скачивания YouTube/Instagram
FROM python:3.11-slim

# ffmpeg обязателен для постпроцессинга (склейка видео+аудио, H.265, MP3)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости (кэшируется отдельным слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Код
COPY bot.py .

# Папка для временных скачиваний (монтируется как volume в compose)
RUN mkdir -p /app/downloads

# -u  → не буферизовать stdout, чтобы логи сразу шли в docker logs
CMD ["python", "-u", "bot.py"]
