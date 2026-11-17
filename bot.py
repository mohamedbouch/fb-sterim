import os
import asyncio
import logging
import subprocess
import tempfile
import uuid
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# حالات المحادثة
WAIT_STREAM_KEY, WAIT_SOURCE_URL, CONFIRM = range(3)

# تخزين مؤقت للعمليات الجارية (لكل مستخدم)
active_streams: dict[int, dict] = {}


# ---------- أدوات مساعدة ----------
def is_valid_url(url: str) -> bool:
    return url.startswith(("http://", "https://", "rtmp://", "rtsp://"))


def detect_format(url: str) -> str:
    url_l = url.lower().split("?")[0]
    for ext in (".m3u8", ".ts", ".mp4", ".mkv", ".flv", ".mov", ".webm", ".mpd"):
        if url_l.endswith(ext):
            return ext[1:].upper()
    return "AUTO"


# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎥 *بوت البث على فيسبوك*\n\n"
        "أرسل /stream لبدء بث جديد\n"
        "أرسل /stop لإيقاف البث الحالي\n"
        "أرسل /status لمعرفة حالة البث"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------- /stream ----------
async def stream_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in active_streams and active_streams[user_id].get("running"):
        await update.message.reply_text(
            "⚠️ لديك بث نشط بالفعل. أرسل /stop أولاً."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🔑 *الخطوة 1/2:*\nأرسل *مفتاح البث* من فيسبوك (Stream Key).",
        parse_mode="Markdown",
    )
    return WAIT_STREAM_KEY


async def receive_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    if len(key) < 6:
        await update.message.reply_text("❌ مفتاح غير صالح، حاول مجدداً.")
        return WAIT_STREAM_KEY

    context.user_data["stream_key"] = key
    await update.message.reply_text(
        "🔗 *الخطوة 2/2:*\nأرسل رابط البث المصدر "
        "(m3u8 / ts / mp4 / rtmp ...)",
        parse_mode="Markdown",
    )
    return WAIT_SOURCE_URL


async def receive_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text(
            "❌ الرابط غير صالح. يجب أن يبدأ بـ http:// أو https:// أو rtmp://"
        )
        return WAIT_SOURCE_URL

    context.user_data["source_url"] = url
    fmt = detect_format(url)
    key = context.user_data["stream_key"]

    text = (
        f"📋 *تأكيد البيانات:*\n\n"
        f"🔑 المفتاح: `{key[:6]}...{key[-4:]}`\n"
        f"🔗 المصدر: `{url[:60]}...`\n"
        f"📦 الصيغة: `{fmt}`\n\n"
        f"هل تريد بدء البث؟"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ بدء البث", callback_data="start_stream")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
        ]
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "cancel":
        await query.edit_message_text("❌ تم الإلغاء.")
        return ConversationHandler.END

    source = context.user_data["source_url"]
    key = context.user_data["stream_key"]
    rtmp_target = f"rtmps://live-api-s.facebook.com:443/rtmp/{key}"

    await query.edit_message_text("🚀 جاري بدء البث...")

    # أمر ffmpeg
    cmd = [
        "ffmpeg",
        "-re",
        "-i", source,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-b:v", "2500k",
        "-maxrate", "2500k",
        "-bufsize", "5000k",
        "-pix_fmt", "yuv420p",
        "-g", "50",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-f", "flv",
        rtmp_target,
    ]

    log_file = tempfile.NamedTemporaryFile(
        mode="w", prefix="ffmpeg_", suffix=".log", delete=False
    )

    try:
        proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True
        )
    except FileNotFoundError:
        await query.edit_message_text("❌ ffmpeg غير مثبت على السيرفر.")
        return ConversationHandler.END

    active_streams[user_id] = {
        "running": True,
        "proc": proc,
        "started": datetime.now(),
        "source": source,
        "log": log_file.name,
    }

    await query.edit_message_text(
        "✅ *البث بدأ بنجاح!*\n\n"
        f"🕐 وقت البدء: {datetime.now().strftime('%H:%M:%S')}\n"
        "أرسل /stop لإيقافه.",
        parse_mode="Markdown",
    )

    # مراقبة العملية في الخلفية
    asyncio.create_task(_watch_process(user_id, proc))
    return ConversationHandler.END


async def _watch_process(user_id: int, proc: subprocess.Popen):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, proc.wait)
    data = active_streams.get(user_id)
    if data and data.get("proc") is proc:
        data["running"] = False
        logger.info(f"Stream for user {user_id} ended (code={proc.returncode})")


# ---------- /stop ----------
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = active_streams.get(user_id)
    if not data or not data.get("running"):
        await update.message.reply_text("ℹ️ لا يوجد بث نشط.")
        return

    proc: subprocess.Popen = data["proc"]
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        logger.warning(f"kill error: {e}")

    data["running"] = False
    await update.message.reply_text("🛑 تم إيقاف البث.")


# ---------- /status ----------
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = active_streams.get(user_id)
    if not data or not data.get("running"):
        await update.message.reply_text("📴 لا يوجد بث نشط.")
        return

    elapsed = datetime.now() - data["started"]
    mins, secs = divmod(int(elapsed.total_seconds()), 60)
    await update.message.reply_text(
        f"📡 *البث نشط*\n\n"
        f"⏱ المدة: {mins}د {secs}ث\n"
        f"🔗 المصدر: `{data['source'][:60]}`",
        parse_mode="Markdown",
    )


# ---------- إلغاء ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم الإلغاء.")
    return ConversationHandler.END


# ---------- main ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود في متغيرات البيئة!")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("stream", stream_start)],
        states={
            WAIT_STREAM_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_key)
            ],
            WAIT_SOURCE_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_source)
            ],
            CONFIRM: [CallbackQueryHandler(confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(conv)

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
