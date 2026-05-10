# Минимальный образ для Telegram-бота скачивания YouTube/Instagram
FROM python:3.11-slim

# Системные зависимости:
#   ffmpeg — постпроцессинг (склейка видео+аудио, MP3)
#   curl/unzip — для скачивания Deno
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno — JavaScript runtime для yt-dlp-ejs плагина.
# Решает n-signature challenge YouTube (без него с мая 2026 нельзя
# получить рабочие URL для скачивания, даже с куками и PO Token).
# Поддерживает x86_64 и aarch64 (Apple Silicon Docker).
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)         DENO_ARCH=x86_64-unknown-linux-gnu ;; \
        aarch64|arm64)  DENO_ARCH=aarch64-unknown-linux-gnu ;; \
        *) echo "Unsupported arch: $ARCH"; exit 1 ;; \
    esac && \
    echo "Installing Deno for $DENO_ARCH" && \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip && \
    unzip /tmp/deno.zip -d /usr/local/bin && \
    rm /tmp/deno.zip && \
    chmod +x /usr/local/bin/deno && \
    deno --version

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
