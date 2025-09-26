import os
import shlex
import subprocess
import uuid
import math
from pathlib import Path
from typing import List, Tuple

from telegram import Update, InputFile
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# Optional: Whisper import
try:
    import whisper
except Exception:
    whisper = None

# ---------- Configuration ----------
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WORKDIR = Path(os.environ.get('WORKDIR', './work')).absolute()
MAX_CLIP_SECONDS = 50

WORKDIR.mkdir(parents=True, exist_ok=True)

# ---------- Helper functions ----------

def run(cmd: List[str], capture_output=False):
    """Run a shell command (list form). Raises on error."""
    print('RUN:', ' '.join(shlex.quote(p) for p in cmd))
    proc = subprocess.run(cmd, capture_output=capture_output, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def download_with_ytdlp(url: str, out_dir: Path) -> Path:
    """Download video using yt-dlp to out_dir and return path to downloaded file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    filename_template = str(out_dir / '%(title).200s-%(id)s.%(ext)s')
    cmd = ['yt-dlp', '-f', 'bestvideo+bestaudio/best', '--merge-output-format', 'mp4', '-o', filename_template, url]
    run(cmd)
    # find latest file in out_dir
    files = sorted(out_dir.glob('*'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError('yt-dlp did not produce a file')
    return files[0]


def convert_to_9_16(input_path: Path, output_path: Path, target_height=1920):
    """Convert a video to 9:16 (portrait) by scaling and padding/cropping to keep content centered.
    This implementation scales width or height to match target aspect ratio and pads as needed.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Use ffmpeg to scale and pad to 9:16. We'll set width based on target_height.
    target_w = int(target_height * 9 / 16)
    # ffmpeg filter: scale preserving aspect, then pad/crop to target_w:target_h
    vf = (f"scale='if(gt(a,{9/16}),{target_w},-2)':'if(gt(a,{9/16}),-2,{target_height})',"
        f"pad={target_w}:{target_height}:(ow-iw)/2:(oh-ih)/2")
    cmd = ['ffmpeg', '-y', '-i', str(input_path), '-vf', vf, '-c:a', 'copy', str(output_path)]
    run(cmd)
    return output_path


def get_video_duration(path: Path) -> float:
    """Return duration in seconds using ffprobe."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError('ffprobe failed: ' + proc.stderr)
    return float(proc.stdout.strip())


def split_into_clips(input_path: Path, out_dir: Path, max_seconds=MAX_CLIP_SECONDS) -> List[Path]:
    """Split input video into clips each <= max_seconds. Returns list of clip paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = get_video_duration(input_path)
    clip_paths = []
    num_clips = math.ceil(duration / max_seconds)
    base = input_path.stem
    for i in range(num_clips):
        start = i * max_seconds
        length = min(max_seconds, duration - start)
        out_path = out_dir / f"{base}_part{i+1:03d}.mp4"
        cmd = ['ffmpeg', '-y', '-i', str(input_path), '-ss', str(start), '-t', str(length), '-c', 'copy', str(out_path)]
        run(cmd)
        clip_paths.append(out_path)
    return clip_paths


def transcribe_with_whisper(clip_path: Path, model_name='base') -> List[dict]:
    """Transcribe audio and return segments list with start, end, text.
    Requires the whisper package (openai/whisper).
    """
    if whisper is None:
        raise RuntimeError('Whisper package is not available. Install "whisper" (and torch).')
    model = whisper.load_model(model_name)
    print('Transcribing', clip_path)
    result = model.transcribe(str(clip_path))
    # result['segments'] is list with 'start','end','text'
    segments = result.get('segments', [])
    return segments


def write_srt(segments: List[dict], srt_path: Path):
    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with srt_path.open('w', encoding='utf-8') as f:
        for i, seg in enumerate(segments, start=1):
            start = fmt_time(seg['start'])
            end = fmt_time(seg['end'])
            text = seg['text'].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def burn_subtitles(clip_path: Path, srt_path: Path, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg subtitles filter requires libass; on many systems ffmpeg is built with it.
    cmd = ['ffmpeg', '-y', '-i', str(clip_path), '-vf', f"subtitles={shlex.quote(str(srt_path))}", str(out_path)]
    run(cmd)
    return out_path


# ---------- Telegram bot handlers ----------

def start(update: Update, context: CallbackContext):
    update.message.reply_text('Send me a video link (or a video file). I will convert it into 9:16 shorts (<=50s) with captions.')


def process_video_url(update: Update, context: CallbackContext):
    url = update.message.text.strip()
    chat_id = update.message.chat_id
    msg = update.message.reply_text('Got your link — starting processing. This may take some time.')
    try:
        session_id = uuid.uuid4().hex[:8]
        base_dir = WORKDIR / session_id
        downloads = base_dir / 'downloads'
        converted = base_dir / 'converted'
        clips = base_dir / 'clips'
        output = base_dir / 'output'
        downloads.mkdir(parents=True, exist_ok=True)

        # 1. Download
        update.message.reply_text('Downloading video...')
        input_video = download_with_ytdlp(url, downloads)

        # 2. Convert to 9:16
        update.message.reply_text('Converting to 9:16 portrait...')
        converted_path = converted / (input_video.stem + '_9_16.mp4')
        convert_to_9_16(input_video, converted_path)

        # 3. Split into <=50s clips
        update.message.reply_text('Splitting into clips <=50s...')
        clip_paths = split_into_clips(converted_path, clips, max_seconds=MAX_CLIP_SECONDS)

        generated = []
        for c in clip_paths:
            update.message.reply_text(f'Processing clip {c.name}...')
            # 4. Transcribe
            try:
                segments = transcribe_with_whisper(c)
                srt = output / f"{c.stem}.srt"
                write_srt(segments, srt)
                # 5. Burn subtitles
                out_clip = output / f"{c.stem}_captioned.mp4"
                burn_subtitles(c, srt, out_clip)
                generated.append(out_clip)
            except Exception as e:
                # If transcription fails, still send original clip
                print('Transcription error:', e)
                # fallback: copy clip to output
                fallback = output / f"{c.stem}_nocaptions.mp4"
                run(['cp', str(c), str(fallback)])
                generated.append(fallback)

        # 6. Send generated clips back to user
        update.message.reply_text(f'Uploading {len(generated)} clips...')
        for g in generated:
            with g.open('rb') as fh:
                context.bot.send_video(chat_id=chat_id, video=fh, supports_streaming=True, timeout=120)

        update.message.reply_text('Done. All clips generated and sent. Files are stored under: ' + str(base_dir))
    except Exception as exc:
        print('Error:', exc)
        update.message.reply_text('An error occurred: ' + str(exc))


def handle_video_file(update: Update, context: CallbackContext):
    # Accept video files sent directly to the bot (not URLs)
    chat_id = update.message.chat_id
    msg = update.message.reply_text('Got your file — downloading and processing...')
    try:
        session_id = uuid.uuid4().hex[:8]
        base_dir = WORKDIR / session_id
        downloads = base_dir / 'downloads'
        converted = base_dir / 'converted'
        clips = base_dir / 'clips'
        output = base_dir / 'output'
        downloads.mkdir(parents=True, exist_ok=True)

        # Download file from Telegram
        video = update.message.video or update.message.document
        file_obj = context.bot.get_file(video.file_id)
        local_path = downloads / (video.file_id + '.mp4')
        file_obj.download(custom_path=str(local_path))

        # Then process same as URL flow
        converted_path = converted / (local_path.stem + '_9_16.mp4')
        convert_to_9_16(local_path, converted_path)
        clip_paths = split_into_clips(converted_path, clips, max_seconds=MAX_CLIP_SECONDS)
        generated = []
        for c in clip_paths:
            try:
                segments = transcribe_with_whisper(c)
                srt = output / f"{c.stem}.srt"
                write_srt(segments, srt)
                out_clip = output / f"{c.stem}_captioned.mp4"
                burn_subtitles(c, srt, out_clip)
                generated.append(out_clip)
            except Exception as e:
                print('Transcription error:', e)
                fallback = output / f"{c.stem}_nocaptions.mp4"
                run(['cp', str(c), str(fallback)])
                generated.append(fallback)

        update.message.reply_text(f'Uploading {len(generated)} clips...')
        for g in generated:
            with g.open('rb') as fh:
                context.bot.send_video(chat_id=chat_id, video=fh, supports_streaming=True, timeout=120)
        update.message.reply_text('Done. Files stored under: ' + str(base_dir))
    except Exception as exc:
        print('Error handling file:', exc)
        update.message.reply_text('Error: ' + str(exc))


# ---------- Main ----------

def main():
    if not TELEGRAM_BOT_TOKEN:
        print('Please set TELEGRAM_BOT_TOKEN environment variable.')
        return
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.entity('url') & Filters.text, process_video_url))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, process_video_url))
    dp.add_handler(MessageHandler(Filters.video | Filters.document.category('video'), handle_video_file))
    print('Bot starting...')
    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()



