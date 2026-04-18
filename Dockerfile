FROM python:3.11-slim

# Set environment variables to avoid writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install required system dependencies: FFMPEG and Git
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       git \
       wget \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirement files first to leverage Docker cache
COPY requirements.txt /app/

# Upgrade pip and install dependencies
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . /app/

# Command to run the bot
CMD ["python3", "bot.py"]
