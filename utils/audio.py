"""ffmpeg wrapper: any TTS output (mp3/wav/pcm) -> Telegram-compatible voice note.

Telegram voice notes must be OGG container / OPUS codec, mono, and Telegram
recommends a low-ish bitrate (voice notes are small on purpose).
"""
import asyncio
from pathlib import Path


async def to_telegram_voice(src_path: Path, dst_path: Path, bitrate: str = "32k") -> Path:
    """Convert any audio file to a Telegram-voice-note-compatible OGG/Opus file."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-ac", "1",              # mono
        "-c:a", "libopus",
        "-b:a", bitrate,
        "-application", "voip",  # opus tuning profile for speech
        str(dst_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed converting {src_path}: {stderr.decode(errors='ignore')}")
    return dst_path


async def probe_duration_seconds(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0
