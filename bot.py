"""
Telegram bot that converts a YouTube video/Shorts link into an MP3 audio file.

Requires:
  - TELEGRAM_BOT_TOKEN environment variable (get one from @BotFather on Telegram)
  - ffmpeg installed on the system (used by yt-dlp to extract/convert audio)

Run:
  export TELEGRAM_BOT_TOKEN="123456:ABC-YourTokenHere"
  python bot.py
"""

import os
import re
import logging
import asyncio
import threading
import shutil
import subprocess
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    _root = Path(__file__).parent
    load_dotenv(_root / ".env")
    load_dotenv(_root / "secrets.env")
except ImportError:
    pass
from http.server import HTTPServer, BaseHTTPRequestHandler

import yt_dlp
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
keyboard=[["🎵 Convert Audio"],["❓ Help","ℹ️ About"]]
MAIN_KEYBOARD=ReplyKeyboardMarkup(keyboard,resize_keyboard=True,is_persistent=True)



def _get_bot_token() -> Optional[str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()

    try:
        from dotenv import load_dotenv
        root = Path(__file__).parent
        load_dotenv(root / ".env")
        load_dotenv(root / "secrets.env")
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if token:
            return token.strip()
    except ImportError:
        pass

    for name in (".env", "secrets.env"):
        path = Path(__file__).parent / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Matches youtube.com, youtu.be, m.youtube.com, and Shorts links
YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE,
)

# Telegram bots can only send files up to 50 MB via the regular Bot API.
MAX_FILESIZE_MB = 50


def _resolve_cookie_file() -> Optional[Path]:
    """
    Return a writable cookies file path for yt-dlp, or None if unavailable.
    Supports Railway-style YOUTUBE_COOKIES_TEXT (paste full cookies.txt content).
    """
    cookies_text = os.environ.get("YOUTUBE_COOKIES_TEXT", "").strip()
    if cookies_text:
        writable_cookie_path = Path("/tmp/cookies.txt")
        writable_cookie_path.write_text(cookies_text + "\n", encoding="utf-8")
        logger.info("Using YouTube cookies from YOUTUBE_COOKIES_TEXT (%d bytes)", len(cookies_text))
        return writable_cookie_path

    cookie_candidates = []
    env_cookie = os.environ.get("YOUTUBE_COOKIES_PATH")
    if env_cookie:
        cookie_candidates.append(Path(env_cookie))
    cookie_candidates.append(Path(__file__).parent / "cookies.txt")
    cookie_candidates.append(Path("/etc/secrets/cookies.txt"))

    secret_cookie_path = next((p for p in cookie_candidates if p.exists()), None)
    if secret_cookie_path is None:
        return None

    writable_cookie_path = Path("/tmp/cookies.txt")
    shutil.copyfile(str(secret_cookie_path), writable_cookie_path)
    logger.info("Using YouTube cookies from %s", secret_cookie_path)
    return writable_cookie_path


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Welcome to GRAB AUDIO!*\n\nChoose an option or send a YouTube link.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Just paste a YouTube link (regular video or Shorts).\n"
        "I'll download it, convert it to MP3, and send it back.\n\n"
        "Limits:\n"
        f"• Files over {MAX_FILESIZE_MB} MB can't be sent by the bot (Telegram API limit).\n"
        "• Only use this on content you have the right to download.\n"
    )


def download_audio(url: str, out_dir: Path):
    """
    Downloads the best available audio stream for `url` and converts it to MP3
    using yt-dlp + ffmpeg. Returns (mp3_path, video_title).
    Runs synchronously — call it via run_in_executor from async code.
    """
    outtmpl = str(out_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best/bv*+ba/b",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
        ],
        "writethumbnail": True,
        # Cloud server IPs often get flagged by YouTube's bot-detection.
        # Pretending to be the Android client usually avoids the
        # "Sign in to confirm you're not a bot" block.
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
            }
        },
    }

    cookie_file = _resolve_cookie_file()
    if cookie_file is not None:
        ydl_opts["cookiefile"] = str(cookie_file)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = info.get("id")
        video_title = info.get("title") or video_id

    mp3_path = out_dir / f"{video_id}.mp3"
    if not mp3_path.exists():
        raise FileNotFoundError("Conversion finished but the MP3 file was not found.")

    # yt-dlp saves the thumbnail alongside the audio with a matching base
    # name (e.g. VIDEOID.jpg / .webp). Find it, whatever the extension.
    raw_thumbnail_path = None
    for candidate in out_dir.glob(f"{video_id}.*"):
        if candidate.suffix.lower() not in (".mp3",):
            raw_thumbnail_path = candidate
            break

    square_thumbnail_path = None
    if raw_thumbnail_path and raw_thumbnail_path.exists():
        try:
            square_thumbnail_path = _embed_square_thumbnail(mp3_path, raw_thumbnail_path)
        except Exception:
            logger.warning("Could not embed thumbnail; continuing without cover art.", exc_info=True)
        finally:
            raw_thumbnail_path.unlink(missing_ok=True)

    # Rename the file to the video's title so the person receives a
    # sensibly-named MP3 instead of a random-looking video ID.
    safe_title = re.sub(r'[\\/*?:"<>|]', "", video_title).strip()
    safe_title = safe_title[:150] if safe_title else video_id  # keep filenames reasonable
    renamed_path = out_dir / f"{safe_title}.mp3"
    if renamed_path != mp3_path:
        try:
            mp3_path.rename(renamed_path)
            mp3_path = renamed_path
        except OSError:
            pass  # fall back to the original id-based filename if rename fails

    return mp3_path, video_title, square_thumbnail_path


def _embed_square_thumbnail(mp3_path: Path, thumbnail_path: Path) -> Path:
    """
    Pads `thumbnail_path` to a square by filling the extra space with a
    blurred, stretched copy of the same image (like Spotify/Instagram do),
    so none of the original 16:9 picture gets cropped off. Embeds the
    result as cover art into `mp3_path` and returns the square image path
    so it can ALSO be passed to Telegram's `thumbnail` parameter directly —
    Telegram's audio preview doesn't reliably read embedded ID3 art, it
    wants a separate thumbnail file.
    """
    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
        str(thumbnail_path),
    ]
    probe = subprocess.run(probe_cmd, check=True, capture_output=True, text=True)
    width_str, height_str = probe.stdout.strip().split(",")
    width, height = int(width_str), int(height_str)
    target = max(width, height)

    cropped_path = thumbnail_path.with_name(thumbnail_path.stem + "_square.jpg")
    filter_complex = (
        f"[0:v]scale={target}:{target}:force_original_aspect_ratio=increase,"
        f"crop={target}:{target},gblur=sigma=20[bg];"
        f"[bg][0:v]overlay=(W-w)/2:(H-h)/2[outv]"
    )
    pad_cmd = [
        "ffmpeg", "-y", "-i", str(thumbnail_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-frames:v", "1",
        str(cropped_path),
    ]
    subprocess.run(pad_cmd, check=True, capture_output=True)

    temp_output = mp3_path.with_name(mp3_path.stem + "_with_art.mp3")
    embed_cmd = [
        "ffmpeg", "-y",
        "-i", str(mp3_path),
        "-i", str(cropped_path),
        "-map", "0:0", "-map", "1:0",
        "-c", "copy",
        "-id3v2_version", "3",
        "-metadata:s:v", "title=Album cover",
        "-metadata:s:v", "comment=Cover (front)",
        str(temp_output),
    ]
    subprocess.run(embed_cmd, check=True, capture_output=True)

    temp_output.replace(mp3_path)
    return cropped_path


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if text=="🎵 Convert Audio":
        m=await update.message.reply_text("📥 Please send me a YouTube video or Shorts URL.",reply_markup=MAIN_KEYBOARD)
        context.user_data["prompt_message_id"]=m.message_id
        return
    elif text=="❓ Help":
        await help_command(update,context); return
    elif text=="ℹ️ About":
        await update.message.reply_text("🎵 GRAB AUDIO\nConvert YouTube videos into MP3.",reply_markup=MAIN_KEYBOARD); return
    match = YOUTUBE_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Please send a valid YouTube video or Shorts link (youtube.com or youtu.be)."
        )
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("⏳ Downloading and converting audio, please wait...")

    user_dir = DOWNLOAD_DIR / str(update.effective_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)

    mp3_path = None
    thumbnail_path = None
    try:
        loop = asyncio.get_running_loop()
        mp3_path, video_title, thumbnail_path = await loop.run_in_executor(
            None, download_audio, url, user_dir
        )

        size_mb = mp3_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILESIZE_MB:
            await status_msg.edit_text(
                f"⚠️ The audio is {size_mb:.1f} MB, which is over Telegram's "
                f"{MAX_FILESIZE_MB} MB bot upload limit. Try a shorter video."
            )
            return

        await status_msg.edit_text("✅ Done! Sending your file...")
        with open(mp3_path, "rb") as audio_file:
            if thumbnail_path and thumbnail_path.exists():
                with open(thumbnail_path, "rb") as thumb_file:
                    await update.message.reply_audio(
                        audio=audio_file,
                        filename=mp3_path.name,
                        title=video_title,
                        thumbnail=thumb_file,
                        caption="sent successfully ✅",
                    )
            else:
                await update.message.reply_audio(
                    audio=audio_file,
                    filename=mp3_path.name,
                    title=video_title,
                    caption="sent successfully ✅",
                )

        # Clean up the "Downloading..." / "Done!" status message now that
        # the actual audio message has been delivered.
        try:
            await status_msg.delete()
        except Exception:
            pass

        pid=context.user_data.pop("prompt_message_id",None)
        if pid:
            try:
                await context.bot.delete_message(update.effective_chat.id,pid)
            except Exception:
                pass
        await update.message.reply_text("✅ Ready for another conversion!",reply_markup=MAIN_KEYBOARD)

        # Also delete the person's original YouTube link message, per request.
        try:
            await update.message.delete()
        except Exception:
            logger.warning("Could not delete the original link message.", exc_info=True)

    except yt_dlp.utils.DownloadError as e:
        logger.exception("yt-dlp failed to download %s", url)
        err = str(e).lower()
        if "sign in" in err or "bot" in err or "confirm" in err:
            await status_msg.edit_text(
                "❌ YouTube blocked this server (cloud IP). "
                "Add a cookies.txt file to the bot and redeploy — "
                "or run the bot on Termux/home instead of Discloud."
            )
        else:
            await status_msg.edit_text(
                "❌ Couldn't download that video. It may be private, age-restricted, "
                "region-locked, or the link is invalid."
            )
    except Exception as e:
        logger.exception("Unexpected error while processing %s", url)
        await status_msg.edit_text(f"❌ Something went wrong: {e}")
    finally:
        if mp3_path is not None and mp3_path.exists():
            try:
                mp3_path.unlink()
            except OSError:
                logger.warning("Could not delete temp file %s", mp3_path)
        if thumbnail_path is not None and thumbnail_path.exists():
            try:
                thumbnail_path.unlink()
            except OSError:
                logger.warning("Could not delete temp file %s", thumbnail_path)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error %s", update, context.error)


class _PingHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler so Render sees this as a 'web service' and
    UptimeRobot has something to ping to keep the free instance awake."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive.")

    def log_message(self, format, *args):
        pass  # silence default request logging noise


def _start_keepalive_server():
    # Only needed on Render-style hosts. Discloud bots do not need this.
    if not os.environ.get("ENABLE_KEEPALIVE"):
        return
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), _PingHandler)
        logger.info("Keep-alive web server listening on port %s", port)
        server.serve_forever()
    except Exception:
        logger.warning("Keep-alive server failed to start.", exc_info=True)


def main():
    logger.info("Starting bot from %s", Path(__file__).resolve())
    logger.info("Files here: %s", [p.name for p in Path(__file__).parent.iterdir()])

    threading.Thread(target=_start_keepalive_server, daemon=True).start()

    cookie_file = _resolve_cookie_file()
    if cookie_file is not None:
        size = cookie_file.stat().st_size
        logger.info("Cookie file ready at %s (%d bytes)", cookie_file, size)
    else:
        logger.warning(
            "Cookie file NOT FOUND — YouTube will likely block downloads on cloud hosts. "
            "Set YOUTUBE_COOKIES_TEXT in Railway, or add cookies.txt and redeploy."
        )

    bot_token = _get_bot_token()
    if not bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Get a token from @BotFather on Telegram and set it before running."
        )

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting (polling mode)...")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
