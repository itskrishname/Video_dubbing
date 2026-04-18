# Automated Video Dubbing Telegram Bot

This is a Telegram Bot designed to automatically dub videos into Hindi or English. It extracts the audio, transcribes it using OpenAI's Whisper, translates it (if Hindi is selected), synthesizes AI speech using Edge TTS, and syncs the new audio to the original video using FFMPEG.

## Features
- **Authorized Access:** Only the specified `OWNER_ID` can use the bot.
- **Language Selection:** Inline keyboard to choose between Hindi and English dubbing.
- **AI Transcriptions & Voice:** Uses Whisper for high-quality English transcriptions and Edge TTS for natural-sounding voices.
- **Audio Sync:** Adjusts the generated TTS audio speed to match the original video duration using FFMPEG.
- **Heroku Optimized:** Cleans up temporary files strictly to avoid exceeding Heroku's ephemeral storage limits.

## Environment Variables

To run this bot, you must set the following environment variables:

| Variable | Description | Default |
| --- | --- | --- |
| `BOT_TOKEN` | The bot token obtained from [@BotFather](https://t.me/BotFather) | *None* (Required) |
| `OWNER_ID` | Your Telegram User ID to restrict access | *None* (Required) |
| `TELEGRAM_API` | Your API ID from [my.telegram.org](https://my.telegram.org) | *Pre-configured* |
| `TELEGRAM_HASH` | Your API Hash from [my.telegram.org](https://my.telegram.org) | *Pre-configured* |

## VPS Deployment (Docker Recommended)

Since this bot performs heavy operations using AI and FFMPEG, it is highly recommended to run it on a VPS (Ubuntu/Debian) rather than Heroku.

**1. Install Docker & Docker Compose on your VPS:**
```bash
sudo apt update
sudo apt install docker.io docker-compose -y
```

**2. Clone the repository and navigate to it:**
```bash
git clone <your-repo-url>
cd <repo-folder>
```

**3. Configure your Bot:**
- Open `config.py` and ensure your `BOT_TOKEN` is set, or define it in the `docker-compose.yml` file under the `environment:` section.

**4. Start the Bot:**
Run the following command to build the image and start the bot in the background:
```bash
sudo docker-compose up --build -d
```

To view the live logs of the bot:
```bash
sudo docker logs -f tg-video-bot
```

## Legacy Heroku Deployment

While you can deploy this on Heroku by adding the `heroku/python` and `https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git` buildpacks, the RAM limits on free/eco tiers (512MB) will frequently crash `openai-whisper` during large video transcriptions. Docker on a VPS is the superior method.
