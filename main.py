import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
import yt_dlp

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")  # например: https://my-bot.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else None

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN")
if not WEBHOOK_URL:
    raise ValueError("Не найден BASE_URL")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MAX_FILE_SIZE = 49 * 1024 * 1024
URL_REGEX = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
)

user_links = {}


def extract_url(text: str):
    if not text:
        return None
    match = URL_REGEX.search(text)
    return match.group(1) if match else None


def is_allowed_url(url: str) -> bool:
    return any(domain in url.lower() for domain in ALLOWED_DOMAINS)


def get_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Скачать видео", callback_data="download_video")],
            [InlineKeyboardButton(text="🎵 Скачать аудио", callback_data="download_audio")],
        ]
    )


def find_file_by_extensions(folder: str, extensions: tuple):
    files = []
    for file in Path(folder).iterdir():
        if file.is_file() and file.suffix.lower() in extensions:
            files.append(file)

    if not files:
        return None

    files.sort(key=lambda x: x.stat().st_size, reverse=True)
    return files[0]


def download_video_sync(url: str, download_dir: str, low_quality: bool = False):
    if low_quality:
        video_format = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
    else:
        video_format = "bestvideo+bestaudio/best"

    ydl_opts = {
        "outtmpl": os.path.join(download_dir, "%(title).80s_%(id)s.%(ext)s"),
        "format": video_format,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    video_path = find_file_by_extensions(download_dir, (".mp4", ".mkv", ".webm", ".mov"))
    if not video_path:
        raise FileNotFoundError("Видео не найдено после скачивания")

    return video_path


def download_audio_sync(url: str, download_dir: str):
    ydl_opts = {
        "outtmpl": os.path.join(download_dir, "%(title).80s_%(id)s.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    audio_path = find_file_by_extensions(download_dir, (".mp3", ".m4a", ".webm"))
    if not audio_path:
        raise FileNotFoundError("Аудио не найдено после скачивания")

    return audio_path


async def safe_delete_message(message_obj):
    try:
        await message_obj.delete()
    except Exception:
        pass


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет 👋\n\n"
        "Пришли ссылку на видео с YouTube, Instagram или TikTok.\n"
        "Потом выбери: скачать видео или аудио."
    )


@dp.message(F.text)
async def link_handler(message: Message):
    url = extract_url(message.text)

    if not url:
        await message.answer("Пришли ссылку на YouTube, Instagram или TikTok.")
        return

    if not is_allowed_url(url):
        await message.answer("Поддерживаются только YouTube, Instagram и TikTok.")
        return

    if not message.from_user:
        await message.answer("Не удалось определить пользователя.")
        return

    user_links[message.from_user.id] = url

    await message.answer(
        "Ссылка получена ✅\nВыбери, что скачать:",
        reply_markup=get_action_keyboard()
    )


@dp.callback_query(F.data.in_(["download_video", "download_audio"]))
async def process_download(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return

    user_id = callback.from_user.id
    url = user_links.get(user_id)

    if not url:
        await callback.answer("Сначала отправь ссылку.", show_alert=True)
        return

    await callback.answer()
    wait_msg = await callback.message.answer("Обрабатываю ссылку...")
    temp_dir = tempfile.mkdtemp(prefix="tg_downloader_")

    try:
        if callback.data == "download_audio":
            file_path = await asyncio.to_thread(download_audio_sync, url, temp_dir)

            if file_path.stat().st_size > MAX_FILE_SIZE:
                await wait_msg.edit_text("Аудио слишком большое для отправки ботом.")
                return

            await callback.message.answer_audio(audio=FSInputFile(str(file_path)))
            await callback.message.answer("Готово ✅")
            await safe_delete_message(wait_msg)
            return

        try:
            file_path = await asyncio.to_thread(download_video_sync, url, temp_dir, False)
        except Exception:
            file_path = await asyncio.to_thread(download_video_sync, url, temp_dir, True)

        if file_path.stat().st_size > MAX_FILE_SIZE:
            shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir = tempfile.mkdtemp(prefix="tg_downloader_low_")
            file_path = await asyncio.to_thread(download_video_sync, url, temp_dir, True)

            if file_path.stat().st_size > MAX_FILE_SIZE:
                await wait_msg.edit_text("Видео слишком большое даже в низком качестве.")
                return

        await callback.message.answer_video(
            video=FSInputFile(str(file_path)),
            caption="Готово ✅"
        )
        await safe_delete_message(wait_msg)

    except yt_dlp.utils.DownloadError:
        await wait_msg.edit_text("Не получилось скачать файл по этой ссылке.")
    except Exception as e:
        await wait_msg.edit_text(f"Ошибка: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def handle_webhook(request: web.Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return web.Response(text="ok")


async def healthcheck(request: web.Request):
    return web.Response(text="ok")


async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)


async def on_shutdown(app):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()


def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/", healthcheck)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()