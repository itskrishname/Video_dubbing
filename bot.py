import os
import sys
import asyncio
import time
import ffmpeg
import whisper
import edge_tts
import g4f
from deep_translator import GoogleTranslator
from indic_transliteration import sanscript
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from pyrogram.errors import MessageNotModified
from config import Config

# Initialize Pyrogram Client
if not Config.TELEGRAM_API_ID or not Config.TELEGRAM_API_HASH:
    raise ValueError("TELEGRAM_API_ID or TELEGRAM_API_HASH is missing. Please check config.py")

app = Client(
    "video_dub_bot",
    api_id=Config.TELEGRAM_API_ID,
    api_hash=Config.TELEGRAM_API_HASH,
    bot_token=Config.BOT_TOKEN
)

# Load Whisper model (Small model for higher VPS accuracy)
print("Loading Whisper Model...")
model = whisper.load_model("small")
print("Whisper Model Loaded.")

# Store ongoing sessions for the review flow
SESSIONS = {}

# Blocking sync functions to run in executor
def run_ffmpeg(stream):
    # Pass threads=0 globally to allow FFMPEG to use all available VPS CPU cores
    stream = ffmpeg.global_args(stream, '-threads', '0')
    ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)

def transcribe_audio(audio_path):
    import torch
    # Auto-detect if CUDA is available on the VPS, otherwise fallback to CPU-friendly fp16=False
    fp16 = torch.cuda.is_available()
    return model.transcribe(audio_path, language="en", fp16=fp16)

def translate_text(text, target_lang):
    if target_lang == "en":
        return text

    if target_lang == "hinglish":
        # Use AI model for natural, conversational Hinglish instead of formal translation
        try:
            prompt = (
                "You are an expert localizer. Your job is to translate the given English text into casual 'Hinglish' "
                "(Hindi written ONLY in the English alphabet, exactly like a WhatsApp chat).\n\n"
                "CRITICAL RULES:\n"
                "1. NEVER output pure English. It must be Hindi grammar/structure.\n"
                "2. NEVER output Devanagari script (e.g., नमस्ते is WRONG. Namaste is CORRECT).\n"
                "3. Keep common English nouns/verbs if they are used in daily life (e.g., use 'time' instead of 'samay', 'phone', 'wait').\n"
                "4. Output ONLY the translation. No quotes, no explanations.\n\n"
                "EXAMPLES:\n"
                "English: I want to eat an apple.\n"
                "Hinglish: Mujhe ek apple khana hai.\n\n"
                "English: What time are we going to the store?\n"
                "Hinglish: Hum log store kis time ja rahe hain?\n\n"
                "English: I will check my phone and call you later.\n"
                "Hinglish: Main apna phone check karke tumhe baad mein call karta hu.\n\n"
                f"Now, translate this English text into Hinglish:\n{text}"
            )
            response = g4f.ChatCompletion.create(
                model='openai',
                provider=g4f.Provider.PollinationsAI,
                messages=[{'role': 'user', 'content': prompt}]
            )
            return response.strip()
        except Exception as e:
            print(f"Hinglish AI Translation Error: {e}")
            # Fallback to transliterated formal Hindi if AI fails
            hindi_text = GoogleTranslator(source='auto', target='hi').translate(text)
            hinglish_text = sanscript.transliterate(hindi_text, sanscript.DEVANAGARI, sanscript.ITRANS)
            return hinglish_text.capitalize()

    # Translate to Hindi Devanagari
    return GoogleTranslator(source='auto', target='hi').translate(text)

def translate_segments_to_srt_string(segments, target_lang):
    srt_content = ""
    for i, segment in enumerate(segments, start=1):
        start = format_timestamp(segment["start"])
        end = format_timestamp(segment["end"])
        text = segment["text"].strip()

        if target_lang != "en":
            # Translate each subtitle segment individually
            text = translate_text(text, target_lang)

        srt_content += f"{i}\n"
        srt_content += f"{start} --> {end}\n"
        srt_content += f"{text}\n\n"
    return srt_content

def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def time_formatter(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h, {minutes}m, {secs}s"
    elif minutes > 0:
        return f"{minutes}m, {secs}s"
    else:
        return f"{secs}s"

# Track last message edit times to avoid FloodWait
PROGRESS_CACHE = {}

async def progress_callback(current, total, message: Message, start_time, operation_name):
    now = time.time()
    diff = now - start_time

    msg_id = message.id
    last_updated = PROGRESS_CACHE.get(msg_id, 0)

    # Update only every 2 seconds to avoid Telegram FloodWait
    if last_updated + 2 > now and current < total:
        return

    PROGRESS_CACHE[msg_id] = now

    percent = round((current / total) * 100, 2)

    # Progress Bar [██████▒▒▒▒]
    filled = int(percent / 10)
    bar = "█" * filled + "▒" * (10 - filled)

    # Speed (Bytes per second)
    speed = current / diff if diff > 0 else 0
    # Speed in MB/s
    speed_mb = speed / (1024 * 1024)

    # Time Taken
    time_taken = diff

    # Time Left
    time_left = (total - current) / speed if speed > 0 else 0

    text = (
        f"✦ {operation_name} ✦\n\n"
        f"♻️ᴘʀᴏɢʀᴇss: {percent}% [{bar}]\n\n"
        f"🕛 ᴛɪᴍᴇ ʟᴇꜰᴛ: {time_formatter(time_left)} ⏱️ ᴛɪᴍᴇ ᴛᴀᴋᴇɴ: {time_formatter(time_taken)}\n"
        f"ꜱᴘᴇᴇᴅ: {speed_mb:.2f} MB/s"
    )

    try:
        await message.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        print(f"Progress Error: {e}")

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
        "I am your Automated Video Processing Bot.\n"
        "Send me a video file, and I will Dub it or add Subtitles for you!"
    )

@app.on_message(filters.command("update") & filters.private)
async def update_command(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ Unauthorized.")
        return

    status_msg = await message.reply_text("🔄 Pulling latest updates from Git...")
    try:
        # Run git pull
        process = await asyncio.create_subprocess_shell(
            "git pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        output = stdout.decode().strip()
        error = stderr.decode().strip()

        response_text = f"✅ **Update Status:**\n\n**Output:**\n`{output}`"
        if error:
            response_text += f"\n\n**Warnings/Errors:**\n`{error}`"

        if "Already up to date" in output:
            await status_msg.edit_text(response_text)
            return

        await status_msg.edit_text(response_text + "\n\n♻️ **Restarting bot to apply changes...**")

        # Restart the process
        os.execv(sys.executable, ['python3'] + sys.argv)

    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to update: {str(e)}")

@app.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎙️ Dub Video", callback_data="menu_dub"),
            InlineKeyboardButton("📝 Subtitle Video", callback_data="menu_sub")
        ]
    ])

    await message.reply_text(
        "🎥 Video received! What would you like to do?",
        reply_markup=keyboard,
        quote=True
    )

@app.on_callback_query(filters.regex("^menu_"))
async def main_menu_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    action = callback_query.data.split("_")[1] # 'dub' or 'sub'

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇮🇳 Hindi", callback_data=f"{action}_hi"),
            InlineKeyboardButton("🇺🇸 English", callback_data=f"{action}_en")
        ],
        [
            InlineKeyboardButton("🇮🇳🇺🇸 Hinglish", callback_data=f"{action}_hinglish")
        ]
    ])

    action_text = "Dubbing" if action == "dub" else "Subtitles"

    await callback_query.message.edit_text(
        f"Select the language for **{action_text}**:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^(dub|sub)_"))
async def process_video_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    action, lang_code = callback_query.data.split("_")

    await callback_query.answer()
    status_msg = callback_query.message
    await update_status(status_msg, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ...")

    video_msg = status_msg.reply_to_message
    if not video_msg or not video_msg.video:
        await update_status(status_msg, "❌ Error: Could not find the original video.")
        return

    # Run the processing pipeline
    asyncio.create_task(process_video(client, video_msg, status_msg, action, lang_code))

@app.on_callback_query(filters.regex("^review_"))
async def review_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    choice = callback_query.data.split("_")[1]
    session = SESSIONS.get(user_id)

    if not session:
        await callback_query.answer("❌ Session expired or not found.", show_alert=True)
        return

    await callback_query.answer()

    if choice == "cancel":
        await update_status(session["status_msg"], "❌ Process Canceled.")
        cleanup_session(user_id)
    elif choice == "regen":
        await session["status_msg"].edit_reply_markup(reply_markup=None)
        await generate_and_send_review(client, user_id)
    elif choice == "done":
        await session["status_msg"].edit_reply_markup(reply_markup=None)
        asyncio.create_task(finalize_video(client, user_id))

async def process_video(client: Client, video_msg: Message, status_msg: Message, action: str, lang_code: str):
    # Temporary file paths
    user_id = video_msg.from_user.id

    # Store session info
    timestamp = int(time.time())
    SESSIONS[user_id] = {
        "timestamp": timestamp,
        "video_msg": video_msg,
        "status_msg": status_msg,
        "action": action,
        "lang_code": lang_code,
        "orig_video_path": f"/tmp/input_{timestamp}.mp4",
        "audio_path": f"/tmp/audio_{timestamp}.wav",
        "transcription_result": None
    }

    try:
        # 1. Download Video
        start_time = time.time()
        await video_msg.download(
            file_name=SESSIONS[user_id]["orig_video_path"],
            progress=progress_callback,
            progress_args=(status_msg, start_time, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ")
        )

        # 2. Extract Audio
        await update_status(status_msg, "ᴇxᴛʀᴀᴄᴛɪɴɢ ᴀᴜᴅɪᴏ...")
        try:
            stream = ffmpeg.input(SESSIONS[user_id]["orig_video_path"]).output(SESSIONS[user_id]["audio_path"], acodec='pcm_s16le', ac=1, ar='16k')
            await asyncio.to_thread(run_ffmpeg, stream)
        except ffmpeg.Error as e:
            print(e.stderr.decode())
            await update_status(status_msg, "❌ Error extracting audio.")
            cleanup_session(user_id)
            return

        # 3. Transcribe with Whisper
        await update_status(status_msg, "ᴛʀᴀɴsᴄʀɪʙɪɴɢ ᴀᴜᴅɪᴏ...")
        result = await asyncio.to_thread(transcribe_audio, SESSIONS[user_id]["audio_path"])
        transcribed_text = result["text"].strip()

        if not transcribed_text:
            await update_status(status_msg, "❌ Could not detect any speech in the video.")
            cleanup_session(user_id)
            return

        SESSIONS[user_id]["transcription_result"] = result
        SESSIONS[user_id]["transcribed_text"] = transcribed_text

        # 4. Generate Initial Translation for Review
        await generate_and_send_review(client, user_id)

    except Exception as e:
        print(f"Error processing video: {e}")
        await update_status(status_msg, f"❌ An error occurred: {str(e)}")
        cleanup_session(user_id)

async def generate_and_send_review(client: Client, user_id: int):
    session = SESSIONS.get(user_id)
    if not session:
        return

    status_msg = session["status_msg"]
    action = session["action"]
    lang_code = session["lang_code"]

    await update_status(status_msg, "ɢᴇɴᴇʀᴀᴛɪɴɢ ᴛʀᴀɴsʟᴀᴛɪᴏɴ ꜰᴏʀ ʀᴇᴠɪᴇᴡ...")

    try:
        if action == "sub":
            # For subtitles, generate the whole SRT string
            result = session["transcription_result"]
            segments = result.get("segments", [])
            translated_script = await asyncio.to_thread(translate_segments_to_srt_string, segments, lang_code)
            session["final_srt_content"] = translated_script

            # Show a preview of the first few lines to the user
            preview_text = "\n".join(translated_script.split("\n")[:15]) + "\n... (truncated)"
        else:
            # For dubbing, generate full translated text
            transcribed_text = session["transcribed_text"]
            translated_script = await asyncio.to_thread(translate_text, transcribed_text, lang_code)
            session["final_dub_text"] = translated_script
            preview_text = translated_script

        # Send Review Message
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done (Proceed)", callback_data="review_done")],
            [InlineKeyboardButton("🔄 Regenerate", callback_data="review_regen")],
            [InlineKeyboardButton("❌ Cancel", callback_data="review_cancel")]
        ])

        await status_msg.edit_text(
            f"👀 **Please Review the Translated Script:**\n\n"
            f"```text\n{preview_text}\n```\n\n"
            f"If it looks good, click **Done** to finish processing.",
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"Error generating review: {e}")
        await update_status(status_msg, f"❌ Error generating review: {e}")
        cleanup_session(user_id)

async def finalize_video(client: Client, user_id: int):
    session = SESSIONS.get(user_id)
    if not session:
        return

    status_msg = session["status_msg"]
    video_msg = session["video_msg"]
    action = session["action"]
    lang_code = session["lang_code"]

    orig_video_path = session["orig_video_path"]
    audio_path = session["audio_path"]

    # Define outputs
    tts_audio_path = f"/tmp/tts_{session['timestamp']}.mp3"
    synced_audio_path = f"/tmp/synced_{session['timestamp']}.mp3"
    srt_path = f"/tmp/subtitles_{session['timestamp']}.srt"
    final_video_path = f"/tmp/final_{session['timestamp']}.mp4"

    session["tts_audio_path"] = tts_audio_path
    session["synced_audio_path"] = synced_audio_path
    session["srt_path"] = srt_path
    session["final_video_path"] = final_video_path

    try:
        if action == "sub":
            # --- SUBTITLE GENERATION (HARDSUBS) ---
            await update_status(status_msg, "ɢᴇɴᴇʀᴀᴛɪɴɢ sᴜʙᴛɪᴛʟᴇ ꜰɪʟᴇ...")
            srt_content = session["final_srt_content"]
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(srt_content)

            await update_status(status_msg, "ʙᴜʀɴɪɴɢ sᴜʙᴛɪᴛʟᴇs ɪɴᴛᴏ ᴠɪᴅᴇᴏ...")
            try:
                escaped_srt_path = srt_path.replace("\\", "\\\\").replace(":", "\\:")

                fonts_dir = os.path.abspath("fonts").replace("\\", "\\\\").replace(":", "\\:")
                style = "FontName=Mukta,FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2"

                in_file = ffmpeg.input(orig_video_path)
                video_stream = in_file.video.filter('subtitles', escaped_srt_path, fontsdir=fonts_dir, force_style=style)
                audio_stream = in_file.audio

                stream = ffmpeg.output(video_stream, audio_stream, final_video_path, vcodec='libx264', acodec='copy')
                await asyncio.to_thread(run_ffmpeg, stream)
            except ffmpeg.Error as e:
                print(f"subtitles error: {e.stderr.decode()}")
                await update_status(status_msg, "❌ Error burning subtitles to video.")
                cleanup_session(user_id)
                return

        else:
            # --- DUBBING GENERATION ---
            await update_status(status_msg, "ɢᴇɴᴇʀᴀᴛɪɴɢ ᴀɪ ᴠᴏɪᴄᴇ...")
            translated_text = session["final_dub_text"]

            voice_id = "hi-IN-MadhurNeural" if lang_code in ["hi", "hinglish"] else "en-US-ChristopherNeural"

            communicate = edge_tts.Communicate(translated_text, voice_id)
            await communicate.save(tts_audio_path)

            await update_status(status_msg, "sʏɴᴄɪɴɢ ᴀᴜᴅɪᴏ ᴡɪᴛʜ ᴠɪᴅᴇᴏ...")
            orig_duration = get_duration(orig_video_path)
            tts_duration = get_duration(tts_audio_path)

            if orig_duration > 0 and tts_duration > 0:
                tempo = tts_duration / orig_duration
                tempo_filters = []
                current_tempo = tempo

                while current_tempo > 2.0:
                    tempo_filters.append(2.0)
                    current_tempo /= 2.0
                while current_tempo < 0.5:
                    tempo_filters.append(0.5)
                    current_tempo /= 0.5

                if current_tempo != 1.0:
                    tempo_filters.append(round(current_tempo, 4))

                try:
                    stream = ffmpeg.input(tts_audio_path)
                    for f in tempo_filters:
                        stream = stream.filter('atempo', f)

                    stream = ffmpeg.output(stream, synced_audio_path, ar=44100)
                    await asyncio.to_thread(run_ffmpeg, stream)
                except ffmpeg.Error as e:
                    print(f"atempo error: {e.stderr.decode()}")
                    synced_audio_path = tts_audio_path
            else:
                synced_audio_path = tts_audio_path

            await update_status(status_msg, "ᴍᴇʀɢɪɴɢ ᴀᴜᴅɪᴏ & ᴠɪᴅᴇᴏ...")

            video_input = ffmpeg.input(orig_video_path)
            audio_input = ffmpeg.input(synced_audio_path)

            try:
                stream = ffmpeg.output(video_input.video, audio_input.audio, final_video_path, vcodec='copy', acodec='aac', strict='experimental')
                await asyncio.to_thread(run_ffmpeg, stream)
            except ffmpeg.Error as e:
                print(f"merge error: {e.stderr.decode()}")
                await update_status(status_msg, "❌ Error merging audio and video.")
                cleanup_session(user_id)
                return

        await update_status(status_msg, "ᴜᴘʟᴏᴀᴅɪɴɢ ᴘʀᴏᴄᴇssᴇᴅ ᴠɪᴅᴇᴏ...")

        action_str = "Dubbed" if action == "dub" else "Subtitled"

        start_time = time.time()
        await video_msg.reply_video(
            video=final_video_path,
            caption=f"✨ {action_str} Video Processed Successfully!",
            progress=progress_callback,
            progress_args=(status_msg, start_time, "ᴜᴘʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ")
        )
        await status_msg.delete()

    except Exception as e:
        print(f"Error finalizing video: {e}")
        await update_status(status_msg, f"❌ An error occurred: {str(e)}")

    finally:
        cleanup_session(user_id)

def cleanup_session(user_id: int):
    session = SESSIONS.get(user_id)
    if not session:
        return

    paths = [
        session.get("orig_video_path"),
        session.get("audio_path"),
        session.get("tts_audio_path"),
        session.get("synced_audio_path"),
        session.get("srt_path"),
        session.get("final_video_path")
    ]

    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                print(f"Failed to remove {p}: {e}")

    del SESSIONS[user_id]

if __name__ == "__main__":
    print("Bot is starting...")
    app.run()
