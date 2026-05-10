import asyncio
import os
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import yt_dlp
from pathlib import Path
from datetime import datetime
from collections import deque
import time
import sys
import aiofiles
import math
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'bot_{datetime.now().strftime("%Y%m%d")}.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Токен вашего бота от @BotFather.
# Берётся из переменной окружения BOT_TOKEN (см. .env / docker-compose.yml).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        "❌ BOT_TOKEN не задан.\n"
        "   • Локально:  export BOT_TOKEN='123456:ABC...' && python bot.py\n"
        "   • Docker:    укажите BOT_TOKEN в файле .env (см. .env.example)"
    )

# Инициализация бота и диспетчера с хранилищем состояний
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния для FSM
class DownloadStates(StatesGroup):
    waiting_for_check_url = State()
    waiting_for_download_url = State()

# Папка для временных файлов
DOWNLOAD_PATH = Path("downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

# Размеры файлов
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB для обычной отправки
CHUNK_SIZE = 49 * 1024 * 1024  # 49 MB для частей
PREMIUM_MAX_SIZE = 2000 * 1024 * 1024  # 2 GB максимум

# Очередь загрузок
download_queue = asyncio.Queue()
active_downloads = {}
max_concurrent_downloads = 2

# Режимы работы для каждого пользователя
user_modes = {}

# Определяем режимы
class DownloadMode:
    NORMAL = "normal"
    AUDIO = "audio"
    SPLIT = "split"
    COMPRESS = "compress"

def format_duration(duration):
    """Безопасно форматирует длительность в минуты:секунды"""
    if not duration:
        return "0:00"

    try:
        duration_int = int(float(duration))
        minutes = duration_int // 60
        seconds = duration_int % 60
        return f"{minutes}:{seconds:02d}"
    except (ValueError, TypeError):
        return "0:00"

def extract_url(text):
    """Извлекает URL видео из текста (YouTube, Instagram)"""
    if not text:
        return None

    # YouTube regex
    youtube_regex = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'
    match = re.search(youtube_regex, text)
    if match:
        video_id = match.group(6)
        return f'https://www.youtube.com/watch?v={video_id}'

    # Instagram регулярки
    instagram_patterns = [
        # Обычные посты и рилсы
        r'(https?://)?(www\.)?(instagram\.com)/(p|reel|reels|tv)/([a-zA-Z0-9_-]+)(/)?(\?.*)?',
        # Stories (если понадобятся)
        r'(https?://)?(www\.)?(instagram\.com)/stories/([^/]+)/([0-9]+)(/)?(\?.*)?',
        # IGTV
        r'(https?://)?(www\.)?(instagram\.com)/tv/([a-zA-Z0-9_-]+)(/)?(\?.*)?'
    ]

    for pattern in instagram_patterns:
        match = re.search(pattern, text)
        if match:
            # Формируем полную URL-ку
            scheme = match.group(1) if match.group(1) else 'https://'
            domain = match.group(3)

            if 'stories' in pattern:
                username = match.group(4)
                story_id = match.group(5)
                return f"{scheme}{domain}/stories/{username}/{story_id}/"
            else:
                post_type = match.group(4)
                post_id = match.group(5)
                return f"{scheme}{domain}/{post_type}/{post_id}/"

    return None

def get_user_mode(user_id):
    """Получает текущий режим пользователя"""
    return user_modes.get(user_id, DownloadMode.NORMAL)

def set_user_mode(user_id, mode):
    """Устанавливает режим для пользователя"""
    user_modes[user_id] = mode


def _run_ydl_download(ydl_opts, url):
    """Синхронный вызов yt-dlp.
    Вынесено в отдельную функцию, чтобы запускать через asyncio.to_thread
    и не блокировать event loop."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        ydl.download([url])
        return info


def _run_ydl_extract(ydl_opts, url):
    """Только получение информации (без скачивания) — синхронно."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


async def download_video(url, message_id, mode=DownloadMode.NORMAL):
    """Скачивает видео с YouTube или Instagram используя современные методы куки и оптимизированный H.265"""

    # Определяем платформу
    is_instagram = 'instagram.com' in url

    # Базовые опции
    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'outtmpl': f'{DOWNLOAD_PATH}/{message_id}_%(title)s.%(ext)s',
        'socket_timeout': 60,
        'retries': 3,
    }

    # Настройки качества в зависимости от режима с оптимизированным H.265
    if isinstance(mode, str) and mode.startswith('quality_'):
        quality = mode.replace('quality_', '')
        format_opts = {
            'format': f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]/best',
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'postprocessor_args': [
                '-c:v', 'libx265',
                '-preset', 'medium',          # Баланс скорости и качества
                '-crf', '23',                 # Хорошее качество
                '-profile:v', 'main',         # Основной профиль H.265
                '-level', '4.0',
                '-c:a', 'libopus',
                '-b:a', '80k',
                '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p',
                '-x265-params', 'log-level=error',  # Убираем лишние логи x265
            ],
        }
        logger.info(f"Запрошено качество: {quality}p с H.265")

    elif mode == "ios_mode":
        # Для iOS H.264 для максимальной совместимости
        format_opts = {
            'format': 'best[filesize<50M][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'postprocessor_args': [
                '-c:v', 'libx264',           # H.264 для iOS совместимости
                '-preset', 'slow',
                '-crf', '23',
                '-profile:v', 'baseline',     # Базовый профиль для старых устройств
                '-level', '3.1',
                '-c:a', 'aac',                # FIX: AAC, а не opus — opus@baseline H.264 в mp4 не играет везде
                '-b:a', '128k',
                '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p',
            ],
        }
        logger.info("Режим iOS: H.264 для максимальной совместимости")

    elif mode == DownloadMode.AUDIO:
        format_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }

    elif mode == DownloadMode.COMPRESS:
        # Максимальное сжатие с H.265
        format_opts = {
            'format': 'worst[ext=mp4]/worst',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'postprocessor_args': [
                '-c:v', 'libx265',
                '-preset', 'slow',            # Медленно, но максимальное сжатие
                '-crf', '28',                 # Агрессивное сжатие (больше = меньше размер)
                '-profile:v', 'main',
                '-level', '4.0',
                '-c:a', 'libopus',
                '-b:a', '64k',                # Сжатое аудио
                '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p',
                '-x265-params', 'log-level=error:me=hex:subme=7:ref=3:b-adapt=2:direct=auto',
            ],
        }
        logger.info("Режим COMPRESS: максимальное сжатие H.265")

    elif mode == DownloadMode.SPLIT:
        # Высокое качество для больших файлов
        format_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'postprocessor_args': [
                '-c:v', 'libx265',
                '-preset', 'slow',
                '-crf', '18',                 # Высокое качество (меньше = лучше)
                '-profile:v', 'main10',       # 10-bit профиль для лучшего качества
                '-level', '5.0',
                '-c:a', 'aac',
                '-b:a', '192k',               # Высококачественное аудио
                '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p10le',    # 10-bit цвет
                '-x265-params', 'log-level=error:me=umh:subme=10:ref=6:b-adapt=2:direct=auto:me-range=24',
            ],
        }
        logger.info("Режим SPLIT: высокое качество H.265 10-bit")

    else:  # DownloadMode.NORMAL
        # Оптимальный баланс для повседневного использования
        format_opts = {
            'format': 'best[filesize<50M][ext=mp4]/bestvideo[filesize<50M][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'postprocessor_args': [
                '-c:v', 'libx265',
                '-preset', 'medium',
                '-crf', '23',
                '-profile:v', 'main',
                '-level', '4.0',
                '-c:a', 'libopus',
                '-b:a', '80k',
                '-movflags', '+faststart',
                '-pix_fmt', 'yuv420p',
                '-x265-params', 'log-level=error',
            ],
        }
        logger.info("Режим NORMAL: H.265 со сбалансированными настройками")

    # СОВРЕМЕННЫЕ СТРАТЕГИИ КУКИ
    if is_instagram:
        logger.info(f"Обнаружен Instagram URL: {url}")

        browser_strategies = [
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
            {'cookiesfrombrowser': ('edge',)},
            {'cookiesfrombrowser': ('safari',)},
            {'cookiesfrombrowser': ('brave',)},
            {'cookiesfrombrowser': ('chrome', '~/.var/app/com.google.Chrome/')},
            {
                'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
            },
        ]

        for i, strategy in enumerate(browser_strategies, 1):
            try:
                browser_name = strategy.get('cookiesfrombrowser', ['manual'])[0] if 'cookiesfrombrowser' in strategy else 'без куки'
                logger.info(f"Instagram стратегия {i}: {browser_name}")

                ydl_opts = {**base_opts, **format_opts, **strategy}

                # FIX: yt-dlp синхронный — выводим в поток, чтобы не блокировать loop
                info = await asyncio.to_thread(_run_ydl_download, ydl_opts, url)
                logger.info(f"Получена информация: {info.get('title', 'Без названия')}")

                # Ищем скачанный файл
                for file in DOWNLOAD_PATH.glob(f'{message_id}_*'):
                    if file.is_file():
                        logger.info(f"✅ Instagram: успех с браузером {browser_name}")
                        return file, info

            except Exception as e:
                error_msg = str(e).lower()

                if "could not copy" in error_msg and "cookie database" in error_msg:
                    logger.warning(f"🔒 Браузер {browser_name} открыт, база данных заблокирована")
                elif "failed to decrypt with dpapi" in error_msg:
                    logger.warning(f"🔐 Браузер {browser_name}: Ошибка расшифровки куки (DPAPI) - переключаюсь на другой браузер")
                elif "no such file or directory" in error_msg:
                    logger.info(f"❌ Браузер {browser_name} не найден")
                elif "database is locked" in error_msg:
                    logger.info(f"🔒 Браузер {browser_name} открыт, пропускаем")
                elif "permission denied" in error_msg:
                    logger.warning(f"🚫 Браузер {browser_name}: Нет доступа к файлам куки")
                else:
                    logger.warning(f"❌ Стратегия {i} ({browser_name}): {str(e)[:100]}...")
                continue

        logger.error("❌ Все Instagram стратегии не сработали")
        return None, None

    # Для YouTube
    else:
        youtube_strategies = [
            {},  # Без куки (стандартно)
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
        ]

        for i, strategy in enumerate(youtube_strategies, 1):
            try:
                ydl_opts = {**base_opts, **format_opts, **strategy}

                # Получаем info первым делом для оптимизации NORMAL режима
                info = await asyncio.to_thread(_run_ydl_extract, ydl_opts, url)

                if mode == DownloadMode.NORMAL:
                    formats = info.get('formats', [])
                    best_format = None

                    for f in formats:
                        if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                            filesize = f.get('filesize') or f.get('filesize_approx', 0)
                            if filesize and filesize < MAX_FILE_SIZE:
                                if not best_format or (f.get('height', 0) > best_format.get('height', 0)):
                                    best_format = f

                    if best_format:
                        ydl_opts['format'] = best_format['format_id']
                        logger.info(f"YouTube оптимизация: {best_format.get('height', 'unknown')}p")

                # Скачиваем
                await asyncio.to_thread(_run_ydl_download, ydl_opts, url)

                # Ищем файл
                for file in DOWNLOAD_PATH.glob(f'{message_id}_*'):
                    if file.is_file():
                        if i > 1:
                            logger.info(f"✅ YouTube: успех с куки (стратегия {i})")
                        return file, info

            except Exception as e:
                error_msg = str(e).lower()

                if "failed to decrypt with dpapi" in error_msg:
                    logger.warning(f"🔐 YouTube стратегия {i}: DPAPI ошибка - переключаюсь на другой браузер")
                elif "could not copy" in error_msg and "cookie" in error_msg:
                    logger.warning(f"🔒 YouTube стратегия {i}: Браузер заблокирован")

                if i < len(youtube_strategies):
                    logger.warning(f"YouTube стратегия {i}: {str(e)[:100]}... Пробуем следующую")
                    continue
                else:
                    logger.error(f"Ошибка YouTube: {e}", exc_info=True)
                    return None, None

    return None, None

async def cleanup_file(filepath):
    """Удаляет временный файл"""
    try:
        if filepath and filepath.exists():
            filepath.unlink()
    except Exception as e:
        logger.warning(f"Ошибка при удалении файла: {e}")

async def split_file(filepath, chunk_size=CHUNK_SIZE):
    """Разделяет большой файл на части"""
    parts = []
    file_size = filepath.stat().st_size
    parts_count = math.ceil(file_size / chunk_size)

    async with aiofiles.open(filepath, 'rb') as infile:
        for i in range(parts_count):
            part_path = filepath.parent / f"{filepath.stem}_part{i+1}{filepath.suffix}"

            async with aiofiles.open(part_path, 'wb') as outfile:
                chunk = await infile.read(chunk_size)
                await outfile.write(chunk)

            parts.append(part_path)

    return parts

async def process_download_queue():
    """Обработчик очереди загрузок"""
    while True:
        try:
            while len(active_downloads) >= max_concurrent_downloads:
                await asyncio.sleep(1)

            task = await download_queue.get()
            active_downloads[task['message'].message_id] = task
            asyncio.create_task(process_download(task))

        except Exception as e:
            logger.error(f"Ошибка в обработчике очереди: {e}", exc_info=True)

async def process_download(task):
    """Обрабатывает отдельную загрузку"""
    message = task['message']
    url = task['url']
    status_message = task['status_message']
    mode = task['mode']
    download_start_message = task.get('download_start_message')

    user_id = task.get('user_id', message.from_user.id if message.from_user else None)

    # FIX: инициализируем filepath, чтобы безопасно ссылаться в finally
    filepath = None

    try:
        logger.info(f"Начинаю загрузку: {url} в режиме {mode} для пользователя {user_id}")

        mode_text = {
            DownloadMode.AUDIO: "🎵 Скачиваю аудио...",
            DownloadMode.COMPRESS: "📦 Скачиваю в сжатом качестве...",
            DownloadMode.SPLIT: "📹 Скачиваю видео (готовлю к разделению)...",
            DownloadMode.NORMAL: "📥 Скачиваю видео..."
        }

        if isinstance(mode, str) and mode.startswith('quality_'):
            quality = mode.replace('quality_', '')
            mode_display = f"📹 Скачиваю в {quality}p..."
        else:
            mode_display = mode_text.get(mode, "📥 Скачиваю...")

        await status_message.edit_text(mode_display)

        # Скачиваем
        filepath, video_info = await download_video(url, message.message_id, mode)

        if not filepath:
            await status_message.edit_text("❌ Не удалось скачать. Проверьте ссылку или попробуйте другой режим.")
            logger.error(f"Не удалось скачать: {url}")
            return

        file_size = filepath.stat().st_size
        logger.info(f"Файл скачан: {filepath.name}, размер: {file_size // 1024 // 1024} MB")

        # Обработка в зависимости от режима
        if mode == DownloadMode.AUDIO:
            await status_message.edit_text("📤 Отправляю аудио...")
            audio_file = types.FSInputFile(filepath)
            await message.answer_audio(
                audio_file,
                title=video_info.get('title', 'Аудио'),
                performer=video_info.get('uploader', 'Неизвестный исполнитель'),
                duration=int(video_info.get('duration', 0)) if video_info.get('duration') else 0
            )

        elif (mode == DownloadMode.SPLIT or (isinstance(mode, str) and mode.startswith('quality_'))) and file_size > MAX_FILE_SIZE:
            await status_message.edit_text(f"✂️ Разделяю на части ({file_size // 1024 // 1024} MB)...")

            parts = await split_file(filepath)
            total_parts = len(parts)

            for i, part_path in enumerate(parts, 1):
                await status_message.edit_text(f"📤 Отправляю часть {i}/{total_parts}...")
                part_file = types.FSInputFile(part_path)
                await message.answer_document(
                    part_file,
                    caption=f"📹 {video_info.get('title', 'Видео')} - Часть {i}/{total_parts}"
                )
                await cleanup_file(part_path)

        else:
            # FIX: даже COMPRESS может не уложиться в 50 MB — раньше код падал на send.
            # Вместо этого режем на части, а не отказываемся.
            if file_size > MAX_FILE_SIZE:
                await status_message.edit_text(
                    f"⚠️ Файл больше 50 MB ({file_size // 1024 // 1024} MB). "
                    f"Делю на части..."
                )
                parts = await split_file(filepath)
                total_parts = len(parts)
                for i, part_path in enumerate(parts, 1):
                    await status_message.edit_text(f"📤 Отправляю часть {i}/{total_parts}...")
                    part_file = types.FSInputFile(part_path)
                    await message.answer_document(
                        part_file,
                        caption=f"📹 {video_info.get('title', 'Видео')} - Часть {i}/{total_parts}"
                    )
                    await cleanup_file(part_path)
            else:
                await status_message.edit_text("📤 Отправляю видео...")
                video_file = types.FSInputFile(filepath)

                caption = f"🎬 {video_info.get('title', 'Видео')}\n"
                caption += f"👤 {video_info.get('uploader', 'Неизвестный автор')}\n"

                duration = video_info.get('duration', 0)
                caption += f"⏱️ {format_duration(duration)}"

                if mode == DownloadMode.COMPRESS:
                    caption += "\n📦 Сжатое качество"
                elif isinstance(mode, str) and mode.startswith('quality_'):
                    quality = mode.replace('quality_', '')
                    caption += f"\n🎬 Качество: {quality}p"

                await message.answer_video(video_file, caption=caption)

        # Удаляем статусное сообщение
        try:
            await status_message.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить статус: {e}")

        if download_start_message:
            try:
                await download_start_message.delete()
                logger.info("Удалено сообщение о начале скачивания")
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение о начале скачивания: {e}")

        logger.info(f"Успешно отправлено: {filepath.name}")

    except Exception as e:
        logger.error(f"Ошибка при обработке загрузки: {e}", exc_info=True)
        try:
            await status_message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
        except Exception:
            pass

    finally:
        if filepath:
            await cleanup_file(filepath)
        if message.message_id in active_downloads:
            del active_downloads[message.message_id]

async def get_queue_position():
    """Получает текущую позицию в очереди"""
    return download_queue.qsize() + len(active_downloads)

@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    await state.clear()

    user_mode = get_user_mode(message.from_user.id)
    mode_name = {
        DownloadMode.NORMAL: "📹 Обычный",
        DownloadMode.AUDIO: "🎵 Только аудио",
        DownloadMode.SPLIT: "✂️ С разделением",
        DownloadMode.COMPRESS: "📦 Сжатый"
    }

    if isinstance(user_mode, str) and user_mode.startswith('quality_'):
        quality = user_mode.replace('quality_', '')
        current_mode = f"🎬 Качество {quality}p"
    else:
        current_mode = mode_name.get(user_mode, '📹 Обычный')

    await message.answer(
        "👋 Привет! Я бот для скачивания видео с YouTube и Instagram.\n\n"
        "🎯 **Два способа использования:**\n\n"
        "**1️⃣ Быстрый режим:**\n"
        "• Выберите режим (/normal, /audio, и т.д.)\n"
        "• Отправьте ссылку\n\n"
        "**2️⃣ С проверкой (рекомендуется):**\n"
        "• Используйте /check\n"
        "• Отправьте ссылку\n"
        "• Выберите качество кнопкой\n\n"
        "📌 **Команды:**\n"
        "/check - проверить видео и выбрать качество\n"
        "/normal - обычное качество\n"
        "/audio - только аудио\n"
        "/compress - сжатое видео\n"
        "/split - с разделением больших файлов\n\n"
        f"Текущий режим: {current_mode}\n\n"
        "💡 **Instagram (новинка 2025):**\n"
        "Просто войдите в аккаунт через браузер - куки извлекутся автоматически!\n\n"
        "🔧 Используйте /test_cookies для проверки браузеров",
        parse_mode="Markdown"
    )

@dp.message(Command("ios"))
async def ios_mode_handler(message: Message):
    """Включает режим совместимости с iOS"""
    set_user_mode(message.from_user.id, "ios_mode")
    await message.answer(
        "📱 **Режим совместимости с iOS**\n\n"
        "✅ Включена оптимизация для iPhone/iPad:\n"
        "• Видеокодек: H.264 (baseline profile)\n"
        "• Аудиокодек: AAC\n"
        "• Оптимизация для быстрого воспроизведения\n\n"
        "Теперь отправьте ссылку на видео.\n\n"
        "💡 Этот режим гарантирует воспроизведение на всех устройствах Apple.",
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def help_handler_final(message: Message):
    """Финальная версия справки"""
    await message.answer(
        "📚 **Бот для скачивания видео (2025)**\n\n"
        "**🎯 Платформы:**\n"
        "• YouTube (любые ссылки)\n"
        "• Instagram (автоматические куки!)\n\n"

        "**📹 Режимы:**\n"
        "/normal - стандартное качество\n"
        "/ios - совместимость с iPhone\n"
        "/audio - только звук (MP3)\n"
        "/compress - сжатое видео\n"
        "/split - для больших файлов\n"
        "/q1080, /q720, /q480 - конкретное качество\n\n"

        "**🔍 Анализ:**\n"
        "/check - проверить видео перед скачиванием\n\n"

        "**🍪 Instagram (новинка 2025):**\n"
        "/instagram - справка по Instagram\n"
        "/cookies - современные куки\n"
        "/test_cookies - проверить браузеры\n\n"

        "**💡 Для Instagram:**\n"
        "Просто войдите в аккаунт через браузер - \n"
        "куки извлекутся автоматически!\n\n"

        "**⚡ Быстрый старт:**\n"
        "1. Выберите режим (например, /normal)\n"
        "2. Отправьте ссылку на видео\n"
        "3. Ждите результат!\n\n"

        "🆘 Проблемы? Используйте /test_cookies",
        parse_mode="Markdown"
    )

@dp.message(Command("normal"))
async def normal_mode_handler(message: Message):
    """Включает обычный режим"""
    set_user_mode(message.from_user.id, DownloadMode.NORMAL)
    await message.answer(
        "📹 Режим: ОБЫЧНЫЙ\n\n"
        "Теперь отправьте ссылку на видео.\n"
        "Видео будет скачано в стандартном качестве (до 50 MB)."
    )

@dp.message(Command("audio"))
async def audio_mode_handler(message: Message):
    """Включает режим аудио"""
    set_user_mode(message.from_user.id, DownloadMode.AUDIO)
    await message.answer(
        "🎵 Режим: ТОЛЬКО АУДИО\n\n"
        "Теперь отправьте ссылку на видео.\n"
        "Будет извлечено только аудио в формате MP3."
    )

@dp.message(Command("compress"))
async def compress_mode_handler(message: Message):
    """Включает режим сжатия"""
    set_user_mode(message.from_user.id, DownloadMode.COMPRESS)
    await message.answer(
        "📦 Режим: СЖАТОЕ ВИДЕО\n\n"
        "Теперь отправьте ссылку на видео.\n"
        "Видео будет скачано в минимальном качестве для экономии размера."
    )

@dp.message(Command("split"))
async def split_mode_handler(message: Message):
    """Включает режим разделения"""
    set_user_mode(message.from_user.id, DownloadMode.SPLIT)
    await message.answer(
        "✂️ Режим: РАЗДЕЛЕНИЕ БОЛЬШИХ ФАЙЛОВ\n\n"
        "Теперь отправьте ссылку на видео.\n"
        "Большие видео будут автоматически разделены на части по 49 MB."
    )

@dp.message(Command("check"))
async def check_command_handler(message: Message, state: FSMContext):
    """Начинает процесс проверки видео"""
    await state.set_state(DownloadStates.waiting_for_check_url)
    await message.answer(
        "🔍 **Проверка видео**\n\n"
        "Отправьте мне ссылку на YouTube или Instagram видео, и я покажу:\n"
        "• Доступные качества\n"
        "• Размеры файлов\n"
        "• Рекомендации по скачиванию\n\n"
        "👉 Отправьте ссылку:",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(DownloadStates.waiting_for_check_url))
async def check_url_handler(message: Message, state: FSMContext):
    """Обрабатывает ссылку для проверки"""
    if not message.text:
        await message.answer("❌ Пожалуйста, отправьте текстовое сообщение со ссылкой")
        return

    url = extract_url(message.text)
    if not url:
        await message.answer(
            "❌ Не могу найти поддерживаемую ссылку\n\n"
            "Поддерживаемые платформы:\n"
            "• YouTube (youtube.com, youtu.be)\n"
            "• Instagram (instagram.com/p/, instagram.com/reels/, instagram.com/tv/)"
        )
        return

    await state.update_data(video_url=url)

    status_message = await message.answer("🔍 Анализирую видео...")

    is_instagram = 'instagram.com' in url

    if is_instagram:
        analysis_strategies = [
            {'name': 'Chrome', 'opts': {'cookiesfrombrowser': ('chrome',)}},
            {'name': 'Firefox', 'opts': {'cookiesfrombrowser': ('firefox',)}},
            {'name': 'Edge', 'opts': {'cookiesfrombrowser': ('edge',)}},
            {'name': 'Safari', 'opts': {'cookiesfrombrowser': ('safari',)}},
            {'name': 'Без куки', 'opts': {'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'}},
        ]
    else:
        analysis_strategies = [
            {'name': 'Без куки', 'opts': {}},
            {'name': 'Chrome', 'opts': {'cookiesfrombrowser': ('chrome',)}},
            {'name': 'Firefox', 'opts': {'cookiesfrombrowser': ('firefox',)}},
        ]

    info = None
    successful_strategy = None

    for strategy in analysis_strategies:
        try:
            strategy_name = strategy['name']
            strategy_opts = strategy['opts']

            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                **strategy_opts
            }

            logger.info(f"Анализ {strategy_name}: пробуем получить информацию о видео")

            # FIX: вынесли в отдельный поток, чтобы не блокировать loop
            info = await asyncio.to_thread(_run_ydl_extract, ydl_opts, url)
            successful_strategy = strategy_name
            logger.info(f"✅ Анализ успешен с помощью: {strategy_name}")
            break

        except Exception as e:
            error_msg = str(e).lower()

            if "failed to decrypt with dpapi" in error_msg:
                logger.warning(f"🔐 Анализ {strategy_name}: DPAPI ошибка - пробуем другой браузер")
            elif "could not copy" in error_msg and "cookie database" in error_msg:
                logger.warning(f"🔒 Анализ {strategy_name}: Браузер заблокирован")
            elif "no such file" in error_msg or "cannot find" in error_msg:
                logger.info(f"❌ Анализ {strategy_name}: Браузер не установлен")
            elif "database is locked" in error_msg:
                logger.warning(f"🔒 Анализ {strategy_name}: База данных заблокирована")
            else:
                logger.warning(f"⚠️ Анализ {strategy_name}: {str(e)[:100]}...")

            continue

    if not info:
        await status_message.edit_text(
            f"❌ Не удалось проанализировать видео\n\n"
            f"**Возможные причины:**\n"
            f"• Видео приватное или удалено\n"
            f"• Проблемы с доступом к {('Instagram' if is_instagram else 'YouTube')}\n"
            f"• Все браузеры заблокированы\n\n"
            f"**Попробуйте:**\n"
            f"• Закрыть все браузеры и повторить\n"
            f"• Использовать /test_cookies для диагностики\n"
            f"• Скачать напрямую без анализа"
        )
        await state.clear()
        return

    try:
        title = info.get('title', 'Неизвестно')
        duration = info.get('duration', 0) or 0

        formats = info.get('formats', [])

        quality_info = {}
        available_qualities = set()

        # Аудио
        audio_formats = {}
        for f in formats:
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                format_id = f.get('format_id', '')
                filesize = f.get('filesize') or f.get('filesize_approx') or 0
                if isinstance(filesize, (int, float)) and filesize > 0:
                    audio_formats[format_id] = {
                        'size': filesize,
                        'abr': f.get('abr', 0),
                        'acodec': f.get('acodec', '')
                    }

        best_audio_size = 0
        best_audio_format = None
        if audio_formats:
            best_audio_format = max(audio_formats.items(),
                                    key=lambda x: x[1]['abr'] if x[1]['abr'] else 0)
            best_audio_size = best_audio_format[1]['size']
            logger.info(f"Лучший аудиоформат: {best_audio_format[1]['acodec']} {best_audio_format[1]['abr']}kbps, размер: {best_audio_size/1024/1024:.1f}MB")

        # Видео
        for f in formats:
            height = f.get('height')
            vcodec = f.get('vcodec')
            acodec = f.get('acodec')

            if height and vcodec != 'none':
                available_qualities.add(height)
                if height not in quality_info:
                    quality_info[height] = {
                        'video_only_size': 0,
                        'combined_size': 0,
                        'estimated_total': 0,
                        'has_combined_format': False
                    }

                filesize = f.get('filesize') or f.get('filesize_approx') or 0
                if not isinstance(filesize, (int, float)):
                    filesize = 0

                if acodec != 'none':
                    quality_info[height]['has_combined_format'] = True
                    current_combined = quality_info[height]['combined_size'] or 0
                    quality_info[height]['combined_size'] = max(current_combined, filesize)
                    quality_info[height]['estimated_total'] = quality_info[height]['combined_size']
                else:
                    current_video = quality_info[height]['video_only_size'] or 0
                    quality_info[height]['video_only_size'] = max(current_video, filesize)
                    quality_info[height]['estimated_total'] = quality_info[height]['video_only_size'] + best_audio_size

        for height in quality_info:
            info_data = quality_info[height]

            if info_data['has_combined_format'] and info_data['combined_size'] > 0:
                info_data['final_size'] = info_data['combined_size']
                info_data['size_type'] = 'combined'
            elif info_data['video_only_size'] > 0:
                info_data['final_size'] = info_data['video_only_size'] + best_audio_size
                info_data['size_type'] = 'video+audio'
            else:
                info_data['final_size'] = 0
                info_data['size_type'] = 'unknown'

        if not available_qualities:
            await status_message.edit_text(
                f"⚠️ **Анализ завершен, но форматы не найдены**\n\n"
                f"📹 {title}\n"
                f"⏱ Длительность: {format_duration(duration)}\n"
                f"🔍 Анализ: {successful_strategy}\n\n"
                f"💡 Видео можно скачать стандартными командами:\n"
                f"/normal - обычное качество\n"
                f"/audio - только звук\n"
                f"/compress - сжатое видео"
            )
            await state.clear()
            return

        result_text = f"📹 **{title}**\n"
        result_text += f"⏱ Длительность: {format_duration(duration)}\n"

        if successful_strategy:
            result_text += f"🔍 Анализ выполнен: {successful_strategy}\n"

        if best_audio_size > 0:
            result_text += f"🎵 Аудио: {best_audio_size/1024/1024:.1f} MB"
            if best_audio_format:
                result_text += f" ({best_audio_format[1]['acodec']})\n"
            else:
                result_text += "\n"

        result_text += "\n📊 **Доступные качества:**\n\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[])

        for height in sorted(available_qualities, reverse=True):
            info_data = quality_info.get(height, {})

            final_size = info_data.get('final_size', 0)
            size_type = info_data.get('size_type', 'unknown')
            size_mb = max(final_size, 0) / 1024 / 1024

            if size_mb <= 50:
                size_emoji = "✅"
                status = "Поместится целиком"
            elif size_mb <= 150:
                size_emoji = "⚠️"
                status = "Нужно разделение"
            else:
                size_emoji = "❌"
                status = "Большой файл"

            if size_mb > 0:
                size_info = f"~{size_mb:.1f} MB"
                if size_type == 'video+audio':
                    size_info += " (видео+аудио)"
                elif size_type == 'combined':
                    size_info += " (готовый файл)"
                result_text += f"{size_emoji} **{height}p** - {size_info} ({status})\n"
            else:
                result_text += f"📹 **{height}p** - размер неизвестен\n"

            button_text = f"{height}p"
            if size_mb <= 50 and size_mb > 0:
                button_text += " ✅"
            elif size_mb <= 150 and size_mb > 0:
                button_text += " ⚠️"
            elif size_mb > 150:
                button_text += " ❌"

            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"quality_{height}_{max(int(size_mb), 0)}"
                )
            ])

        special_buttons = []

        audio_button_text = "🎵 Только аудио"
        if best_audio_size > 0:
            audio_mb = best_audio_size / 1024 / 1024
            audio_button_text += f" ({audio_mb:.1f}MB)"

        special_buttons.append(InlineKeyboardButton(text=audio_button_text, callback_data="quality_audio_0"))
        special_buttons.append(InlineKeyboardButton(text="📦 Сжатое", callback_data="quality_compress_0"))
        keyboard.inline_keyboard.append(special_buttons)

        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data="quality_cancel_0")
        ])

        result_text += "\n💡 **Информация:**\n"
        result_text += "• Размеры включают видео + аудиодорожку\n"
        result_text += "• Файлы >50MB будут разделены на части\n"
        result_text += "• Выберите качество кнопкой ниже\n"

        if is_instagram and successful_strategy in ['Firefox', 'Edge', 'Safari']:
            result_text += "• ✅ Использован стабильный браузер\n"
        elif is_instagram and successful_strategy == 'Chrome':
            result_text += "• ⚠️ Chrome может иметь DPAPI проблемы\n"

        total_formats = len(formats)
        video_formats = len([f for f in formats if f.get('vcodec') != 'none'])
        audio_formats_count = len(audio_formats)
        result_text += f"\n🔧 Найдено: {total_formats} форматов ({video_formats} видео, {audio_formats_count} аудио)"

        await status_message.edit_text(result_text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Ошибка при обработке информации о видео: {e}", exc_info=True)
        await status_message.edit_text(
            f"❌ **Ошибка при анализе**\n\n"
            f"📹 {info.get('title', 'Неизвестно') if info else 'Видео'}\n"
            f"⚠️ Не удалось проанализировать форматы\n\n"
            f"💡 **Можете скачать стандартными командами:**\n"
            f"/normal - обычное качество\n"
            f"/audio - только звук\n"
            f"/compress - сжатое видео\n\n"
            f"🔧 Техническая ошибка: {str(e)[:100]}..."
        )
        await state.clear()


@dp.message(Command("quick_check"))
async def quick_check_handler(message: Message):
    """Быстрая проверка видео без использования куки.
    NOTE: исходный квик-чек обработчик имел нерабочий фильтр
    (F.text.contains('quick_check_mode')) — он никогда не срабатывал.
    Здесь принимаем ссылку прямо из этой же команды как параметр или
    из следующего сообщения через тот же state.
    """
    await message.answer(
        "⚡ **Быстрая проверка видео**\n\n"
        "Отправьте ссылку этой командой так:\n"
        "`/quick_check <ссылка>`\n\n"
        "или используйте /check для полного анализа.",
        parse_mode="Markdown"
    )


@dp.message(Command("quick_check"), F.text.regexp(r'/quick_check\s+\S+'))
async def quick_check_with_url(message: Message):
    """Обрабатывает /quick_check <url>"""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return

    url = extract_url(parts[1])
    if not url:
        await message.answer("❌ Не могу найти поддерживаемую ссылку")
        return

    status_message = await message.answer("⚡ Быстрый анализ без куки...")

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        info = await asyncio.to_thread(_run_ydl_extract, ydl_opts, url)

        title = info.get('title', 'Неизвестно')
        duration = info.get('duration', 0) or 0
        uploader = info.get('uploader', 'Неизвестный автор')

        result_text = f"⚡ **Быстрый анализ**\n\n"
        result_text += f"📹 {title}\n"
        result_text += f"👤 {uploader}\n"
        result_text += f"⏱ {format_duration(duration)}\n\n"
        result_text += f"✅ Видео доступно для скачивания\n"
        result_text += f"💡 Используйте обычные команды для скачивания"

        await status_message.edit_text(result_text, parse_mode="Markdown")

    except Exception as e:
        error_msg = str(e).lower()
        result_text = f"❌ **Быстрый анализ не удался**\n\n"

        if "private" in error_msg or "login" in error_msg:
            result_text += f"🔒 Видео приватное или требует входа\n"
            result_text += f"💡 Попробуйте /check с куки браузера"
        else:
            result_text += f"⚠️ Ошибка доступа к видео\n"
            result_text += f"💡 Используйте /check для полного анализа"

        await status_message.edit_text(result_text, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("quality_"))
async def quality_callback_handler(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор качества"""
    await callback.answer()

    parts = callback.data.split("_")
    quality = parts[1]

    if quality == "cancel":
        await callback.message.edit_text("❌ Отменено")
        await state.clear()
        return

    data = await state.get_data()
    video_url = data.get('video_url')

    if not video_url:
        await callback.message.edit_text("❌ Ошибка: URL видео не найден. Используйте /check заново")
        await state.clear()
        return

    if quality == "audio":
        set_user_mode(callback.from_user.id, DownloadMode.AUDIO)
        mode_text = "🎵 Режим: Только аудио"
    elif quality == "compress":
        set_user_mode(callback.from_user.id, DownloadMode.COMPRESS)
        mode_text = "📦 Режим: Сжатое видео"
    else:
        set_user_mode(callback.from_user.id, f"quality_{quality}")
        mode_text = f"🎬 Режим: Качество {quality}p"

    download_start_message = await callback.message.edit_text(
        f"{mode_text}\n\n"
        f"📥 Начинаю скачивание...\n"
        f"URL: {video_url}"
    )

    await state.clear()

    user_mode = get_user_mode(callback.from_user.id)
    position = await get_queue_position()

    status_message = await callback.message.answer(
        f"✅ Добавлено в очередь\n"
        f"Позиция: {position + 1}\n"
        f"Режим: {user_mode}"
    )

    task = {
        'message': callback.message,
        'url': video_url,
        'status_message': status_message,
        'mode': user_mode,
        'timestamp': time.time(),
        'user_id': callback.from_user.id,
        'chat_id': callback.message.chat.id,
        'download_start_message': download_start_message
    }

    await download_queue.put(task)

@dp.message(Command("quality"))
async def quality_mode_handler(message: Message):
    """Позволяет выбрать конкретное качество"""
    await message.answer(
        "🎯 **Выбор качества видео**\n\n"
        "Используйте команды:\n"
        "/q1080 - Full HD (1080p)\n"
        "/q720 - HD (720p)\n"
        "/q480 - SD (480p)\n"
        "/q360 - Низкое (360p)\n\n"
        "После выбора качества отправьте ссылку на видео.\n\n"
        "💡 Если видео в выбранном качестве больше 50 MB,\n"
        "будет выбрано ближайшее меньшее качество.",
        parse_mode="Markdown"
    )

@dp.message(Command(commands=["q1080", "q720", "q480", "q360"]))
async def set_quality_handler(message: Message):
    """Устанавливает конкретное качество"""
    quality = message.text.split()[0].replace('/q', '').strip()
    set_user_mode(message.from_user.id, f"quality_{quality}")

    await message.answer(
        f"🎬 Установлено качество: **{quality}p**\n\n"
        f"Теперь отправьте ссылку на видео.",
        parse_mode="Markdown"
    )

# Команда для тестирования куки

@dp.message(Command("test_cookies"))
async def test_modern_cookies_simple(message: Message):
    """Простой тест современных методов куки"""
    status_message = await message.answer("🧪 Тестирую доступ к Instagram через браузеры...")

    test_url = "https://www.instagram.com/reel/DHnsGc9scLs/"
    browsers = ['chrome', 'firefox', 'edge', 'safari', 'brave']

    results = []

    for browser in browsers:
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'cookiesfrombrowser': (browser,),
            }

            info = await asyncio.to_thread(_run_ydl_extract, ydl_opts, test_url)
            title = info.get('title', 'Неизвестно')[:30]
            results.append(f"✅ {browser.title()}: {title}")

        except Exception as e:
            error_msg = str(e).lower()
            if "no such file" in error_msg or "cannot find" in error_msg:
                results.append(f"❌ {browser.title()}: Не установлен")
            elif "locked" in error_msg or "database" in error_msg:
                results.append(f"🔒 {browser.title()}: Закройте браузер")
            else:
                results.append(f"⚠️ {browser.title()}: Ошибка")

    result_text = "🧪 **Тест куки Instagram:**\n\n" + "\n".join(results)

    working = len([r for r in results if "✅" in r])
    if working > 0:
        result_text += f"\n\n🎉 Найдено {working} рабочих браузеров!"
    else:
        result_text += "\n\n💡 Установите Chrome/Firefox и войдите в Instagram"

    await status_message.edit_text(result_text, parse_mode="Markdown")

@dp.message(Command("cookies"))
async def cookies_help_modern(message: Message):
    """Современная справка по куки"""
    await message.answer(
        "🍪 **Современная работа с куки (2025)**\n\n"
        "**✨ Автоматически поддерживается:**\n"
        "• Chrome\n"
        "• Firefox\n"
        "• Edge\n"
        "• Safari (macOS)\n"
        "• Brave\n\n"
        "**🔧 Как пользоваться:**\n"
        "1. Войдите в Instagram через браузер\n"
        "2. Отправьте ссылку боту\n"
        "3. Куки извлекутся автоматически!\n\n"
        "**⚠️ Важно:**\n"
        "• Закрывайте браузер при скачивании\n"
        "• Куки действуют ограниченное время\n"
        "• Для приватного контента нужна подписка\n\n"
        "**🧪 Диагностика:**\n"
        "/test_cookies - проверить браузеры\n\n"
        "**🚫 Больше НЕ нужно:**\n"
        "• Создавать файлы cookies.txt\n"
        "• Экспортировать куки вручную\n"
        "• Устанавливать расширения",
        parse_mode="Markdown"
    )

@dp.message(Command("instagram"))
async def instagram_help_modern(message: Message):
    """Обновленная помощь по Instagram"""
    await message.answer(
        "📸 **Instagram - Автоматические куки (2025)**\n\n"
        "**📱 Поддерживаемые форматы:**\n"
        "• Посты: instagram.com/p/ABC123/\n"
        "• Рилсы: instagram.com/reel/ABC123/\n"
        "• IGTV: instagram.com/tv/ABC123/\n\n"
        "**🍪 Новый подход к куки:**\n"
        "• Автоматическое извлечение из браузера\n"
        "• Поддержка всех популярных браузеров\n"
        "• Никаких ручных настроек!\n\n"
        "**✅ Простые шаги:**\n"
        "1. Откройте instagram.com в браузере\n"
        "2. Войдите в свой аккаунт\n"
        "3. Отправьте ссылку боту\n"
        "4. Готово!\n\n"
        "**🔧 Если не работает:**\n"
        "• Закройте все окна браузера\n"
        "• Попробуйте другой браузер\n"
        "• Используйте /test_cookies\n"
        "• Проверьте, что видео публичное\n\n"
        "**💡 Режимы для Instagram:**\n"
        "/normal - обычное качество\n"
        "/audio - только звук\n"
        "/compress - сжатое видео",
        parse_mode="Markdown"
    )

@dp.message(Command("debug_url"))
async def debug_url_handler(message: Message, state: FSMContext):
    """Отладка распознавания URL"""
    await state.set_state(DownloadStates.waiting_for_check_url)
    await message.answer(
        "🔧 **Отладка URL**\n\n"
        "Отправьте ссылку, и я покажу:\n"
        "• Как я её распознаю\n"
        "• Какая платформа определена\n"
        "• Нормализованную версию\n\n"
        "👉 Отправьте любую ссылку:"
    )

@dp.message(Command("stats"))
async def stats_handler(message: Message):
    """Показывает статистику бота"""
    queue_size = download_queue.qsize()
    active_count = len(active_downloads)

    await message.answer(
        f"📊 **Статистика бота**\n\n"
        f"🏃‍♂️ Активных загрузок: {active_count}\n"
        f"⏳ В очереди: {queue_size}\n"
        f"⚡ Макс. одновременно: {max_concurrent_downloads}\n\n"
        f"**Ваш режим:** {get_user_mode(message.from_user.id)}\n\n"
        f"💡 Используйте /check для анализа перед скачиванием",
        parse_mode="Markdown"
    )

@dp.message()
async def download_handler(message: Message, state: FSMContext):
    """Обработчик всех сообщений - ищет YouTube и Instagram ссылки"""
    if not message.text:
        return

    if message.text.startswith('/'):
        return

    current_state = await state.get_state()
    if current_state:
        return

    url = extract_url(message.text)

    if not url:
        await message.answer(
            "💡 Сначала выберите режим:\n"
            "/normal - обычное видео\n"
            "/audio - только звук\n"
            "/compress - сжатое видео\n"
            "/split - для больших файлов\n\n"
            "Затем отправьте ссылку на YouTube или Instagram видео.\n\n"
            "🔍 Используйте /check для анализа видео перед скачиванием"
        )
        return

    user_mode = get_user_mode(message.from_user.id)
    position = await get_queue_position()

    mode_emoji = {
        DownloadMode.AUDIO: "🎵",
        DownloadMode.COMPRESS: "📦",
        DownloadMode.SPLIT: "✂️",
        DownloadMode.NORMAL: "📹"
    }

    platform = "Instagram" if 'instagram.com' in url else "YouTube"

    status_message = await message.answer(
        f"{mode_emoji.get(user_mode, '📹')} Добавлено в очередь\n"
        f"Платформа: {platform}\n"
        f"Позиция: {position + 1}\n"
        f"Режим: {user_mode}"
    )

    task = {
        'message': message,
        'url': url,
        'status_message': status_message,
        'mode': user_mode,
        'timestamp': time.time(),
        'user_id': message.from_user.id,
        'chat_id': message.chat.id,
    }

    await download_queue.put(task)

async def main():
    """Основная функция запуска бота"""
    logger.info("Запуск бота...")

    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(process_download_queue())

    print("=" * 50)
    print("🤖 Бот запущен!")
    print("=" * 50)
    print("Режимы работы:")
    print("  /normal - обычное видео")
    print("  /audio - только аудио")
    print("  /compress - сжатое видео")
    print("  /split - с разделением")
    print("=" * 50)
    print("Instagram поддержка:")
    print("  • Автоматические куки из браузера")
    print("  • Поддержка постов, рилсов, IGTV")
    print("  • Команда /test_cookies для диагностики")
    print("=" * 50)

    logger.info("Бот успешно запущен с поддержкой современных куки")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем")
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        input("Нажмите Enter для выхода...")
