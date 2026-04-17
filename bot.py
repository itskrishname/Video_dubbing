import os
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

# Store ongoing sessions for the review flow
SESSIONS = {}

# Blocking sync functions to run in executor
def run_ffmpeg(stream):
    ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)

def transcribe_audio(audio_path):
    return model.transcribe(audio_path, language="en", fp16=False)

def translate_text(text, target_lang):
    if target_lang == "en":
        return text

    if target_lang == "hinglish":
        # Use AI model for natural, conversational Hinglish instead of formal translation
        try:
            prompt = (
                "You are an expert translator converting English into extremely casual, Gen-Z / conversational 'Hinglish' "
                "(Hindi written in the English alphabet, like WhatsApp chats). \n"
                "CRITICAL RULES:\n"
                "1. DO NOT use formal or 'Shuddh' Hindi words (e.g., do NOT use kripya, samay, pratiksha, upayog).\n"
                "2. KEEP common English words exactly as they are (e.g., time, phone, please, check, use, problem, store, shopping).\n"
                "3. Make it sound completely natural, exactly how Indian friends chat online.\n"
                "4. Output ONLY the translated text. No quotes, no explanations, no chat.\n\n"
                f"Text to translate: {text}"
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
        await generate_and_send_review(client, user_id)
    elif choice == "done":
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
        await video_msg.download(file_name=SESSIONS[user_id]["orig_video_path"])

        # 2. Extract Audio
        await update_status(status_msg, "ᴇxᴛʀᴀᴄᴛɪɴɢ ᴀᴜᴅɪᴏ...")
        try:
            stream = ffmpeg.input(SESSIONS[user_id]["orig_video_path"]).output(SESSIONS[user_id]["audio_path"], acodec='pcm_s16le', ac=1, ar='16k')
            await asyncio.to_thread(run_ffmpeg, stream)
        except ffmpeg.Error as e:
            print(e.stderr.decode())
            await update_status(status_msg, "❌ Error extracting audio.")
            return

        # 3. Transcribe with Whisper
        await update_status(status_msg, "ᴛʀᴀɴsᴄʀɪʙɪɴɢ ᴀᴜᴅɪᴏ...")
        result = await asyncio.to_thread(transcribe_audio, SESSIONS[user_id]["audio_path"])
        transcribed_text = result["text"].strip()

        if not transcribed_text:
            await update_status(status_msg, "❌ Could not detect any speech in the video.")
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
        await video_msg.reply_video(
            video=final_video_path,
            caption=f"✨ {action_str} Video Processed Successfully!"
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
