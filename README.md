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

## Deployment on Heroku

Follow these steps to deploy the bot to Heroku:

1. **Create a new Heroku App.**
2. **Add Buildpacks:**
   Go to your app's "Settings" > "Buildpacks" and add the following in this order:
   - `heroku/python` (Official Python buildpack)
   - `https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git` (Third-party FFMPEG buildpack)
3. **Configure Environment Variables:**
   Go to "Settings" > "Config Vars" and add the required variables (`BOT_TOKEN`, `OWNER_ID`, `TELEGRAM_API`, `TELEGRAM_HASH`).
4. **Deploy the Code:**
   Connect your GitHub repository and deploy the branch, or use the Heroku CLI to push the code.
5. **Start the Worker Dyno:**
   Go to the "Resources" tab and toggle the `worker` dyno to ON.

## Note on Memory Limits
This bot uses `openai-whisper` and `ffmpeg`. The `base` whisper model uses around ~500MB of RAM. If you are using Heroku's Eco or Basic dynos (512MB RAM), the bot might occasionally crash due to memory constraints when processing larger videos. It is recommended to test with short clips first.
