"""
Простой бот для скачивания YouTube/Instagram.

Две кнопки на выбор: 🎬 Видео и 🎵 MP3.
Видео — точно та же схема, что в исходном коде (которая работала):
формат селектор с filesize<50M и H.265 постпроцессор.
"""

import asyncio
import os
import re
import sys
import time
import shutil
import tempfile
import logging
from pathlib import Path
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import yt_dlp

# ============================================================================
# Логирование
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(
            f'bot_{datetime.now().strftime("%Y%m%d")}.log',
            encoding='utf-8',
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================================
# Конфигурация
# ============================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в окружении")

LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").strip()

if LOCAL_BOT_API_URL:
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    _api_server = TelegramAPIServer.from_base(LOCAL_BOT_API_URL, is_local=True)
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(api=_api_server))
    logger.info(f"Bot API: локальный сервер {LOCAL_BOT_API_URL}")
else:
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot API: официальный")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ============================================================================
# Состояния
# ============================================================================
class DownloadStates(StatesGroup):
    waiting_for_check_url = State()


# ============================================================================
# Папки и очередь
# ============================================================================
DOWNLOAD_PATH = Path("downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

download_queue = asyncio.Queue()
active_downloads = {}
MAX_CONCURRENT_DOWNLOADS = 2


# ============================================================================
# Whisper для транскрипции (лениво, при первом использовании)
# ============================================================================
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small").strip()
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info(f"Загружаю Whisper '{WHISPER_MODEL_SIZE}' (первый раз — может качать модель)...")
        _whisper_model = WhisperModel(
            WHISPER_MODEL_SIZE, device="cpu", compute_type="int8",
        )
        logger.info(f"Whisper '{WHISPER_MODEL_SIZE}' загружен")
    return _whisper_model


def _transcribe_audio(audio_path, progress_cb=None):
    """Возвращает (текст_с_таймкодами, язык, длительность)."""
    model = _get_whisper_model()
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    lines = []
    for seg in segments_iter:
        ts_min, ts_sec = int(seg.start) // 60, int(seg.start) % 60
        lines.append(f"[{ts_min:02d}:{ts_sec:02d}]  {seg.text.strip()}")
        if progress_cb:
            try: progress_cb(seg.end, info.duration)
            except Exception: pass
    return "\n".join(lines), info.language, info.duration


# ============================================================================
# Утилиты
# ============================================================================
def format_duration(duration):
    if not duration:
        return "0:00"
    try:
        d = int(float(duration))
        return f"{d // 60}:{d % 60:02d}"
    except (ValueError, TypeError):
        return "0:00"


def extract_url(text):
    if not text:
        return None
    youtube_regex = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'
    m = re.search(youtube_regex, text)
    if m:
        return f'https://www.youtube.com/watch?v={m.group(6)}'

    instagram_patterns = [
        r'(https?://)?(www\.)?(instagram\.com)/(p|reel|reels|tv)/([a-zA-Z0-9_-]+)(/)?(\?.*)?',
        r'(https?://)?(www\.)?(instagram\.com)/stories/([^/]+)/([0-9]+)(/)?(\?.*)?',
        r'(https?://)?(www\.)?(instagram\.com)/tv/([a-zA-Z0-9_-]+)(/)?(\?.*)?',
    ]
    for pattern in instagram_patterns:
        m = re.search(pattern, text)
        if m:
            scheme = m.group(1) or 'https://'
            domain = m.group(3)
            if 'stories' in pattern:
                return f"{scheme}{domain}/stories/{m.group(4)}/{m.group(5)}/"
            return f"{scheme}{domain}/{m.group(4)}/{m.group(5)}/"
    return None


def _sanitize_filename(name, default="output", max_len=80):
    """Чистит строку для использования как имя файла. Кириллица сохраняется."""
    if not name:
        return default
    cleaned = re.sub(r'[\\/:*?"<>|]+', '_', name)
    cleaned = re.sub(r'[\s_]+', '_', cleaned).strip('._-')[:max_len]
    return cleaned or default


def _format_video_info(info):
    """Форматирует метаданные ролика человекочитаемо."""
    title = info.get('title', 'Без названия')
    uploader = info.get('uploader', '') or info.get('channel', '')
    upload_date = info.get('upload_date', '')
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[6:8]}.{upload_date[4:6]}.{upload_date[:4]}"
    duration = info.get('duration', 0) or 0
    view_count = info.get('view_count')
    like_count = info.get('like_count')
    description = info.get('description', '') or ''
    tags = info.get('tags', []) or []
    chapters = info.get('chapters', []) or []

    parts = [f"🎬 {title}"]
    if uploader:    parts.append(f"👤 {uploader}")
    if upload_date: parts.append(f"📅 {upload_date}")
    if duration:    parts.append(f"⏱ {format_duration(duration)}")
    if view_count:  parts.append(f"👁 {view_count:,} просмотров".replace(",", " "))
    if like_count:  parts.append(f"👍 {like_count:,}".replace(",", " "))
    if tags:        parts.append(f"🏷 {', '.join(tags[:10])}")
    if chapters:
        parts.append("\n📑 Главы:")
        for ch in chapters[:30]:
            start = ch.get('start_time', 0) or 0
            parts.append(f"  {int(start)//60:02d}:{int(start)%60:02d}  {ch.get('title', '')}")
    if description:
        parts.append(f"\n📝 Описание:\n{description}")
    return "\n".join(parts)


# ============================================================================
# Прогресс хук — обновляет статусное сообщение в Telegram во время скачивания
# ============================================================================
async def _safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


def _make_progress_hook(loop, status_message):
    """yt-dlp progress hook (вызывается из рабочего потока). Шедулит правки
    статусного сообщения на основной event loop. Троттлим до 1.5 сек."""
    last_update = [0.0]

    def hook(d):
        try:
            now = time.time()
            status = d.get('status')
            if status == 'downloading':
                if now - last_update[0] < 1.5:
                    return
                last_update[0] = now

                downloaded = d.get('downloaded_bytes', 0) or 0
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                speed = d.get('speed') or 0
                eta = d.get('eta') or 0

                if total > 0:
                    pct = downloaded * 100 / total
                    bar = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))
                    progress = f"[{bar}] {pct:.0f}%"
                else:
                    progress = "идёт скачивание..."

                speed_mb = (speed / 1024 / 1024) if speed else 0
                if eta:
                    eta_s = f"{eta // 60}:{eta % 60:02d}"
                else:
                    eta_s = "?"
                downloaded_mb = downloaded / 1024 / 1024
                total_mb = (total / 1024 / 1024) if total else 0

                text = (
                    f"📥 Скачиваю с YouTube/Instagram\n\n"
                    f"{progress}\n"
                    f"💾 {downloaded_mb:.1f} / {total_mb:.1f} МБ\n"
                    f"⚡ {speed_mb:.2f} МБ/с\n"
                    f"⏱ Осталось: ~{eta_s}"
                )
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status_message, text), loop,
                )
            elif status == 'finished':
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status_message, "✅ Скачано, обрабатываю..."), loop,
                )
            elif status == 'started':
                # Постпроцессор стартанул (конвертирование)
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status_message, "🔧 Конвертирую..."), loop,
                )
        except Exception as e:
            logger.warning(f"progress_hook error: {e}")
    return hook


async def _upload_progress_loop(status_message, file_size_mb, stop_event, kind="видео"):
    """Фоновый таск с таймером во время отправки в Telegram."""
    start = time.time()
    spinner = ['⏳', '⌛']
    i = 0
    while not stop_event.is_set():
        elapsed = time.time() - start
        if elapsed < 60:
            elapsed_s = f"{int(elapsed)} сек"
        else:
            elapsed_s = f"{int(elapsed)//60} мин {int(elapsed)%60} сек"
        text = (
            f"📤 Загружаю {kind} в Telegram\n\n"
            f"💾 Размер: {file_size_mb:.1f} МБ\n"
            f"⏱ Прошло: {elapsed_s}\n"
            f"{spinner[i % 2]} Подождите..."
        )
        await _safe_edit(status_message, text)
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


# ============================================================================
# yt-dlp синхронные обёртки (запускаем через asyncio.to_thread)
# ============================================================================
def _ydl_extract(opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _ydl_download(opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


# ============================================================================
# Защита cookies.txt от перезаписи. yt-dlp пишет в cookiefile после каждого
# запроса (обновляя сессионные токены), и в итоге портит файл — следующие
# запросы получают 403. Решение: на каждый вызов yt-dlp копируем оригинал
# (который замаунтан read-only) в одноразовую копию в /tmp.
# ============================================================================
COOKIES_SRC = Path("cookies.txt")


def _make_temp_cookies():
    """Если есть cookies.txt — копирует его в /tmp/...txt и возвращает путь.
    Иначе возвращает None. Каждый вызов даёт НОВЫЙ файл — не портит оригинал."""
    if not COOKIES_SRC.exists():
        return None
    fd, tmp_path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
    os.close(fd)
    try:
        shutil.copy2(COOKIES_SRC, tmp_path)
        return tmp_path
    except Exception as e:
        logger.warning(f"Не удалось скопировать cookies в /tmp: {e}")
        try: os.unlink(tmp_path)
        except Exception: pass
        return None


def _cleanup_temp_cookies(path):
    """Удаляет временную копию cookies после использования."""
    if path:
        try: os.unlink(path)
        except Exception: pass


# ============================================================================
# Скачивание — точно как в рабочем оригинале
# ============================================================================
async def download_video(url, message_id, mode, progress_hook=None):
    """mode = 'video' | 'audio'

    Для видео — точные настройки из исходного кода: filesize<50M фильтр
    + FFmpegVideoConvertor + H.265 постпроцессор. Так оно работало.
    """
    is_instagram = 'instagram.com' in url

    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'outtmpl': f'{DOWNLOAD_PATH}/{message_id}_%(title)s.%(ext)s',
        'socket_timeout': 60,
        'retries': 3,
        # Обход YouTube бот-детекции:
        # 1) player_client — несколько клиентов на пробу
        # 2) bgutil-pot плагин решает n-sig и PO Token (без него YouTube
        #    отдаёт только превью-картинки, а не реальные форматы)
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'web'],
            },
            'youtubepot-bgutilhttp': {
                'base_url': ['http://bgutil-pot:4416'],
            },
        },
    }
    if progress_hook:
        base_opts['progress_hooks'] = [progress_hook]
        base_opts['postprocessor_hooks'] = [progress_hook]

    if mode == 'audio':
        # MP3 как в исходном коде
        format_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        logger.info("Режим: AUDIO (MP3)")
    else:
        # ВИДЕО:
        #   • format — отбор кандидатов: всё видео ≤1080p + любое аудио
        #   • format_sort — КЛЮЧЕВОЕ: сортирует кандидатов по приоритетам.
        #     1) есть видео-поток    2) высота 1080  3) предпочтение H.264
        #     4) ВЫСОКИЙ БИТРЕЙТ     5) высокий fps
        #     Без этого yt-dlp может взять самый дешёвый по битрейту 1080p
        #     H.264 (1.2 Мбит/с — выглядит как 720p растянутый), что и было.
        #   • Без постпроцессора — никакого re-encode, скачиваем как есть.
        format_opts = {
            'format': (
                'bv*[height<=1080]+ba[ext=m4a]/'
                'bv*[height<=1080]+ba/'
                'best[height<=1080]/best'
            ),
            'format_sort': ['hasvid', 'res:1080', 'codec:h264:m4a', 'tbr', 'fps'],
            'merge_output_format': 'mp4',
        }
        logger.info("Режим: VIDEO (1080p, лучший битрейт, без перекодирования)")

    # Стратегии куки. Используем _make_temp_cookies — копию оригинала в /tmp,
    # чтобы yt-dlp не портил оригинал перезаписями (это и было причиной 403).
    cookies_tmp = _make_temp_cookies()
    base_strategies = []
    if cookies_tmp:
        base_strategies.append({'cookiefile': cookies_tmp})
        logger.info("cookies.txt подготовлен (одноразовая копия)")

    if is_instagram:
        strategies = base_strategies + [
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
            {'cookiesfrombrowser': ('edge',)},
            {'cookiesfrombrowser': ('safari',)},
            {'cookiesfrombrowser': ('brave',)},
            {'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                           'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
                           'Mobile/15E148 Safari/604.1'},
        ]
    else:
        strategies = base_strategies + [
            {},
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
        ]

    try:
        for i, strategy in enumerate(strategies, 1):
            try:
                opts = {**base_opts, **format_opts, **strategy}
                info = await asyncio.to_thread(_ydl_download, opts, url)
                for f in DOWNLOAD_PATH.glob(f'{message_id}_*'):
                    if f.is_file():
                        return f, info
            except Exception as e:
                logger.warning(f"Стратегия {i}: {str(e)[:120]}")
                continue
        return None, None
    finally:
        _cleanup_temp_cookies(cookies_tmp)


# ============================================================================
# Обработка задач из очереди
# ============================================================================
async def cleanup_file(filepath):
    try:
        if filepath and filepath.exists():
            filepath.unlink()
    except Exception as e:
        logger.warning(f"cleanup: {e}")


async def _send_long_text(message, text, filename_stem="output"):
    """Отправляет текст: если ≤4000 символов — текстом, иначе .txt файлом."""
    TG_LIMIT = 4000
    if len(text) <= TG_LIMIT:
        await message.answer(text, disable_web_page_preview=True)
        return
    txt_path = DOWNLOAD_PATH / f"{filename_stem}.txt"
    txt_path.write_text(text, encoding='utf-8')
    try:
        await message.answer_document(
            types.FSInputFile(txt_path),
            caption=f"📄 Длинный текст ({len(text)} симв.) — кладу файлом",
        )
    finally:
        try: txt_path.unlink()
        except Exception: pass


async def process_download_queue():
    while True:
        try:
            while len(active_downloads) >= MAX_CONCURRENT_DOWNLOADS:
                await asyncio.sleep(1)
            task = await download_queue.get()
            active_downloads[task['key']] = task
            asyncio.create_task(process_download(task))
        except Exception as e:
            logger.error(f"Очередь: {e}", exc_info=True)


async def process_download(task):
    message = task['message']
    url = task['url']
    status_message = task['status_message']
    mode = task['mode']  # 'video' | 'audio'
    download_start_message = task.get('download_start_message')
    user_id = task.get('user_id')
    key = task['key']

    filepath = None
    try:
        logger.info(f"Задача {url} mode={mode} user={user_id}")

        # === ОПИСАНИЕ — без скачивания, только метаданные ===
        if mode == 'info':
            await _safe_edit(status_message, "🔍 Получаю описание и метаданные...")
            info = None
            for strat in ([{}] if 'instagram.com' not in url else
                          [{'cookiesfrombrowser': ('chrome',)},
                           {'cookiesfrombrowser': ('firefox',)}, {}]):
                try:
                    opts = {'quiet': True, 'no_warnings': True, **strat}
                    info = await asyncio.to_thread(_ydl_extract, opts, url)
                    break
                except Exception as e:
                    logger.warning(f"info extract: {str(e)[:100]}")
                    continue
            if not info:
                await _safe_edit(status_message, "❌ Не удалось получить описание.")
                return
            text_out = _format_video_info(info)
            safe_name = _sanitize_filename(info.get('title', 'info'), default="info") + "_info"
            await _send_long_text(message, text_out, filename_stem=safe_name)
            try: await status_message.delete()
            except Exception: pass
            if download_start_message:
                try: await download_start_message.delete()
                except Exception: pass
            return

        await _safe_edit(status_message, "📥 Запрашиваю файл...")

        loop = asyncio.get_running_loop()
        progress_hook = _make_progress_hook(loop, status_message)

        # Для транскрипции скачиваем как 'audio' — формат не важен для Whisper
        download_mode = 'audio' if mode == 'transcribe' else mode

        filepath, video_info = await download_video(url, key, download_mode, progress_hook)

        if not filepath:
            await _safe_edit(status_message, "❌ Не удалось скачать. Проверьте ссылку.")
            return

        file_size = filepath.stat().st_size
        file_size_mb = file_size / 1024 / 1024
        logger.info(f"Скачан {filepath.name} ({file_size_mb:.1f} МБ)")

        # Что именно подобрал yt-dlp (для отладки качества)
        if video_info:
            fmt = video_info.get('format', '?')
            fmt_id = video_info.get('format_id', '?')
            vcodec = video_info.get('vcodec', '?')
            vbr = video_info.get('vbr')
            tbr = video_info.get('tbr')
            br_str = ""
            if vbr: br_str = f" vbr={vbr:.0f}k"
            elif tbr: br_str = f" tbr={tbr:.0f}k"
            logger.info(f"🎯 yt-dlp выбрал: id={fmt_id} vcodec={vcodec}{br_str}  ({fmt})")

        # Диагностика + парсинг реальных размеров для answer_video.
        # Без width/height/duration Telegram сам гадает aspect ratio
        # и часто промахивается → видео в плеере выглядит «растянутым».
        probed_width, probed_height, probed_duration = None, None, None
        try:
            import subprocess
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=codec_name,width,height,bit_rate:format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=0', str(filepath)],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0:
                info_str = ' '.join(probe.stdout.split())
                logger.info(f"📐 Параметры файла: {info_str}")
                # Парсим в отдельные переменные
                for line in probe.stdout.splitlines():
                    if line.startswith('width='):
                        try: probed_width = int(line.split('=', 1)[1])
                        except ValueError: pass
                    elif line.startswith('height='):
                        try: probed_height = int(line.split('=', 1)[1])
                        except ValueError: pass
                    elif line.startswith('duration='):
                        try: probed_duration = int(float(line.split('=', 1)[1]))
                        except ValueError: pass
        except Exception as e:
            logger.warning(f"ffprobe diagnostic skipped: {e}")

        # === ТРАНСКРИПЦИЯ ===
        if mode == 'transcribe':
            await _safe_edit(
                status_message,
                f"🧠 Запускаю Whisper ({WHISPER_MODEL_SIZE})...\n"
                f"💾 Аудио {file_size_mb:.1f} МБ\n"
                f"⏳ ~1/2 длительности видео. Прогресс ниже.",
            )
            last_upd = [0.0]
            t0 = time.time()
            def progress_cb(cur, total):
                now = time.time()
                if now - last_upd[0] < 2.0: return
                last_upd[0] = now
                pct = (cur / total * 100) if total else 0
                bar = '█' * min(20, int(pct / 5)) + '░' * (20 - min(20, int(pct / 5)))
                elapsed = now - t0
                eta = elapsed * (100 - pct) / pct if pct > 0 else 0
                eta_s = f"{int(eta)//60}:{int(eta)%60:02d}" if eta else "?"
                txt = (
                    f"🧠 Транскрибирую ({WHISPER_MODEL_SIZE})\n\n"
                    f"[{bar}] {pct:.0f}%\n"
                    f"🎧 {int(cur)//60:02d}:{int(cur)%60:02d} / {int(total)//60:02d}:{int(total)%60:02d}\n"
                    f"⏱ Осталось ~{eta_s}"
                )
                asyncio.run_coroutine_threadsafe(_safe_edit(status_message, txt), loop)
            try:
                text_t, lang, dur = await asyncio.to_thread(_transcribe_audio, filepath, progress_cb)
            except Exception as e:
                logger.error(f"Whisper упал: {e}", exc_info=True)
                await _safe_edit(status_message, f"❌ Транскрипция не удалась: {str(e)[:200]}")
                return
            if not text_t.strip():
                await _safe_edit(status_message, "⚠️ Whisper не услышал речи.")
                return
            title_t = video_info.get('title', 'Транскрипт') if video_info else 'Транскрипт'
            header = f"📝 Транскрипция «{title_t}»\n🌐 Язык: {lang}\n⏱ {format_duration(dur)}\n\n"
            safe_name = _sanitize_filename(title_t, default=f"transcript_{key}")
            await _send_long_text(message, header + text_t, filename_stem=safe_name)
            try: await status_message.delete()
            except Exception: pass
            if download_start_message:
                try: await download_start_message.delete()
                except Exception: pass
            return

        # === MP3 ===
        if mode == 'audio':
            stop_event = asyncio.Event()
            uploader = asyncio.create_task(
                _upload_progress_loop(status_message, file_size_mb, stop_event, kind="MP3")
            )
            try:
                await message.answer_audio(
                    types.FSInputFile(filepath),
                    title=video_info.get('title', 'Аудио') if video_info else 'Аудио',
                    performer=(video_info.get('uploader', 'Неизвестный') if video_info else 'Неизвестный'),
                    duration=int(video_info.get('duration', 0)) if (video_info and video_info.get('duration')) else 0,
                )
            finally:
                stop_event.set()
                await uploader
        else:
            # === Видео ===
            title = video_info.get('title', 'Видео') if video_info else 'Видео'
            uploader_name = video_info.get('uploader', '') if video_info else ''
            duration = (video_info.get('duration', 0) or 0) if video_info else 0

            caption = f"🎬 {title}"
            if uploader_name:
                caption += f"\n👤 {uploader_name}"
            caption += f"\n⏱ {format_duration(duration)}"

            stop_event = asyncio.Event()
            uploader = asyncio.create_task(
                _upload_progress_loop(status_message, file_size_mb, stop_event, kind="видео")
            )
            # Передаём ТОЧНЫЕ width/height/duration (из ffprobe).
            # Без них Telegram сам угадывает aspect ratio и часто
            # промахивается — видео выглядит растянутым или сжатым.
            final_width = probed_width or (video_info.get('width') if video_info else None)
            final_height = probed_height or (video_info.get('height') if video_info else None)
            final_duration = probed_duration or (int(duration) if duration else 0)
            logger.info(f"📤 Отправляю answer_video: width={final_width} height={final_height} duration={final_duration}")
            try:
                await message.answer_video(
                    types.FSInputFile(filepath),
                    caption=caption,
                    width=final_width,
                    height=final_height,
                    duration=final_duration,
                    supports_streaming=True,
                )
            finally:
                stop_event.set()
                await uploader

        # Чистим статусные сообщения
        try: await status_message.delete()
        except Exception: pass
        if download_start_message:
            try: await download_start_message.delete()
            except Exception: pass

        logger.info(f"✅ Отправлено: {filepath.name}")

    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await _safe_edit(status_message, f"❌ Ошибка: {str(e)[:200]}")
    finally:
        if filepath:
            await cleanup_file(filepath)
        if key in active_downloads:
            del active_downloads[key]


# ============================================================================
# Меню — две кнопки
# ============================================================================
async def _show_two_button_menu(message: Message, url: str, state: FSMContext):
    """Показывает меню с двумя кнопками: 🎬 Видео / 🎵 MP3."""
    await state.update_data(video_url=url)

    # Минимальный анализ — только название и длительность, без форматов
    status_message = await message.answer("🔍 Получаю информацию...")

    info = None
    is_instagram = 'instagram.com' in url

    # Одноразовая копия cookies — yt-dlp пишет туда, оригинал не портится
    cookies_tmp = _make_temp_cookies()
    base_strategies = []
    if cookies_tmp:
        base_strategies.append({'cookiefile': cookies_tmp})

    if is_instagram:
        strategies = base_strategies + [
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
            {'cookiesfrombrowser': ('edge',)},
            {'cookiesfrombrowser': ('safari',)},
            {},
        ]
    else:
        strategies = base_strategies + [{}]

    # Тот же extractor_args что в download_video — обход YouTube бот-детекции
    yt_extractor_args = {
        'youtube': {'player_client': ['mweb', 'web']},
        'youtubepot-bgutilhttp': {'base_url': ['http://bgutil-pot:4416']},
    }

    try:
        for strat in strategies:
            try:
                opts = {
                    'quiet': True, 'no_warnings': True,
                    'extractor_args': yt_extractor_args,
                    **strat,
                }
                info = await asyncio.to_thread(_ydl_extract, opts, url)
                break
            except Exception as e:
                logger.warning(f"Анализ: {str(e)[:100]}")
                continue
    finally:
        _cleanup_temp_cookies(cookies_tmp)

    if not info:
        await status_message.edit_text(
            "❌ Не удалось получить информацию.\n"
            "Возможно, видео приватное или нужны куки браузера."
        )
        await state.clear()
        return

    title = info.get('title', 'Видео')
    duration = info.get('duration', 0) or 0
    uploader = info.get('uploader', '')

    text = f"📹 {title}"
    if uploader:
        text += f"\n👤 {uploader}"
    text += f"\n⏱ {format_duration(duration)}\n\nВыберите формат:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Видео", callback_data="dl_video"),
            InlineKeyboardButton(text="🎵 MP3", callback_data="dl_audio"),
        ],
        [
            InlineKeyboardButton(text="📝 Транскрипция", callback_data="dl_transcribe"),
            InlineKeyboardButton(text="📋 Описание", callback_data="dl_info"),
        ],
    ])

    try:
        await status_message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"меню: {e}")


# ============================================================================
# Хендлеры
# ============================================================================
@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет!\n\n"
        "Пришлите ссылку YouTube или Instagram — выберите кнопкой:\n"
        "🎬 Видео или 🎵 MP3.\n\n"
        "Команды:\n"
        "/check — анализ ссылки и меню\n"
        "/stats — состояние очереди"
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "📚 Справка\n\n"
        "Пришлите ссылку YouTube или Instagram → выберите кнопку:\n"
        "🎬 Видео или 🎵 MP3.\n\n"
        "Команды:\n"
        "/check — анализ ссылки и меню\n"
        "/stats — состояние очереди"
    )


@dp.message(Command("check"))
async def check_handler(message: Message, state: FSMContext):
    await state.set_state(DownloadStates.waiting_for_check_url)
    await message.answer("🔍 Пришлите ссылку:")


@dp.message(StateFilter(DownloadStates.waiting_for_check_url))
async def check_url(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Пришлите ссылку текстом")
        return
    url = extract_url(message.text)
    if not url:
        await message.answer("❌ Это не похоже на YouTube/Instagram ссылку")
        return
    await _show_two_button_menu(message, url, state)


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    await message.answer(
        f"📊 Очередь: {download_queue.qsize()}\n"
        f"🏃 Активных: {len(active_downloads)}\n"
        f"⚡ Параллельно: {MAX_CONCURRENT_DOWNLOADS}"
    )


@dp.callback_query(F.data.startswith("dl_"))
async def dl_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    action = callback.data.replace("dl_", "")

    data = await state.get_data()
    video_url = data.get('video_url')
    if not video_url:
        try:
            await callback.message.edit_text("❌ URL не найден. Пришлите ссылку заново.")
        except Exception:
            pass
        await state.clear()
        return

    mode_labels = {
        'video':      ('video',      '🎬 Видео'),
        'audio':      ('audio',      '🎵 MP3'),
        'transcribe': ('transcribe', '📝 Транскрипция'),
        'info':       ('info',       '📋 Описание'),
    }
    mode, label = mode_labels.get(action, ('video', '🎬 Видео'))

    try:
        download_start_message = await callback.message.edit_text(
            f"{label}\n📥 Ставлю в очередь..."
        )
    except Exception:
        download_start_message = None

    await state.clear()

    status_message = await callback.message.answer(
        f"✅ В очереди (позиция {download_queue.qsize() + len(active_downloads) + 1})"
    )

    # Уникальный ключ задачи (на случай если callback.message.message_id повторится)
    task_key = f"{callback.message.chat.id}_{callback.message.message_id}_{int(time.time())}"

    await download_queue.put({
        'message': callback.message,
        'url': video_url,
        'status_message': status_message,
        'mode': mode,
        'timestamp': time.time(),
        'user_id': callback.from_user.id,
        'chat_id': callback.message.chat.id,
        'download_start_message': download_start_message,
        'key': task_key,
    })


@dp.message()
async def fallback(message: Message, state: FSMContext):
    if not message.text or message.text.startswith('/'):
        return
    if await state.get_state():
        return
    url = extract_url(message.text)
    if not url:
        await message.answer("💡 Пришлите ссылку YouTube или Instagram.")
        return
    await _show_two_button_menu(message, url, state)


# ============================================================================
# Запуск
# ============================================================================
async def main():
    logger.info("Запуск бота...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(process_download_queue())
    print("=" * 50)
    print("🤖 Бот запущен")
    print("=" * 50)
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
        if sys.stdin and sys.stdin.isatty():
            try: input("Нажмите Enter для выхода...")
            except EOFError: pass
        sys.exit(1)