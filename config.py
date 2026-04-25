import os

class Config:
    # Telegram Bot Token from BotFather
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "8432154170:AAEcYvbTaEeH9ZdctxXtc0ZEPHeH-COHNb0")

    # Your Telegram User ID
    OWNER_ID = 7660990923

    # Telegram API ID and API HASH from my.telegram.org
    TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API", "0"))
    TELEGRAM_API_HASH = os.environ.get("TELEGRAM_HASH", "")
