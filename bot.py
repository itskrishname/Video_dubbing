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

# Voice configurations
VOICES = {
    "hi": [
        {"id": "hi-IN-MadhurNeural", "name": "1. Boy (Madhur)", "type": "male"},
        {"id": "hi-IN-SwaraNeural", "name": "2. Girl (Swara)", "type": "female"},
        # Falling back to Indian English for more Hindi accent varieties if needed
        {"id": "en-IN-PrabhatNeural", "name": "3. Boy (Prabhat)", "type": "male"},
        {"id": "en-IN-NeerjaNeural", "name": "4. Girl (Neerja)", "type": "female"},
        {"id": "en-IN-NeerjaExpressiveNeural", "name": "5. Girl (Neerja Expressive)", "type": "female"},
    ],
    "en": [
        {"id": "en-US-ChristopherNeural", "name": "1. Boy (Christopher)", "type": "male"},
        {"id": "en-US-JennyNeural", "name": "2. Girl (Jenny)", "type": "female"},
        {"id": "en-US-GuyNeural", "name": "3. Boy (Guy)", "type": "male"},
        {"id": "en-US-AriaNeural", "name": "4. Girl (Aria)", "type": "female"},
        {"id": "en-US-AndrewNeural", "name": "5. Boy (Andrew)", "type": "male"},
    ]
}

# User state to store selected voice. Default to English Christopher.
# In a real database this should persist, but for a simple bot memory is fine.
USER_SETTINGS = {
    "lang_code": "en",
    "voice_id": "en-US-ChristopherNeural"
}

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
        "1. First, use /voice to choose your preferred dubbing voice.\n"
        "2. Then, send me a video file and I will dub it automatically!\n"
    )

@app.on_message(filters.command("voice") & filters.private)
async def voice_command(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ Unauthorized.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang_hi"),
         InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")]
    ])
    await message.reply_text(
        "🗣 **Voice Settings**\n\nChoose the language first:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^lang_"))
async def language_selection_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    lang = callback_query.data.split("_")[1]
    USER_SETTINGS["lang_code"] = lang

    # Build keyboard for 1 to 5 voices
    buttons = []
    for voice in VOICES[lang]:
        buttons.append([InlineKeyboardButton(voice["name"], callback_data=f"voice_{voice['id']}")])

    buttons.append([InlineKeyboardButton("🔙 Back to Language", callback_data="back_lang")])

    await callback_query.message.edit_text(
        f"🗣 **Voice Settings ({'Hindi' if lang == 'hi' else 'English'})**\n\n"
        "Tap a voice to set it and hear a quick preview 👀:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex("^back_lang$"))
async def back_to_lang_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang_hi"),
         InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")]
    ])
    await callback_query.message.edit_text(
        "🗣 **Voice Settings**\n\nChoose the language first:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^voice_"))
async def voice_selection_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    voice_id = callback_query.data.replace("voice_", "")
    USER_SETTINGS["voice_id"] = voice_id

    await callback_query.answer("Voice updated! Generating preview... 👀", show_alert=False)

    # Generate Preview
    preview_text = "Hello! This is a preview of my voice. Aap mujhe video bhej sakte hain." if USER_SETTINGS["lang_code"] == "hi" else "Hello! This is a preview of my voice. Send me a video and I will dub it."
    preview_path = f"/tmp/preview_{int(time.time())}.mp3"

    try:
        communicate = edge_tts.Communicate(preview_text, voice_id)
        await communicate.save(preview_path)
        await callback_query.message.reply_audio(audio=preview_path, title="Voice Preview", performer="AI Dub Bot")
    except Exception as e:
        print(f"Preview error: {e}")
        await callback_query.message.reply_text("❌ Error generating preview.")
    finally:
        if os.path.exists(preview_path):
            os.remove(preview_path)

@app.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Instantly start processing with saved settings
    status_msg = await message.reply_text("✦ ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ... ✦", quote=True)

    # Run the processing pipeline
    asyncio.create_task(process_video(client, message, status_msg))

async def process_video(client: Client, video_msg: Message, status_msg: Message):
    lang_code = USER_SETTINGS["lang_code"]
    voice_id = USER_SETTINGS["voice_id"]

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
            tts_text = translated_text
        else:
            # Keep English
            tts_text = transcribed_text

        # Generate TTS audio using user's selected voice
        communicate = edge_tts.Communicate(tts_text, voice_id)
        await communicate.save(tts_audio_path)

        # 5. Audio Sync
        await update_status(status_msg, "sʏɴᴄɪɴɢ ᴀᴜᴅɪᴏ ᴡɪᴛʜ ᴠɪᴅᴇᴏ...")
        orig_duration = get_duration(orig_video_path)
        tts_duration = get_duration(tts_audio_path)

        if orig_duration > 0 and tts_duration > 0:
            tempo = tts_duration / orig_duration

            # Since we want the new audio to perfectly fit the old duration:
            # tempo = tts_duration / orig_duration
            # FFMPEG atempo filter limits are 0.5 to 100.0 per filter.
            # We chain multiple filters to achieve highly precise, extreme tempo changes if needed for lipsync.

            tempo_filters = []
            current_tempo = tempo

            while current_tempo > 2.0:
                tempo_filters.append(2.0)
                current_tempo /= 2.0
            while current_tempo < 0.5:
                tempo_filters.append(0.5)
                current_tempo /= 0.5

            if current_tempo != 1.0:
                # Keep high precision for the remaining tempo
                tempo_filters.append(round(current_tempo, 4))

            try:
                # Apply the tempo filters
                stream = ffmpeg.input(tts_audio_path)
                for f in tempo_filters:
                    stream = stream.filter('atempo', f)

                stream = ffmpeg.output(stream, synced_audio_path, ar=44100) # standardize sample rate for better quality
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
