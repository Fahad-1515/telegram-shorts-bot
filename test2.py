import os
import shlex
import subprocess
import uuid
import math
import re
from pathlib import Path
from typing import List, Tuple, Optional
from datetime import datetime

from telegram import Update, InputFile
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# Optional: Whisper import
try:
    import whisper
except ImportError:
    whisper = None

# ---------- Configuration ----------
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WORKDIR = Path(os.environ.get('WORKDIR', './work')).absolute()
MAX_CLIP_SECONDS = 50
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
MAX_VIDEO_DURATION = 3600  # 1 hour
MAX_VIDEO_SIZE_MB = 50  # Telegram file size limit

# Create work directory
WORKDIR.mkdir(parents=True, exist_ok=True)

# ---------- Helper functions ----------

def sanitize_filename(filename: str) -> str:
    """Prevent path traversal attacks and sanitize filenames."""
    # Remove path components and keep only safe characters
    clean_name = re.sub(r'[^a-zA-Z0-9\-_\.]', '_', os.path.basename(filename))
    return clean_name[:100]  # Limit length

def validate_video_file(path: Path) -> Tuple[bool, str]:
    """Check if video is within acceptable limits."""
    try:
        if not path.exists():
            return False, "File does not exist"
        
        if path.stat().st_size > MAX_FILE_SIZE:
            return False, f"File too large ({path.stat().st_size / 1024 / 1024:.1f}MB > {MAX_FILE_SIZE / 1024 / 1024:.1f}MB)"
        
        duration = get_video_duration(path)
        if duration > MAX_VIDEO_DURATION:
            return False, f"Video too long ({duration:.1f}s > {MAX_VIDEO_DURATION}s)"
        
        return True, "Valid"
    except Exception as e:
        return False, f"Validation error: {str(e)}"

def run(cmd: List[str], capture_output=False, timeout=300):
    """Run a shell command (list form). Raises on error."""
    print('RUN:', ' '.join(shlex.quote(p) for p in cmd))
    try:
        proc = subprocess.run(cmd, capture_output=capture_output, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout} seconds: {' '.join(cmd)}")

def download_with_ytdlp(url: str, out_dir: Path) -> Path:
    """Download video using yt-dlp to out_dir and return path to downloaded file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    filename_template = str(out_dir / '%(title).100s-%(id)s.%(ext)s')
    cmd = [
        'yt-dlp', 
        '-f', 'best[height<=1080]/best',  # Prefer 1080p or lower
        '--merge-output-format', 'mp4', 
        '--no-playlist',
        '-o', filename_template, 
        url
    ]
    run(cmd)
    
    # Find the downloaded file
    files = sorted(out_dir.glob('*'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError('yt-dlp did not produce a file')
    
    # Validate the downloaded file
    is_valid, message = validate_video_file(files[0])
    if not is_valid:
        raise RuntimeError(f"Downloaded video validation failed: {message}")
    
    return files[0]

def get_video_dimensions(path: Path) -> Tuple[int, int]:
    """Get video width and height."""
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', 
           '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)]
    proc = run(cmd, capture_output=True)
    width, height = map(int, proc.stdout.strip().split(','))
    return width, height

def convert_to_9_16_enhanced(input_path: Path, output_path: Path, target_height=1920):
    """Enhanced conversion to 9:16 with better handling of different aspect ratios."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        width, height = get_video_dimensions(input_path)
        aspect_ratio = width / height
        
        target_width = int(target_height * 9 / 16)
        
        # Choose the best method based on original aspect ratio
        if aspect_ratio > 1.7:  # Very wide content (cinematic)
            # Crop sides to focus on center - preferred for YouTube Shorts
            vf = f"crop=ih*9/16:ih:({width}-ih*9/16)/2:0,scale={target_width}:{target_height}:flags=lanczos"
        elif aspect_ratio < 0.7:  # Very tall content (already vertical)
            # Scale to fit width, pad sides if needed
            vf = f"scale={target_width}:-2:flags=lanczos,pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black"
        else:  # Standard widescreen (16:9) - use blurred background method
            vf = (f"split=2[bg][vid];[bg]scale={target_width}:{target_height}:flags=lanczos,gblur=sigma=20[bg];"
                  f"[vid]scale=-2:{target_height}:flags=lanczos[vid];[bg][vid]overlay=(W-w)/2:(H-h)/2")
        
        cmd = [
            'ffmpeg', '-y', '-i', str(input_path), 
            '-vf', vf,
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            '-f', 'mp4', str(output_path)
        ]
        run(cmd)
        return output_path
    except Exception as e:
        # Fallback to simple conversion if enhanced method fails
        print(f"Enhanced conversion failed, using fallback: {e}")
        return convert_to_9_16_fallback(input_path, output_path, target_height)

def convert_to_9_16_fallback(input_path: Path, output_path: Path, target_height=1920):
    """Fallback conversion method."""
    target_width = int(target_height * 9 / 16)
    vf = f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease:flags=lanczos,pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black"
    
    cmd = [
        'ffmpeg', '-y', '-i', str(input_path), 
        '-vf', vf,
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart', str(output_path)
    ]
    run(cmd)
    return output_path

def get_video_duration(path: Path) -> float:
    """Return duration in seconds using ffprobe."""
    cmd = [
        'ffprobe', '-v', 'error', 
        '-show_entries', 'format=duration', 
        '-of', 'default=noprint_wrappers=1:nokey=1', 
        str(path)
    ]
    proc = run(cmd, capture_output=True)
    return float(proc.stdout.strip())

def split_into_clips(input_path: Path, out_dir: Path, max_seconds=MAX_CLIP_SECONDS) -> List[Path]:
    """Split input video into clips each <= max_seconds. Returns list of clip paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = get_video_duration(input_path)
    
    if duration <= max_seconds:
        # No splitting needed, just copy
        out_path = out_dir / f"{input_path.stem}_full.mp4"
        run(['cp', str(input_path), str(out_path)])
        return [out_path]
    
    clip_paths = []
    num_clips = math.ceil(duration / max_seconds)
    base = sanitize_filename(input_path.stem)
    
    for i in range(num_clips):
        start = i * max_seconds
        length = min(max_seconds, duration - start)
        out_path = out_dir / f"{base}_part{i+1:02d}.mp4"
        
        cmd = [
            'ffmpeg', '-y', '-i', str(input_path), 
            '-ss', str(start), '-t', str(length),
            '-c', 'copy',  # Stream copy for speed
            '-avoid_negative_ts', 'make_zero',
            str(out_path)
        ]
        run(cmd)
        clip_paths.append(out_path)
    
    return clip_paths

def optimize_for_shorts(input_path: Path, output_path: Path):
    """Apply Shorts-specific optimizations."""
    cmd = [
        'ffmpeg', '-y', '-i', str(input_path),
        '-c:v', 'libx264', '-profile:v', 'main', '-level', '3.1',
        '-crf', '21', '-preset', 'fast',  # Slightly better quality for Shorts
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        '-vf', 'fps=30,format=yuv420p',  # Standard Shorts settings
        '-max_muxing_queue_size', '1024',
        str(output_path)
    ]
    run(cmd)
    return output_path

def transcribe_with_whisper(clip_path: Path, model_name='base') -> List[dict]:
    """Transcribe audio and return segments list with start, end, text."""
    if whisper is None:
        raise RuntimeError('Whisper package is not available. Install "openai-whisper" (and torch).')
    
    try:
        model = whisper.load_model(model_name)
        print(f'Transcribing {clip_path}')
        result = model.transcribe(
            str(clip_path),
            fp16=False,  # More compatible
            verbose=True
        )
        segments = result.get('segments', [])
        return segments
    except Exception as e:
        raise RuntimeError(f"Whisper transcription failed: {str(e)}")

def write_srt(segments: List[dict], srt_path: Path):
    """Write segments to SRT subtitle file."""
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
    """Burn subtitles into video with nice styling."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Enhanced subtitle styling for better readability
    subtitle_style = (
        "force_style="
        "FontName=Arial,"
        "FontSize=24,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BackColour=&H80000000,"
        "Bold=1,"
        "MarginV=50"
    )
    
    cmd = [
        'ffmpeg', '-y', '-i', str(clip_path),
        '-vf', f"subtitles={shlex.quote(str(srt_path))}:{subtitle_style}",
        '-c:a', 'copy', str(out_path)
    ]
    run(cmd)
    return out_path

def cleanup_old_files(max_age_hours=24):
    """Clean up files older than specified hours."""
    try:
        cutoff_time = datetime.now().timestamp() - (max_age_hours * 3600)
        for item in WORKDIR.glob('*'):
            if item.is_dir() and item.stat().st_mtime < cutoff_time:
                import shutil
                shutil.rmtree(item)
                print(f"Cleaned up old directory: {item}")
    except Exception as e:
        print(f"Cleanup error: {e}")

# ---------- Telegram bot handlers ----------

def start(update: Update, context: CallbackContext):
    """Handle /start command."""
    help_text = """
🎬 *YouTube Shorts Converter Bot*

*Send me:*
• A YouTube/TikTok/Instagram video URL
• Or a video file (up to 50MB)

*I will:*
1. Convert to vertical 9:16 format
2. Split into ≤50 second clips
3. Add automatic captions
4. Optimize for YouTube Shorts

*Commands:*
/start - Show this help
/cleanup - Clean up old files (admin)

Just send me a video link or file to get started!
    """
    update.message.reply_text(help_text, parse_mode='Markdown')

def cleanup(update: Update, context: CallbackContext):
    """Handle /cleanup command."""
    # Basic admin check (you might want to enhance this)
    if update.effective_user.id not in [123456789]:  # Replace with your user ID
        update.message.reply_text("❌ Only admin can use this command.")
        return
    
    try:
        cleanup_old_files()
        update.message.reply_text("✅ Cleanup completed.")
    except Exception as e:
        update.message.reply_text(f"❌ Cleanup failed: {str(e)}")

def process_video_url(update: Update, context: CallbackContext):
    """Process video from URL."""
    url = update.message.text.strip()
    chat_id = update.message.chat_id
    
    # Basic URL validation
    if not re.match(r'^https?://', url):
        update.message.reply_text("❌ Please provide a valid URL starting with http:// or https://")
        return
    
    status_msg = update.message.reply_text('📥 Got your link — starting processing...')
    
    try:
        session_id = uuid.uuid4().hex[:8]
        base_dir = WORKDIR / session_id
        downloads = base_dir / 'downloads'
        converted = base_dir / 'converted'
        clips = base_dir / 'clips'
        output = base_dir / 'output'
        
        downloads.mkdir(parents=True, exist_ok=True)

        # 1. Download
        status_msg.edit_text('📥 Downloading video...')
        input_video = download_with_ytdlp(url, downloads)

        # Process the video
        return process_video_file(update, context, input_video, base_dir, status_msg)
        
    except Exception as exc:
        error_msg = f"❌ Processing failed: {str(exc)}"
        print(f"Error processing URL: {exc}")
        status_msg.edit_text(error_msg)

def handle_video_file(update: Update, context: CallbackContext):
    """Handle video files sent directly to the bot."""
    chat_id = update.message.chat_id
    
    # Check file size
    video = update.message.video or update.message.document
    if video.file_size > MAX_VIDEO_SIZE_MB * 1024 * 1024:
        update.message.reply_text(f"❌ File too large. Maximum size is {MAX_VIDEO_SIZE_MB}MB.")
        return
    
    status_msg = update.message.reply_text('📥 Downloading your video...')
    
    try:
        session_id = uuid.uuid4().hex[:8]
        base_dir = WORKDIR / session_id
        downloads = base_dir / 'downloads'
        downloads.mkdir(parents=True, exist_ok=True)

        # Download file from Telegram
        file_obj = context.bot.get_file(video.file_id)
        local_path = downloads / f"{video.file_id}.mp4"
        file_obj.download(custom_path=str(local_path))

        # Validate the downloaded file
        is_valid, message = validate_video_file(local_path)
        if not is_valid:
            raise RuntimeError(f"Invalid video file: {message}")

        # Process the video
        return process_video_file(update, context, local_path, base_dir, status_msg)
        
    except Exception as exc:
        error_msg = f"❌ Processing failed: {str(exc)}"
        print(f"Error handling file: {exc}")
        status_msg.edit_text(error_msg)

def process_video_file(update: Update, context: CallbackContext, input_video: Path, base_dir: Path, status_msg):
    """Common processing pipeline for both URLs and files."""
    chat_id = update.message.chat_id
    converted = base_dir / 'converted'
    clips = base_dir / 'clips'
    output = base_dir / 'output'
    
    try:
        # 2. Convert to 9:16
        status_msg.edit_text('🔄 Converting to vertical format...')
        converted_path = converted / f"{input_video.stem}_9_16.mp4"
        convert_to_9_16_enhanced(input_video, converted_path)

        # 3. Split into clips
        status_msg.edit_text('✂️ Splitting into Shorts-friendly clips...')
        clip_paths = split_into_clips(converted_path, clips, max_seconds=MAX_CLIP_SECONDS)

        if not clip_paths:
            raise RuntimeError("No clips were generated")

        generated = []
        
        for i, clip_path in enumerate(clip_paths):
            status_msg.edit_text(f'🎙️ Processing clip {i+1}/{len(clip_paths)} (transcribing)...')
            
            try:
                # 4. Transcribe
                segments = transcribe_with_whisper(clip_path)
                srt_path = output / f"{clip_path.stem}.srt"
                write_srt(segments, srt_path)
                
                # 5. Burn subtitles
                captioned_path = output / f"{clip_path.stem}_captioned.mp4"
                burn_subtitles(clip_path, srt_path, captioned_path)
                
                # 6. Final optimization
                final_path = output / f"{clip_path.stem}_final.mp4"
                optimize_for_shorts(captioned_path, final_path)
                
                generated.append(final_path)
                
            except Exception as e:
                print(f'Error processing clip {clip_path}: {e}')
                # Fallback: use original clip without captions
                fallback_path = output / f"{clip_path.stem}_nocaptions.mp4"
                optimize_for_shorts(clip_path, fallback_path)
                generated.append(fallback_path)

        # 7. Send results
        status_msg.edit_text(f'📤 Uploading {len(generated)} clip(s)...')
        
        for i, final_clip in enumerate(generated):
            status_msg.edit_text(f'📤 Uploading clip {i+1}/{len(generated)}...')
            
            with final_clip.open('rb') as fh:
                context.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    supports_streaming=True,
                    timeout=120,
                    caption=f"Short #{i+1} (Generated by YouTube Shorts Bot)"
                )
        
        status_msg.edit_text(f'✅ Successfully processed and sent {len(generated)} clip(s)!')
        
        # Optional: Send summary
        total_duration = sum(get_video_duration(clip) for clip in generated)
        update.message.reply_text(
            f"📊 Summary:\n"
            f"• Clips generated: {len(generated)}\n"
            f"• Total duration: {total_duration:.1f}s\n"
            f"• Session ID: {base_dir.name}\n"
            f"🗑️ Files will be automatically cleaned up after 24 hours."
        )
        
    except Exception as exc:
        raise exc

def error_handler(update: Update, context: CallbackContext):
    """Handle errors in the bot."""
    print(f"Error: {context.error}")
    if update and update.message:
        update.message.reply_text("❌ An unexpected error occurred. Please try again.")

# ---------- Main ----------

def main():
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print('❌ Please set TELEGRAM_BOT_TOKEN environment variable.')
        return
    
    print('🤖 Starting YouTube Shorts Converter Bot...')
    print(f'📁 Work directory: {WORKDIR}')
    print(f'⏰ Max clip duration: {MAX_CLIP_SECONDS}s')
    
    # Clean up old files on startup
    cleanup_old_files()
    
    try:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        
        # Add handlers
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("cleanup", cleanup))
        dp.add_handler(MessageHandler(Filters.entity("url") & Filters.text, process_video_url))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, process_video_url))
        dp.add_handler(MessageHandler(Filters.video | Filters.document.mime_type("video/*"), handle_video_file))
        
        # Error handler
        dp.add_error_handler(error_handler)
        
        print('✅ Bot is running... Press Ctrl+C to stop.')
        updater.start_polling()
        updater.idle()
        
    except Exception as e:
        print(f'❌ Failed to start bot: {e}')

if __name__ == '__main__':
    main()
