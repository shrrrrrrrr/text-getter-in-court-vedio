from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

DEMUXERS = "mov,matroska,webm,avi,mpeg,mpegts,flv,asf,rm,ogg,wav,mp3,flac,aac,aiff,amr,ape,ac3,caf,nut"

class Cancelled(Exception):
    pass


def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled()


def command(args: list[str], event=None, timeout=3600):
    check_cancel(event)
    # No shell, no URL input, no stdin interaction. ffmpeg's protocol list is restricted below.
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        started = time.monotonic()
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if (event and event.is_set()) or time.monotonic() - started > timeout:
                    proc.kill()
                    proc.communicate()
                    check_cancel(event)
                    raise RuntimeError("媒体处理超时")
        if proc.returncode:
            # Store stderr locally for diagnosis; don't return local paths or raw FFmpeg output to API.
            raise RuntimeError("媒体处理失败：文件损坏、编码不支持或所选音轨不存在")
        return out


def probe(path: Path) -> dict:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("需要安装 FFmpeg，并确保 ffmpeg / ffprobe 在 PATH 中")
    out = command(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS, "-show_format", "-show_streams", "-of", "json", str(path)], timeout=60)
    data = json.loads(out)
    streams = data.get("streams", [])
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not audios:
        raise ValueError("视频中未发现音轨，无法从静默画面恢复原话")
    duration = float(data.get("format", {}).get("duration") or max(float(s.get("duration") or 0) for s in audios))
    if not 0 < duration < float("inf"):
        raise ValueError("无法确定媒体时长")
    return {
        "duration": duration,
        "format": data.get("format", {}).get("format_name", "unknown"),
        "has_video": any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams),
        "audio_streams": [{"index": i, "codec": s.get("codec_name"), "channels": s.get("channels"), "sample_rate": s.get("sample_rate")} for i, s in enumerate(audios)],
    }


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def audio(source: Path, target: Path, stream=0, event=None):
    command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS, "-i", str(source), "-map", f"0:a:{stream}", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)], event)


def preview(source: Path, target: Path, stream=0, event=None):
    command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0", "-map", f"0:a:{stream}", "-vf", "scale=960:540:force_original_aspect_ratio=decrease", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-movflags", "+faststart", str(target)], event)


def frame(source: Path, target: Path, seconds: float):
    command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-ss", str(seconds), "-i", str(source), "-frames:v", "1", "-vf", "scale=960:-2", str(target)], timeout=60)


def cloud_audio(source: Path, target: Path, event=None):
    command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source), "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(target)], event)
