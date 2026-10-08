"""Settings, job options and the code that fetches and prepares the audio.

Used by both modes: the audio of a URL or an uploaded file is fetched here and
turned into a small mono FLAC, which goes either to the GPU worker (local mode,
see local.py) or to the cloud provider (cloud.py).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("yt-transcribe")


@dataclass
class Settings:
    speaker_label: str = os.environ.get("SPEAKER_LABEL", "")         # empty = by language
    cookies_file: str = os.environ.get("YTDLP_COOKIES", "")
    max_duration_s: int = int(os.environ.get("MAX_DURATION_MIN", "360")) * 60
    # Per-request choices: which modes/models callers may ask for, and the defaults
    default_mode: str = os.environ.get("DEFAULT_MODE", "local").lower()      # local | cloud
    whisper_model: str = os.environ.get("WHISPER_MODEL", "large-v3")
    local_models: tuple = tuple(
        m.strip() for m in os.environ.get("LOCAL_MODELS", "large-v3,large-v3-turbo").split(",")
        if m.strip())
    # Local mode: the GPU worker, reached through llama-swap (see local.py)
    worker_url: str = os.environ.get(
        "WORKER_URL", "http://127.0.0.1:8080/upstream/yt-transcribe-gpu").rstrip("/")
    worker_timeout_s: int = int(float(os.environ.get("WORKER_TIMEOUT_MIN", "30")) * 60)
    # Cloud mode (see cloud.py)
    assemblyai_api_key: str = os.environ.get("ASSEMBLYAI_API_KEY", "")
    assemblyai_base_url: str = os.environ.get(
        "ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com").rstrip("/")
    cloud_default_model: str = os.environ.get("CLOUD_MODEL", "auto").lower()
    cloud_monthly_hours: float = float(os.environ.get("CLOUD_MONTHLY_HOURS", "20"))
    cloud_delete_after: bool = os.environ.get("CLOUD_DELETE_AFTER", "1") == "1"
    cloud_timeout_s: int = int(os.environ.get("CLOUD_TIMEOUT_MIN", "60")) * 60
    data_dir: str = os.environ.get("DATA_DIR", "/var/lib/yt-transcribe")


@dataclass
class Options:
    url: str | None = None             # a video URL ...
    upload_id: str | None = None       # ... or a file uploaded to this service
    mode: str = "local"                # local | cloud
    model: str = "large-v3"            # a local Whisper name, or the cloud model id
    language: str | None = None        # None = auto-detect
    diarize: bool = True
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    warnings: list[str] = field(default_factory=list)


class PipelineError(Exception):
    """Error whose message is safe and useful to show to the caller."""


def _ydl_opts(settings: Settings, outdir: str | None = None) -> dict:
    opts: dict = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    if settings.cookies_file and os.path.exists(settings.cookies_file):
        opts["cookiefile"] = settings.cookies_file
    if outdir:
        opts["format"] = "bestaudio/best"
        opts["outtmpl"] = os.path.join(outdir, "audio.%(ext)s")
    return opts


def probe(url: str, settings: Settings) -> dict:
    """Fetch video metadata without downloading."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(settings)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many types
        raise PipelineError(f"yt-dlp could not read the video: {exc}") from exc
    if info.get("_type") == "playlist":
        raise PipelineError("This URL is a playlist; pass a single video URL.")
    if info.get("is_live"):
        raise PipelineError("Live streams are not supported; wait until the stream has ended.")
    duration = info.get("duration") or 0
    if duration and duration > settings.max_duration_s:
        raise PipelineError(
            f"Video is {duration // 60} min long, above the MAX_DURATION_MIN limit "
            f"({settings.max_duration_s // 60} min).")
    return {
        "id": f"{(info.get('extractor_key') or 'video').lower()}_{info.get('id')}",
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": duration,
        "url": info.get("webpage_url") or url,
        "upload_date": info.get("upload_date"),
    }


def _download(url: str, settings: Settings, outdir: str) -> str:
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(settings, outdir)) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        raise PipelineError(f"yt-dlp could not download the audio: {exc}") from exc
    files = [f for f in glob.glob(os.path.join(outdir, "audio.*")) if not f.endswith(".part")]
    if not files:
        raise PipelineError("yt-dlp finished but produced no audio file.")
    return files[0]


def upload_dir(settings: Settings, upload_id: str) -> Path:
    return Path(settings.data_dir) / "uploads" / upload_id


def uploaded_file(settings: Settings, upload_id: str) -> Path:
    """The media file of an upload; raises if it is missing (expired or never uploaded)."""
    files = [f for f in upload_dir(settings, upload_id).glob("media*") if f.is_file()]
    if not files:
        raise PipelineError("The uploaded file is no longer on the server; upload it again.")
    return files[0]


def audio_duration(path: str | Path) -> float:
    """Length of the first audio stream in seconds; raises if the file has no audio."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_type:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, check=True).stdout
        info = json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise PipelineError("The file could not be read as audio or video.") from exc
    if not info.get("streams"):
        raise PipelineError("The file has no audio track.")
    try:
        return float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        return 0.0


def describe_upload(settings: Settings, upload_id: str) -> dict:
    """Metadata for an uploaded file, shaped like probe()'s result for a URL."""
    path = uploaded_file(settings, upload_id)
    try:
        meta = json.loads((upload_dir(settings, upload_id) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    name = meta.get("name") or path.name
    duration = audio_duration(path)
    if duration > settings.max_duration_s:
        raise PipelineError(
            f"File is {int(duration) // 60} min long, above the MAX_DURATION_MIN limit "
            f"({settings.max_duration_s // 60} min).")
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(name).stem)[:40]
    return {"id": f"file_{stem}_{upload_id[:8]}", "title": name, "uploader": None,
            "duration": int(duration), "url": f"file: {name}", "upload_date": None}


def source_audio(opts: Options, settings: Settings, workdir: str) -> str:
    """Path of the media to transcribe: the uploaded file, or the URL's downloaded audio."""
    if opts.upload_id:
        return str(uploaded_file(settings, opts.upload_id))
    return _download(opts.url, settings, workdir)


def to_flac(src: str, workdir: str) -> str:
    """Mono 16 kHz FLAC: lossless for speech models and a fraction of a video's size."""
    dst = os.path.join(workdir, "audio.flac")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src, "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "flac", dst],
                       check=True, capture_output=True, timeout=3600)
    except subprocess.CalledProcessError as exc:
        raise PipelineError("ffmpeg could not extract the audio: "
                            + exc.stderr.decode("utf-8", "replace")[-300:]) from exc
    return dst
