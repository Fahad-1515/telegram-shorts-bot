# Telegram Shorts Bot 🎬🤖

This bot automates turning YouTube (or uploaded) videos into **9:16 portrait shorts** of ≤50 seconds each, and send back the Clips after Generated.

---

#11min tested

## ✨ Features

- Download videos via **yt-dlp** or accept direct uploads.
- Convert videos to **9:16 portrait** (for TikTok, Reels, Shorts).
- Split into clips ≤50 seconds.
- Return clips directly in Telegram.

---

## 🚀 Quickstart (Local)

### 1. Install system dependencies

```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-venv python3-pip git build-essential
```

---

..venv\Scripts\Activate.ps1

winget install ffmpeg

ffmpeg -version

pip install python-telegram-bot yt-dlp openai-whisper ffmpeg-python
pip install "urllib3<2"

$env:TELEGRAM_BOT_TOKEN="your tokem"

python .\telegram_shorts_bot.py

================= docker-compose down

docker-compose build --no-cache docker-compose up -d docker-compose logs -f

python3 -m venv venv

docker-compose logs
