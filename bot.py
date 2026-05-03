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
        # ВИДЕО — буква в букву из исходного кода (NORMAL):
        format_opts = {
            'format': (
                'best[filesize<50M][ext=mp4]/'
                'bestvideo[filesize<50M][ext=mp4]+bestaudio[ext=m4a]/'
                'best[ext=mp4]/best'
            ),
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
        logger.info("Режим: VIDEO (как в исходном коде)")

    # Стратегии куки
    if is_instagram:
        strategies = [
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
        strategies = [
            {},
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
        ]

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


# ============================================================================
# Обработка задач из очереди
# ============================================================================
async def cleanup_file(filepath):
    try:
        if filepath and filepath.exists():
            filepath.unlink()
    except Exception as e:
        logger.warning(f"cleanup: {e}")


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
        logger.info(f"Загрузка {url} mode={mode} user={user_id}")
        await _safe_edit(status_message, "📥 Запрашиваю файл...")

        loop = asyncio.get_running_loop()
        progress_hook = _make_progress_hook(loop, status_message)

        filepath, video_info = await download_video(url, key, mode, progress_hook)

        if not filepath:
            await _safe_edit(status_message, "❌ Не удалось скачать. Проверьте ссылку.")
            return

        file_size = filepath.stat().st_size
        file_size_mb = file_size / 1024 / 1024
        logger.info(f"Скачан {filepath.name} ({file_size_mb:.1f} МБ)")

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
            try:
                await message.answer_video(
                    types.FSInputFile(filepath),
                    caption=caption,
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
    if is_instagram:
        strategies = [
            {'cookiesfrombrowser': ('chrome',)},
            {'cookiesfrombrowser': ('firefox',)},
            {'cookiesfrombrowser': ('edge',)},
            {'cookiesfrombrowser': ('safari',)},
            {},
        ]
    else:
        strategies = [{}]

    for strat in strategies:
        try:
            opts = {'quiet': True, 'no_warnings': True, **strat}
            info = await asyncio.to_thread(_ydl_extract, opts, url)
            break
        except Exception as e:
            logger.warning(f"Анализ: {str(e)[:100]}")
            continue

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
        [InlineKeyboardButton(text="🎬 Видео", callback_data="dl_video")],
        [InlineKeyboardButton(text="🎵 MP3 (только звук)", callback_data="dl_audio")],
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

    mode = 'audio' if action == 'audio' else 'video'
    label = "🎵 MP3" if mode == 'audio' else "🎬 Видео"

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
