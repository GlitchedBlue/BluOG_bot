import os
import re
import sys
import json
import time
import uuid
import shutil
import asyncio
import tempfile
import subprocess

import requests

# YouTube changes constantly, so grab the newest yt-dlp every time the bot starts.
try:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", "--quiet", "yt-dlp[default]"],
        timeout=180, check=False,
    )
except Exception as _e:
    print(f"yt-dlp upgrade skipped: {_e}")

import yt_dlp  # noqa: E402  (imported after the upgrade on purpose)
from telethon import TelegramClient, events, Button  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

# ---- Settings (Railway variables) ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))    # from https://my.telegram.org -> API development tools
API_HASH = os.getenv("API_HASH", "")
YT_COOKIES = os.getenv("YT_COOKIES")      # full text of a cookies.txt (Netscape format), optional
PROXY = os.getenv("PROXY")                # optional, e.g. http://user:pass@host:port

MAX_MB = int(os.getenv("MAX_MB", "300"))                       # biggest file we will send
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "480"))               # default quality cap
COMPRESS_MIN_MINUTES = int(os.getenv("COMPRESS_MIN_MINUTES", "10"))  # only videos at least this long get compressed
COMPRESS_PRESET = os.getenv("COMPRESS_PRESET", "veryfast")     # ultrafast = quicker but bigger files
LONG_SECONDS = int(os.getenv("LONG_SECONDS", "300"))           # YouTube videos longer than this get quality buttons
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))             # downloads at the same time

URL_REGEX = re.compile(r"https?://[^\s]+")
PLATFORMS = (
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "facebook.com", "fb.watch",
)
QUALITY_STEPS = (360, 480, 720, 1080)

# Bundled ffmpeg (lets yt-dlp merge separate video + audio, and compress)
FFMPEG = None
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as _e:
    print(f"ffmpeg not available, falling back to single-file formats: {_e}")

COOKIE_FILE = None
if YT_COOKIES:
    COOKIE_FILE = "/tmp/cookies.txt"
    with open(COOKIE_FILE, "w") as f:
        f.write(YT_COOKIES)

client = TelegramClient(StringSession(), API_ID, API_HASH)
DL_SEM = None            # created in main() so it belongs to the running event loop
pending = {}             # quality-button requests waiting for a click


# ------------------------------------------------------------------ yt-dlp helpers
def base_opts(outdir: str, height=None) -> dict:
    """height: None = default cap, an int = max height, 'audio' = audio only."""
    if height == "audio":
        fmt = "ba[ext=m4a]/ba/b"
    else:
        h = height or MAX_HEIGHT
        if FFMPEG:
            fmt = (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
                   f"bv*[height<={h}]+ba/b[height<={h}]/w")
        else:
            fmt = f"b[height<={h}][ext=mp4]/b[ext=mp4]/b"
    opts = {
        "format": fmt,
        "outtmpl": os.path.join(outdir, "video.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "concurrent_fragment_downloads": 4,
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    if PROXY:
        opts["proxy"] = PROXY
    return opts


def youtube_attempts(outdir: str, height=None):
    """Different ways of asking YouTube, tried in order until one works."""
    attempts = []
    if COOKIE_FILE:
        o = base_opts(outdir, height); o["cookiefile"] = COOKIE_FILE
        attempts.append(("cookies+default", o))
        for c in ("mweb", "web_safari", "tv"):
            o = base_opts(outdir, height); o["cookiefile"] = COOKIE_FILE
            o["extractor_args"] = {"youtube": {"player_client": [c]}}
            attempts.append((f"cookies+{c}", o))
    for c in ("android_vr", "tv_simply", "ios"):
        o = base_opts(outdir, height)
        o["extractor_args"] = {"youtube": {"player_client": [c]}}
        attempts.append((f"nocookies+{c}", o))
    return attempts


def est_size(info: dict):
    s = info.get("filesize") or info.get("filesize_approx")
    if s:
        return s
    parts = [f.get("filesize") or f.get("filesize_approx") for f in (info.get("requested_formats") or [])]
    if parts and all(parts):
        return sum(parts)
    return None


def run_ytdlp(opts: dict, url: str, outdir: str) -> str:
    for name in os.listdir(outdir):
        try:
            os.remove(os.path.join(outdir, name))
        except OSError:
            pass
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        size = est_size(info)
        print(f"yt-dlp picked format={info.get('format_id')} ext={info.get('ext')} "
              f"res={info.get('width')}x{info.get('height')} size={size}")
        if size and size > MAX_BYTES:
            raise ValueError(f"video too large ({size // (1024 * 1024)} MB)")
        done = ydl.process_ie_result(info, download=True)
        path = None
        for d in (done.get("requested_downloads") or []):
            if d.get("filepath"):
                path = d["filepath"]
        if not path:
            path = ydl.prepare_filename(done)
    if path and os.path.exists(path):
        return path
    leftovers = os.listdir(outdir)
    for name in leftovers:
        if not name.endswith((".part", ".ytdl")):
            return os.path.join(outdir, name)
    raise FileNotFoundError(f"yt-dlp finished but no file found (folder has: {leftovers})")


def ytdlp_download(url: str, outdir: str, height=None) -> str:
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        attempts = youtube_attempts(outdir, height)
    else:
        attempts = [("default", base_opts(outdir, height))]
    errors = []
    for label, opts in attempts:
        try:
            print(f"yt-dlp attempt: {label}")
            path = run_ytdlp(opts, url, outdir)
            if os.path.getsize(path) > MAX_BYTES:
                raise ValueError("video too large")
            return path
        except Exception as e:
            text = str(e)
            print(f"yt-dlp attempt '{label}' failed: {text}")
            errors.append(f"[{label}] {text}")
            if "too large" in text.lower():
                raise RuntimeError(f"video too large (over {MAX_MB} MB limit)")
    raise RuntimeError(errors[0] if errors else "yt-dlp failed")


# ------------------------------------------------------------------ quality buttons
def get_youtube_info(url: str):
    """Fetch title/duration/formats without downloading. Returns None if it can't."""
    for label, opts in youtube_attempts("/tmp"):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # process=False skips format selection, so this can't fail on format choice
                info = ydl.extract_info(url, download=False, process=False)
            if info and info.get("duration") is not None:
                return info
        except Exception as e:
            print(f"info attempt '{label}' failed: {e}")
    return None


def _fsize(f: dict, duration: float):
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and duration:
        s = f["tbr"] * 1000 / 8 * duration
    return s


def quality_options(info: dict):
    """List of (button_value, label) the user can pick from."""
    dur = info.get("duration") or 0
    fmts = info.get("formats") or []
    video = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("height")]
    audio = [f for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    best_audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    a_size = (_fsize(best_audio, dur) or 0) if best_audio else 0

    options, seen = [], set()
    for h in QUALITY_STEPS:
        cands = [f for f in video if f["height"] <= h]
        if not cands:
            continue
        actual = max(f["height"] for f in cands)
        if actual in seen:
            continue
        top = [f for f in cands if f["height"] == actual]
        avc = [f for f in top if str(f.get("vcodec", "")).startswith("avc1")]
        pool = avc or top
        sizes = [s for s in (_fsize(f, dur) for f in pool) if s]
        size = (min(sizes) + a_size) if sizes else None
        if size and size > MAX_BYTES and options:
            continue            # too big to send, but always keep the lowest option
        seen.add(actual)
        label = f"{actual}p" + (f"  ~{int(size / 1024 / 1024)} MB" if size else "")
        options.append((h, label))
    if best_audio:
        a_label = "Audio only" + (f"  ~{int(a_size / 1024 / 1024)} MB" if a_size else "")
        options.append(("audio", a_label))
    return options


def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def prune_pending():
    now = time.time()
    for k in [k for k, v in pending.items() if now - v["ts"] > 3600]:
        pending.pop(k, None)


# ------------------------------------------------------------------ other platforms
def tiktok_fallback_download(url: str, outdir: str) -> str:
    """Third-party resolver used when yt-dlp is blocked by TikTok."""
    clean = url.split("?")[0]
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    res = requests.get("https://www.tikwm.com/api/", params={"url": clean, "hd": 1},
                       headers={"User-Agent": "Mozilla/5.0"}, timeout=20, proxies=proxies)
    res.raise_for_status()
    data = res.json().get("data") or {}
    play = data.get("hdplay") or data.get("play")
    if not play:
        raise ValueError(f"fallback returned no video: {res.text[:200]}")
    if play.startswith("/"):
        play = "https://www.tikwm.com" + play
    path = os.path.join(outdir, "video.mp4")
    with requests.get(play, headers={"User-Agent": "Mozilla/5.0"}, stream=True,
                      timeout=30, proxies=proxies) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("video too large")
                f.write(chunk)
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
                    raise ValueError("video too large")
                f.write(chunk)
    return path


def fetch_video(url: str, outdir: str, height=None) -> str:
    low = url.lower()
    errors = []
    if "instagram.com" in low:
        try:
            return instagram_embed_download(url, outdir)
        except Exception as e:
            errors.append(f"embed: {e}")
    if "tiktok.com" in low:
        url = url.split("?")[0]   # drop tracking junk like ?is_from_webapp=1
    try:
        return ytdlp_download(url, outdir, height)
    except Exception as e:
        errors.append(f"yt-dlp: {e}")
        if "too large" in str(e).lower():
            raise RuntimeError(" | ".join(errors))
    if "tiktok.com" in low:
        try:
            print("TikTok: trying fallback resolver")
            return tiktok_fallback_download(url, outdir)
        except Exception as e:
            print(f"TikTok fallback failed: {e}")
            errors.append(f"tiktok-fallback: {e}")
    raise RuntimeError(" | ".join(errors))


# ------------------------------------------------------------------ compression
def video_duration(path: str) -> float:
    """Length of a video file in seconds (0 if unknown)."""
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception as e:
        print(f"Could not read duration: {e}")
    return 0


def maybe_compress(path: str) -> str:
    """Re-encode a video to a smaller, still watchable file. Returns the path to send."""
    size = os.path.getsize(path)
    if not FFMPEG:
        return path
    out = os.path.join(os.path.dirname(path), "compressed.mp4")
    h = MAX_HEIGHT
    vf = f"scale='if(gt(iw,ih),-2,min(iw,{h}))':'if(gt(iw,ih),min(ih,{h}),-2)'"
    cmd = [
        FFMPEG, "-y", "-i", path,
        "-vf", vf,
        "-threads", "0",
        "-c:v", "libx264", "-preset", COMPRESS_PRESET, "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        out,
    ]
    print(f"Compressing {size // (1024 * 1024)} MB video to max {h}p")
    try:
        subprocess.run(cmd, check=True, timeout=900,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        new_size = os.path.getsize(out)
        print(f"Compressed: {size // (1024 * 1024)} MB -> {new_size // (1024 * 1024)} MB")
        if 0 < new_size < size:
            return out
    except Exception as e:
        print(f"Compression failed, sending original: {e}")
    return path


# ------------------------------------------------------------------ bot logic
def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "too large" in msg:
        return f"That video is over my {MAX_MB} MB limit. Try a lower quality."
    if "sign in" in msg or "confirm you" in msg:
        return "YouTube wants a login from this server. The bot owner needs to refresh the cookies."
    if "needs to be reloaded" in msg:
        return "YouTube rejected the request from this server. Try again in a bit."
    return "Couldn't download that video. It may be private or the site blocked the request."


async def say(status, text, **kwargs):
    """Update the status message; never let a failed edit break the download."""
    if status is None:
        return
    try:
        await status.edit(text, **kwargs)
    except Exception as e:
        if "not modified" not in str(e).lower():
            print(f"status edit skipped: {e}")


async def process_and_send(chat_id, reply_to, url, height=None, status=None):
    """Download, (maybe) compress and send, keeping one status message updated.
    The status message is deleted when the video is sent, or turned into an error text."""
    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        queued = DL_SEM.locked()
        if queued:
            await say(status, "Queued, waiting for a free slot...")
        async with DL_SEM:
            async with client.action(chat_id, "audio" if height == "audio" else "video"):
                try:
                    await say(status, "Downloading...")
                    path = await asyncio.to_thread(fetch_video, url, tmp, height)
                    if height is None and FFMPEG:
                        dur = await asyncio.to_thread(video_duration, path)
                        print(f"Downloaded video length: {int(dur)}s")
                        if dur >= COMPRESS_MIN_MINUTES * 60:
                            mb = os.path.getsize(path) // (1024 * 1024)
                            await say(status, f"Compressing ({mb} MB)... this is the slow part.")
                            path = await asyncio.to_thread(maybe_compress, path)
                except Exception as e:
                    print(f"Download failed for {url}: {e}")
                    await say(status, friendly_error(e))
                    return
                try:
                    mb = os.path.getsize(path) // (1024 * 1024)
                    await say(status, f"Uploading ({mb} MB)...")
                    last = [0.0]

                    async def progress(sent, total):
                        now = time.time()
                        if total and now - last[0] > 4:
                            last[0] = now
                            await say(status, f"Uploading... {int(sent * 100 / total)}%")

                    await client.send_file(
                        chat_id, path, reply_to=reply_to,
                        supports_streaming=(height != "audio"),
                        progress_callback=progress,
                    )
                except Exception as e:
                    print(f"Upload failed: {e}")
                    await say(status, "Downloaded the video but Telegram refused the upload.")
                    return
        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@client.on(events.NewMessage(incoming=True, pattern=r"^/(start|help)"))
async def start(event):
    await event.reply("Send me an Instagram, TikTok or YouTube link and I'll send the video back. "
                      "For long YouTube videos you can pick the quality.")


@client.on(events.NewMessage(incoming=True))
async def handle_messages(event):
    text = event.raw_text or ""
    if text.startswith("/"):
        return
    match = URL_REGEX.search(text)
    if not match:
        return
    url = match.group(0)
    low = url.lower()
    if not any(p in low for p in PLATFORMS):
        return

    # Instant feedback: this one message is updated all the way through, then deleted
    status = await event.reply("Preparing your video...")

    # Long YouTube videos: let the user pick the quality first
    if "youtube.com" in low or "youtu.be" in low:
        info = await asyncio.to_thread(get_youtube_info, url)
        if info and (info.get("duration") or 0) > LONG_SECONDS and not info.get("is_live"):
            options = quality_options(info)
            if options:
                prune_pending()
                pid = uuid.uuid4().hex[:8]
                pending[pid] = {
                    "url": url, "chat": event.chat_id, "msg": event.id,
                    "user": event.sender_id, "ts": time.time(),
                    "labels": {str(v): lbl for v, lbl in options},
                }
                buttons = [Button.inline(lbl, data=f"q|{pid}|{v}".encode()) for v, lbl in options]
                rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
                title = (info.get("title") or "this video")[:80]
                await say(
                    status,
                    f"{title}\nLength: {fmt_dur(info.get('duration'))}\n\n"
                    f"Pick a quality (higher = better but slower to send):",
                    buttons=rows,
                )
                return

    await process_and_send(event.chat_id, event.id, url, None, status)


@client.on(events.CallbackQuery(pattern=rb"^q\|"))
async def on_quality(event):
    try:
        _, pid, choice = event.data.decode().split("|", 2)
    except Exception:
        return
    req = pending.get(pid)
    if not req:
        await event.answer("This request expired. Send the link again.", alert=True)
        return
    if event.sender_id != req["user"]:
        await event.answer("Only the person who sent the link can choose.", alert=True)
        return
    pending.pop(pid, None)          # stops double clicks
    height = "audio" if choice == "audio" else int(choice)
    label = req["labels"].get(choice, choice)
    await event.answer()
    status = await event.edit(f"Starting {label}...")
    await process_and_send(req["chat"], req["msg"], req["url"], height, status)


async def main():
    global DL_SEM
    if not (BOT_TOKEN and API_ID and API_HASH):
        print("Missing BOT_TOKEN, API_ID or API_HASH. Set them as Railway variables.")
        return
    DL_SEM = asyncio.Semaphore(MAX_PARALLEL)
    await client.start(bot_token=BOT_TOKEN)
    print(f"Bot is online. yt-dlp {yt_dlp.version.__version__}, cookies: {bool(COOKIE_FILE)}, "
          f"ffmpeg: {bool(FFMPEG)}, limit: {MAX_MB} MB, max height: {MAX_HEIGHT}p, "
          f"compress videos of {COMPRESS_MIN_MINUTES}+ min, buttons for videos over: {LONG_SECONDS}s")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
