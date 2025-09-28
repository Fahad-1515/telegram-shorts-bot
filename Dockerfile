# ----------------- Dockerfile -----------------
# Telegram Shorts Bot - Build an image with ffmpeg, yt-dlp, and Python deps

FROM python:3.10-slim

# Install system dependencies (minimal, no extras)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m pip install --upgrade pip
# Copy bot code
COPY telegram_shorts_bot.py .

# Environment variables (overridable at runtime)
ENV TELEGRAM_BOT_TOKEN=""
ENV WORKDIR="/app/work"

# Create persistent workdir
RUN mkdir -p /app/work

# Default command: start the bot
CMD ["python", "telegram_shorts_bot.py"]
