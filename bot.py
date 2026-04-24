import os
import sys
import asyncio
import time
import ffmpeg
import whisper
import edge_tts
from pdf2image import convert_from_path
import pytesseract
from PIL import Image, ImageDraw, ImageFont
import textwrap
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
    ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)

def transcribe_audio(audio_path):
    import torch
    # Auto-detect if CUDA is available on the VPS, otherwise fallback to CPU-friendly fp16=False
    fp16 = torch.cuda.is_available()
    return model.transcribe(audio_path, language="en", fp16=fp16)

async def translate_text(text, target_lang):
    if target_lang == "en" or not text.strip():
        return text

    if target_lang == "hinglish":
        # Standard Transliteration fallback: 100% reliable but sounds like formal "Shuddh" Hindi
        try:
            hindi_text = await asyncio.to_thread(GoogleTranslator(source='auto', target='hi').translate, text)
            if not hindi_text:
                return text
            hinglish_text = sanscript.transliterate(hindi_text, sanscript.DEVANAGARI, sanscript.ITRANS)
            return hinglish_text.capitalize()
        except Exception as e:
            print(f"Hinglish Transliteration Error: {e}")
            return text

    # Translate to Hindi Devanagari
    try:
        translated = await asyncio.to_thread(GoogleTranslator(source='auto', target='hi').translate, text)
        return translated if translated else text
    except Exception as e:
        print(f"Google Translate Error: {e}")
        return text

async def translate_segments_to_srt_string(segments, target_lang):
    srt_content = ""
    # Concurrency limit to prevent free API rate limits / dropped subtitles
    semaphore = asyncio.Semaphore(5)

    async def process_segment(i, segment):
        start = format_timestamp(segment["start"])
        end = format_timestamp(segment["end"])
        text = segment["text"].strip()

        async with semaphore:
            if target_lang != "en" and text:
                text = await translate_text(text, target_lang)

        return f"{i}\n{start} --> {end}\n{text}\n\n"

    # Process all segments concurrently
    tasks = [process_segment(i, seg) for i, seg in enumerate(segments, start=1)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            print(f"Translation Error in segment: {r}")
        else:
            srt_content += r

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

def get_subtitle_streams(filename):
    try:
        probe = ffmpeg.probe(filename)
        subtitle_streams = [stream for stream in probe['streams'] if stream['codec_type'] == 'subtitle']
        return subtitle_streams
    except ffmpeg.Error as e:
        print(f"ffprobe error: {e.stderr.decode()}")
        return []

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

        # Force exit to avoid Pyrogram "Task cannot await on itself" deadlock.
        # Docker/systemd will instantly restart the process.
        os._exit(0)

    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to update: {str(e)}")

@app.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    if message.document.mime_type != "application/pdf":
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇮🇳 Hindi", callback_data="pdflang_hi"),
            InlineKeyboardButton("🇺🇸 English", callback_data="pdflang_en")
        ],
        [
            InlineKeyboardButton("🇮🇳🇺🇸 Hinglish", callback_data="pdflang_hinglish")
        ]
    ])

    await message.reply_text(
        "📄 PDF received! Which language would you like to translate this document into?",
        reply_markup=keyboard,
        quote=True
    )

@app.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    if message.from_user.id != Config.OWNER_ID:
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Acknowledge and download briefly to probe for subtitles
    status_msg = await message.reply_text("✦ ᴄʜᴇᴄᴋɪɴɢ ᴠɪᴅᴇᴏ ɪɴꜰᴏ... ✦", quote=True)

    user_id = message.from_user.id
    timestamp = int(time.time())
    orig_video_path = f"/tmp/input_{timestamp}.mp4"

    # Store initial session path
    SESSIONS[user_id] = {
        "timestamp": timestamp,
        "video_msg": message,
        "status_msg": status_msg,
        "orig_video_path": orig_video_path
    }

    start_time = time.time()
    await message.download(
        file_name=orig_video_path,
        progress=progress_callback,
        progress_args=(status_msg, start_time, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ ꜰᴏʀ ᴀɴᴀʟʏsɪs")
    )

    await update_status(status_msg, "ᴀɴᴀʟʏᴢɪɴɢ ᴠɪᴅᴇᴏ...")
    subs = await asyncio.to_thread(get_subtitle_streams, orig_video_path)

    keyboard_buttons = []

    if subs:
        for i, sub in enumerate(subs):
            lang = sub.get('tags', {}).get('language', f'Track {i}')
            keyboard_buttons.append([InlineKeyboardButton(f"📝 Extract Subtitles ({lang.upper()})", callback_data=f"extract_{i}")])

    keyboard_buttons.append([InlineKeyboardButton("🎙️ AI Dub Video", callback_data="menu_dub")])
    keyboard_buttons.append([InlineKeyboardButton("📝 AI Subtitle Video", callback_data="menu_sub")])

    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    await status_msg.edit_text(
        "🎥 Video received & analyzed!\n\nWould you like to extract existing subtitles or use AI Whisper to generate new ones?",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^pdflang_"))
async def process_pdf_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    lang_code = callback_query.data.split("_")[1]

    await callback_query.answer()
    status_msg = callback_query.message
    doc_msg = status_msg.reply_to_message

    if not doc_msg or not doc_msg.document:
        await update_status(status_msg, "❌ Error: Could not find the original PDF.")
        return

    await update_status(status_msg, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴘᴅꜰ...")

    timestamp = int(time.time())
    orig_pdf_path = f"/tmp/input_{timestamp}.pdf"
    new_pdf_path = f"/tmp/translated_{timestamp}.pdf"

    try:
        start_time = time.time()
        await doc_msg.download(
            file_name=orig_pdf_path,
            progress=progress_callback,
            progress_args=(status_msg, start_time, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴘᴅꜰ")
        )

        await update_status(status_msg, "ᴘʀᴏᴄᴇssɪɴɢ ᴘᴅꜰ ᴠɪsᴜᴀʟʟʏ (ᴏᴄʀ)... ᴛʜɪs ᴡɪʟʟ ᴛᴀᴋᴇ ᴛɪᴍᴇ ⏳")

        async def process_visual_pdf():
            images = convert_from_path(orig_pdf_path)
            font_path = os.path.abspath("fonts/Mukta.ttf")
            try:
                font = ImageFont.truetype(font_path, 20)
            except:
                font = ImageFont.load_default()

            processed_images = []

            for img in images:
                draw = ImageDraw.Draw(img)
                # Use pytesseract to get data dict
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

                # Group words into blocks to maintain context for translation
                blocks = {}
                for i in range(len(data['text'])):
                    if int(data['conf'][i]) > 30 and data['text'][i].strip() != '':
                        block_num = data['block_num'][i]
                        if block_num not in blocks:
                            blocks[block_num] = {
                                'text': [],
                                'x': data['left'][i],
                                'y': data['top'][i],
                                'w': 0,
                                'h': 0,
                                'max_right': 0,
                                'max_bottom': 0
                            }

                        b = blocks[block_num]
                        b['text'].append(data['text'][i])
                        b['x'] = min(b['x'], data['left'][i])
                        b['y'] = min(b['y'], data['top'][i])
                        b['max_right'] = max(b['max_right'], data['left'][i] + data['width'][i])
                        b['max_bottom'] = max(b['max_bottom'], data['top'][i] + data['height'][i])

                # Translate and draw blocks
                for b_id, b in blocks.items():
                    original_text = " ".join(b['text'])
                    if not original_text.strip():
                        continue

                    b['w'] = b['max_right'] - b['x']
                    b['h'] = b['max_bottom'] - b['y']

                    # Draw white rectangle over the original text
                    draw.rectangle([b['x'], b['y'], b['max_right'], b['max_bottom']], fill="white")

                # We need to gather translations concurrently so it doesn't take hours
                # Extract all text blocks
                block_list = list(blocks.values())

                # Fetch translations using semaphore
                sem = asyncio.Semaphore(5)
                async def fetch_trans(text):
                    async with sem:
                        return await translate_text(text, lang_code)

                tasks = [fetch_trans(" ".join(b['text'])) for b in block_list]
                translated_texts = await asyncio.gather(*tasks, return_exceptions=True)

                for idx, b in enumerate(block_list):
                    t_text = translated_texts[idx]
                    if isinstance(t_text, Exception):
                        t_text = " ".join(b['text'])

                    # Wrap text to fit the bounding box
                    # Approximate chars per line based on box width and font size (avg 10px per char)
                    chars_per_line = max(10, int(b['w'] / 10))
                    wrapped_text = textwrap.fill(t_text, width=chars_per_line)

                    draw.text((b['x'], b['y']), wrapped_text, fill="black", font=font)

                processed_images.append(img)

            if processed_images:
                processed_images[0].save(
                    new_pdf_path, "PDF" ,resolution=100.0, save_all=True, append_images=processed_images[1:]
                )
            else:
                raise Exception("Failed to process any images from PDF.")


        def run_visual_sync():
            # Create a new event loop for the background thread to handle async gathers
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(process_visual_pdf())
            loop.close()

        await asyncio.to_thread(run_visual_sync)

        await update_status(status_msg, "ᴜᴘʟᴏᴀᴅɪɴɢ ᴛʀᴀɴsʟᴀᴛᴇᴅ ᴘᴅꜰ...")
        start_time = time.time()

        await doc_msg.reply_document(
            document=new_pdf_path,
            caption="✨ Document Translated Successfully!",
            progress=progress_callback,
            progress_args=(status_msg, start_time, "ᴜᴘʟᴏᴀᴅɪɴɢ ᴘᴅꜰ")
        )
        await status_msg.delete()

    except Exception as e:
        print(f"Error processing PDF: {e}")
        await update_status(status_msg, f"❌ An error occurred: {str(e)}")
    finally:
        for p in [orig_pdf_path, new_pdf_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass

@app.on_callback_query(filters.regex("^(menu|extract)_"))
async def main_menu_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    data = callback_query.data
    user_id = callback_query.from_user.id

    if data.startswith("extract_"):
        track_id = data.split("_")[1]
        SESSIONS[user_id]["extract_track"] = track_id
        action = "sub"
    else:
        action = data.split("_")[1] # 'dub' or 'sub'
        if "extract_track" in SESSIONS.get(user_id, {}):
            del SESSIONS[user_id]["extract_track"]

    # Save intent
    if user_id in SESSIONS:
        SESSIONS[user_id]["action"] = action

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇮🇳 Hindi", callback_data=f"langbtn_hi"),
            InlineKeyboardButton("🇺🇸 English", callback_data=f"langbtn_en")
        ],
        [
            InlineKeyboardButton("🇮🇳🇺🇸 Hinglish", callback_data=f"langbtn_hinglish")
        ]
    ])

    action_text = "Dubbing" if action == "dub" else "Subtitles"
    if "extract_track" in SESSIONS.get(user_id, {}):
        action_text = "Extracted Subtitles Translation"

    await callback_query.message.edit_text(
        f"Select the language for **{action_text}**:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^langbtn_"))
async def process_video_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id != Config.OWNER_ID:
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    lang_code = callback_query.data.split("_")[1]
    user_id = callback_query.from_user.id

    if user_id not in SESSIONS:
        await callback_query.answer("❌ Session expired.", show_alert=True)
        return

    SESSIONS[user_id]["lang_code"] = lang_code
    action = SESSIONS[user_id]["action"]

    await callback_query.answer()
    status_msg = callback_query.message
    video_msg = SESSIONS[user_id]["video_msg"]

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
    user_id = video_msg.from_user.id

    # Update existing session info (preserving extract_track and orig_video_path)
    session = SESSIONS.get(user_id, {})
    timestamp = session.get("timestamp", int(time.time()))

    session.update({
        "timestamp": timestamp,
        "video_msg": video_msg,
        "status_msg": status_msg,
        "action": action,
        "lang_code": lang_code,
        "audio_path": f"/tmp/audio_{timestamp}.wav",
        "transcription_result": None
    })

    # In case there was no pre-existing session/download
    if "orig_video_path" not in session:
        session["orig_video_path"] = f"/tmp/input_{timestamp}.mp4"

    SESSIONS[user_id] = session

    try:
        # 1. Download Video (if not already downloaded during analysis)
        if not os.path.exists(SESSIONS[user_id]["orig_video_path"]):
            start_time = time.time()
            await video_msg.download(
                file_name=SESSIONS[user_id]["orig_video_path"],
                progress=progress_callback,
                progress_args=(status_msg, start_time, "ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ")
            )

        extract_track = SESSIONS[user_id].get("extract_track")

        if extract_track:
            # --- Extract Subtitles via FFMPEG ---
            await update_status(status_msg, "ᴇxᴛʀᴀᴄᴛɪɴɢ sᴜʙᴛɪᴛʟᴇs ꜰʀᴏᴍ ᴠɪᴅᴇᴏ...")
            SESSIONS[user_id]["srt_path"] = f"/tmp/extracted_{timestamp}.srt"

            try:
                stream = ffmpeg.input(SESSIONS[user_id]["orig_video_path"]).output(SESSIONS[user_id]["srt_path"], map=f"0:s:{extract_track}", threads=0)
                await asyncio.to_thread(run_ffmpeg, stream)
            except ffmpeg.Error as e:
                print(e.stderr.decode())
                await update_status(status_msg, "❌ Error extracting subtitle track.")
                cleanup_session(user_id)
                return

            # Parse SRT into whisper-like segments format so our translation logic works seamlessly
            await update_status(status_msg, "ᴘᴀʀsɪɴɢ ᴇxᴛʀᴀᴄᴛᴇᴅ sᴜʙᴛɪᴛʟᴇs...")
            segments = []
            full_text = ""
            if os.path.exists(SESSIONS[user_id]["srt_path"]):
                with open(SESSIONS[user_id]["srt_path"], "r", encoding="utf-8") as f:
                    content = f.read()
                    import re
                    blocks = re.split(r'\n\s*\n', content)
                    for block in blocks:
                        lines = block.split('\n')
                        if len(lines) >= 3:
                            time_line = lines[1]
                            text_lines = " ".join(lines[2:])

                            # Parse SRT timestamp to seconds for whisper format compatibility
                            # 00:00:01,000 --> 00:00:04,000
                            try:
                                start_str, end_str = time_line.split(" --> ")
                                def parse_ts(ts):
                                    h, m, s_ms = ts.split(":")
                                    s, ms = s_ms.split(",")
                                    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000.0

                                segments.append({
                                    "start": parse_ts(start_str),
                                    "end": parse_ts(end_str),
                                    "text": text_lines
                                })
                                full_text += text_lines + " "
                            except Exception as e:
                                pass

            if not segments:
                await update_status(status_msg, "❌ Could not extract any text from the subtitle track.")
                cleanup_session(user_id)
                return

            SESSIONS[user_id]["transcription_result"] = {"segments": segments, "text": full_text.strip()}
            SESSIONS[user_id]["transcribed_text"] = full_text.strip()
        else:
            # --- Extract Audio and Transcribe via Whisper ---
            # 2. Extract Audio
            await update_status(status_msg, "ᴇxᴛʀᴀᴄᴛɪɴɢ ᴀᴜᴅɪᴏ...")
            try:
                stream = ffmpeg.input(SESSIONS[user_id]["orig_video_path"]).output(SESSIONS[user_id]["audio_path"], acodec='pcm_s16le', ac=1, ar='16k', threads=0)
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
            translated_script = await translate_segments_to_srt_string(segments, lang_code)
            session["final_srt_content"] = translated_script

            # Show a preview of the first few lines to the user
            preview_text = "\n".join(translated_script.split("\n")[:15]) + "\n... (truncated)"
        else:
            # For dubbing, generate full translated text
            transcribed_text = session["transcribed_text"]
            translated_script = await translate_text(transcribed_text, lang_code)
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

                stream = ffmpeg.output(video_stream, audio_stream, final_video_path, vcodec='libx264', acodec='copy', threads=0)
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

                    stream = ffmpeg.output(stream, synced_audio_path, ar=44100, threads=0)
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
                stream = ffmpeg.output(video_input.video, audio_input.audio, final_video_path, vcodec='copy', acodec='aac', strict='experimental', threads=0)
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
