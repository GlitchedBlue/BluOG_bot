import os
import re
import json
import shutil
import asyncio
import tempfile

import requests
import yt_dlp
from telebot.async_telebot import AsyncTeleBot

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = AsyncTeleBot(BOT_TOKEN)

# Optional settings (set these as Railway variables)
YT_COOKIES = os.getenv("YT_COOKIES")      # full text of a cookies.txt (Netscape format)
PROXY = os.getenv("PROXY")                # e.g. http://user:pass@host:port

MAX_BYTES = 50 * 1024 * 1024              # Telegram bot upload limit
URL_REGEX = re.compile(r"https?://[^\s]+")
PLATFORMS = (
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "facebook.com", "fb.watch",
)

# Write cookies to a file once at startup so yt-dlp can use them
COOKIE_FILE = None
if YT_COOKIES:
    COOKIE_FILE = "/tmp/cookies.txt"
    with open(COOKIE_FILE, "w") as f:
        f.write(YT_COOKIES)


def ytdlp_download(url: str, outdir: str) -> str:
    """Blocking download with yt-dlp. Returns the file path. Raises on failure."""
    opts = {
        # Single-file formats only (no ffmpeg needed), small enough for Telegram
        "format": "best[ext=mp4][filesize<48M]/best[ext=mp4][filesize_approx<48M]/best[height<=480][ext=mp4]/best[ext=mp4]/best",
        "outtmpl": os.path.join(outdir, "video.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "max_filesize": MAX_BYTES,
        "retries": 2,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    if PROXY:
        opts["proxy"] = PROXY
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        # extension can differ after post-processing
        for name in os.listdir(outdir):
            return os.path.join(outdir, name)
        raise FileNotFoundError("yt-dlp produced no file")
    return path


def instagram_embed_download(url: str, outdir: str) -> str:
    """Scrape the public embed page for the direct mp4. Raises on failure."""
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p|tv)/([\w-]+)", url)
    if not m:
        raise ValueError("not an instagram post url")
    embed = f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    res = requests.get(embed, headers=headers, timeout=12, proxies=proxies)
    res.raise_for_status()
    vm = re.search(r'"video_url":"([^"]+)"', res.text)
    if not vm:
        raise ValueError("no video_url in embed page")
    direct = json.loads('"' + vm.group(1) + '"')
    path = os.path.join(outdir, "video.mp4")
    with requests.get(direct, headers=headers, stream=True, timeout=20, proxies=proxies) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("file too large")
                f.write(chunk)
    return path


def fetch_video(url: str, outdir: str) -> str:
    low = url.lower()
    errors = []
    if "instagram.com" in low:
        try:
            return instagram_embed_download(url, outdir)
        except Exception as e:
            errors.append(f"embed: {e}")
    try:
        return ytdlp_download(url, outdir)
    except Exception as e:
        errors.append(f"yt-dlp: {e}")
    raise RuntimeError(" | ".join(errors))


@bot.message_handler(commands=["start", "help"])
async def start(message):
    await bot.reply_to(
        message,
        "Send me an Instagram, TikTok or YouTube link and I'll send the video back.",
    )


@bot.message_handler(func=lambda m: bool(m.text))
async def handle_messages(message):
    match = URL_REGEX.search(message.text)
    if not match:
        return
    url = match.group(0)
    if not any(p in url.lower() for p in PLATFORMS):
        return

    await bot.send_chat_action(message.chat.id, "upload_video")
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        try:
            path = await asyncio.to_thread(fetch_video, url, tmp)
        except Exception as e:
            print(f"Download failed for {url}: {e}")
            msg = str(e).lower()
            if "sign in" in msg or "bot" in msg or "cookies" in msg:
                text = "YouTube is asking for login from this server. The bot owner needs to add cookies."
            elif "larger than" in msg or "too large" in msg or "max-filesize" in msg:
                text = "That video is over Telegram's 50 MB bot limit."
            else:
                text = "Couldn't download that video. It may be private or the site blocked the request."
            await bot.reply_to(message, text)
            return

        if os.path.getsize(path) > MAX_BYTES:
            await bot.reply_to(message, "That video is over Telegram's 50 MB bot limit.")
            return

        try:
            with open(path, "rb") as video:
                await bot.send_video(
                    message.chat.id, video,
                    reply_to_message_id=message.message_id,
                    supports_streaming=True,
                )
        except Exception as e:
            print(f"send_video failed, sending as document: {e}")
            with open(path, "rb") as video:
                await bot.send_document(
                    message.chat.id, video, reply_to_message_id=message.message_id
                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    print("Bot is online.")
    await bot.polling(non_stop=True, timeout=90)


if __name__ == "__main__":
    asyncio.run(main())
