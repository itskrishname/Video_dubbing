import os
import asyncio
import time
import ffmpeg
import whisper
import edge_tts
from deep_translator import GoogleTranslator
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from pyrogram.errors import MessageNotModified
from config import Config

# Initialize Pyrogram Client
app = Client(
    "video_dub_bot",
    api_id=Config.TELEGRAM_API_ID,
    api_hash=Config.TELEGRAM_API_HASH,
    bot_token=Config.BOT_TOKEN
)

# Load Whisper model (Base model)
print("Loading Whisper Model...")
model = whisper.load_model("base")
print("Whisper Model Loaded.")

# Blocking sync functions to run in executor
def run_ffmpeg(stream):
    ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)

def transcribe_audio(audio_path):
    return model.transcribe(audio_path, language="en", fp16=False)

def translate_text(text):
    return GoogleTranslator(source='auto', target='hi').translate(text)

# Helper function to get duration of audio/video using ffprobe
def get_duration(filename):
    try:
        probe = ffmpeg.probe(filename)
        duration = float(probe['format']['duration'])
        return duration
    except ffmpeg.Error as e:
        print(f"ffprobe error: {e.stderr.decode()}")
        return 0

async def update_status(message: Message, text: str):
    try:
        await message.edit_text(f"✦ {text} ✦")
    except MessageNotModified:
        pass

@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ Hello! I am a private Video Dubbing bot and only my owner can use me.")
        return

    await message.reply_text(
        "👋 Hello there!\n\n"
        "I am your Automated Video Dubbing Bot.\n"
        "Send me a video file, and I will translate and dub it for you into English or Hindi using AI."
    )

@app.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Send language selection
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Dub in Hindi 🇮🇳", callback_data="dub_hi"),
                InlineKeyboardButton("Dub in English 🇺🇸", callback_data="dub_en")
            ]
        ]
    )
    await message.reply_text(
        "🎥 Video received! Choose the language you want to dub it into:",
        reply_markup=keyboard,
        quote=True
    )

@app.on_callback_query(filters.regex("^dub_"))
async def process_video_callback(client: Client, callback_query: CallbackQuery):
    lang_code = callback_query.data.split("_")[1]

    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    # Acknowledge callback and update status
    await callback_query.answer()
    status_msg = callback_query.message
    await update_status(status_msg, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ...")

    # Original video message
    video_msg = status_msg.reply_to_message
    if not video_msg or not video_msg.video:
        await update_status(status_msg, "❌ Error: Could not find the original video.")
        return

    # Temporary file paths
    timestamp = int(time.time())
    orig_video_path = f"/tmp/input_{timestamp}.mp4"
    audio_path = f"/tmp/audio_{timestamp}.wav"
    tts_audio_path = f"/tmp/tts_{timestamp}.mp3"
    synced_audio_path = f"/tmp/synced_{timestamp}.mp3"
    final_video_path = f"/tmp/final_{timestamp}.mp4"

    try:
        # 1. Download Video
        await video_msg.download(file_name=orig_video_path)

        # 2. Extract Audio
        await update_status(status_msg, "ᴇxᴛʀᴀᴄᴛɪɴɢ ᴀᴜᴅɪᴏ...")
        try:
            stream = ffmpeg.input(orig_video_path).output(audio_path, acodec='pcm_s16le', ac=1, ar='16k')
            await asyncio.to_thread(run_ffmpeg, stream)
        except ffmpeg.Error as e:
            print(e.stderr.decode())
            await update_status(status_msg, "❌ Error extracting audio.")
            return

        # 3. Transcribe with Whisper
        await update_status(status_msg, "ᴛʀᴀɴsᴄʀɪʙɪɴɢ ᴀᴜᴅɪᴏ...")
        result = await asyncio.to_thread(transcribe_audio, audio_path)
        transcribed_text = result["text"].strip()

        if not transcribed_text:
            await update_status(status_msg, "❌ Could not detect any speech in the video.")
            return

        # 4. Translate & TTS
        await update_status(status_msg, "ɢᴇɴᴇʀᴀᴛɪɴɢ ᴀɪ ᴠᴏɪᴄᴇ...")

        if lang_code == "hi":
            # Translate to Hindi
            translated_text = await asyncio.to_thread(translate_text, transcribed_text)
            voice = "hi-IN-MadhurNeural"
            tts_text = translated_text
        else:
            # Keep English
            voice = "en-US-ChristopherNeural"
            tts_text = transcribed_text

        # Generate TTS audio
        communicate = edge_tts.Communicate(tts_text, voice)
        await communicate.save(tts_audio_path)

        # 5. Audio Sync
        await update_status(status_msg, "sʏɴᴄɪɴɢ ᴀᴜᴅɪᴏ ᴡɪᴛʜ ᴠɪᴅᴇᴏ...")
        orig_duration = get_duration(orig_video_path)
        tts_duration = get_duration(tts_audio_path)

        if orig_duration > 0 and tts_duration > 0:
            tempo = tts_duration / orig_duration
            # ffmpeg atempo filter limits are 0.5 to 100.
            # If the tempo is outside this range, we might need multiple atempo filters or just clamp it.
            # For simplicity, we clamp it to valid ranges if it gets too extreme (though it might not perfectly sync).
            # Usually, TTS duration isn't wildly different.

            # Since we want the new audio to fit the old duration:
            # new_duration = old_duration / tempo
            # So if we want new audio to be orig_duration, tempo = tts_duration / orig_duration

            tempo_filters = []
            current_tempo = tempo
            while current_tempo > 2.0:
                tempo_filters.append("atempo=2.0")
                current_tempo /= 2.0
            while current_tempo < 0.5:
                tempo_filters.append("atempo=0.5")
                current_tempo /= 0.5

            if 0.5 <= current_tempo <= 2.0:
                tempo_filters.append(f"atempo={current_tempo}")

            atempo_str = ",".join(tempo_filters)

            try:
                # Apply the tempo filters
                stream = ffmpeg.input(tts_audio_path)
                for f in tempo_filters:
                    # ffmpeg-python expects kwargs for filters but atempo is simple
                    stream = stream.filter('atempo', f.split('=')[1])

                stream = ffmpeg.output(stream, synced_audio_path)
                await asyncio.to_thread(run_ffmpeg, stream)

            except ffmpeg.Error as e:
                # If atempo fails (e.g. out of bounds), just use the unsynced TTS audio
                print(f"atempo error: {e.stderr.decode()}")
                synced_audio_path = tts_audio_path
        else:
            synced_audio_path = tts_audio_path

        # 6. Merge
        await update_status(status_msg, "ᴍᴇʀɢɪɴɢ ᴀᴜᴅɪᴏ & ᴠɪᴅᴇᴏ...")

        video_input = ffmpeg.input(orig_video_path)
        audio_input = ffmpeg.input(synced_audio_path)

        try:
            stream = ffmpeg.output(video_input.video, audio_input.audio, final_video_path, vcodec='copy', acodec='aac', strict='experimental')
            await asyncio.to_thread(run_ffmpeg, stream)
        except ffmpeg.Error as e:
            print(f"merge error: {e.stderr.decode()}")
            await update_status(status_msg, "❌ Error merging audio and video.")
            return

        # 7. Upload
        await update_status(status_msg, "ᴜᴘʟᴏᴀᴅɪɴɢ ᴅᴜʙʙᴇᴅ ᴠɪᴅᴇᴏ...")
        await video_msg.reply_video(
            video=final_video_path,
            caption="✨ Dubbed Video Processed Successfully!"
        )
        await status_msg.delete()

    except Exception as e:
        print(f"Error processing video: {e}")
        await update_status(status_msg, f"❌ An error occurred: {str(e)}")

    finally:
        # Cleanup
        files_to_remove = [orig_video_path, audio_path, tts_audio_path, synced_audio_path, final_video_path]
        for f in files_to_remove:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception as e:
                    print(f"Failed to remove {f}: {e}")

if __name__ == "__main__":
    print("Bot is starting...")
    app.run()
