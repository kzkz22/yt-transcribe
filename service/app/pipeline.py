"""Local mode: download -> Whisper -> forced alignment -> speaker diarization.
Also holds the settings and the code that fetches the audio, shared with cloud mode.

Heavy libraries are imported inside the functions so the HTTP server starts
fast. The pipeline itself runs in a short-lived child process (see runner.py),
so every byte of RAM and VRAM is returned when a job ends.
"""
from __future__ import annotations

import gc
import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import formatting

log = logging.getLogger("yt-transcribe")


def _physical_cores() -> int:
    """Physical core count (hyper-threads do not help Whisper on CPU); falls back to logical."""
    try:
        cores = set()
        for cpu in os.sched_getaffinity(0):
            path = f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
            with open(path, encoding="ascii") as fh:
                cores.add(fh.read().strip())
        if cores:
            return len(cores)
    except (OSError, AttributeError):
        pass
    return os.cpu_count() or 4


@dataclass
class Settings:
    device: str = os.environ.get("DEVICE", "auto").lower()           # auto | cpu | cuda
    whisper_model: str = os.environ.get("WHISPER_MODEL", "large-v3")
    compute_type: str = os.environ.get("COMPUTE_TYPE", "")           # empty = by device
    batch_size: int = int(os.environ.get("BATCH_SIZE", "0"))         # 0 = by device
    cpu_threads: int = int(os.environ.get("CPU_THREADS", "0"))       # 0 = physical cores
    hf_token: str = os.environ.get("HF_TOKEN", "")
    diarize_model: str = os.environ.get("DIARIZE_MODEL", "")         # empty = whisperx default
    speaker_label: str = os.environ.get("SPEAKER_LABEL", "")         # empty = by language
    cookies_file: str = os.environ.get("YTDLP_COOKIES", "")
    max_duration_s: int = int(os.environ.get("MAX_DURATION_MIN", "360")) * 60
    # GPU sharing with the LLM (see gpu.py)
    llama_server_url: str = os.environ.get("LLAMA_SERVER_URL", "").rstrip("/")
    llama_server_api_key: str = os.environ.get("LLAMA_SERVER_API_KEY", "")
    min_free_vram_mb: int = int(os.environ.get("MIN_FREE_VRAM_MB", "7000"))
    # Per-request choices: which modes/models callers may ask for, and the defaults
    default_mode: str = os.environ.get("DEFAULT_MODE", "local").lower()      # local | cloud
    local_models: tuple = tuple(
        m.strip() for m in os.environ.get("LOCAL_MODELS", "large-v3,large-v3-turbo").split(",")
        if m.strip())
    # Cloud mode (see cloud.py)
    assemblyai_api_key: str = os.environ.get("ASSEMBLYAI_API_KEY", "")
    assemblyai_base_url: str = os.environ.get(
        "ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com").rstrip("/")
    cloud_default_model: str = os.environ.get("CLOUD_MODEL", "auto").lower()
    cloud_monthly_hours: float = float(os.environ.get("CLOUD_MONTHLY_HOURS", "20"))
    cloud_delete_after: bool = os.environ.get("CLOUD_DELETE_AFTER", "1") == "1"
    cloud_timeout_s: int = int(os.environ.get("CLOUD_TIMEOUT_MIN", "60")) * 60
    data_dir: str = os.environ.get("DATA_DIR", "/data")

    def compute_type_for(self, device: str) -> str:
        return self.compute_type or ("float16" if device == "cuda" else "int8")

    def batch_size_for(self, device: str) -> int:
        return self.batch_size or (8 if device == "cuda" else 4)

    def threads(self) -> int:
        return self.cpu_threads or _physical_cores()


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


def _free() -> None:
    """Collect garbage and hand cached GPU memory back (callers `del` their models first)."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _json_default(obj):
    """numpy scalars (timestamps from the aligner) are not JSON types."""
    return obj.item() if hasattr(obj, "item") else str(obj)


def asr_checkpoint_path(video_dir: Path, model: str, language: str | None) -> Path:
    """Where the transcription (before speaker separation) of a video is kept.

    Whisper is the slow part and does not depend on the speaker options, so a
    job that fails later, or is repeated with another speaker count, reuses it.
    """
    key = hashlib.sha1(json.dumps([model, language]).encode()).hexdigest()[:10]
    return video_dir / f"asr_{key}.json"


def run(opts: Options, video: dict, settings: Settings, device: str,
        stage: Callable[[str], None] = lambda s: None,
        checkpoint: Path | None = None) -> dict:
    """Run the pipeline for one video on `device` ("cpu" or "cuda") and return the result dict."""
    if opts.diarize and not settings.hf_token:
        raise PipelineError(
            "Speaker separation needs a Hugging Face token: set HF_TOKEN on the container "
            "and accept the terms of pyannote/speaker-diarization-community-1. "
            "Or submit with diarize=false.")

    import torch
    import whisperx

    torch.set_num_threads(settings.threads())
    asr = None
    if checkpoint is not None and checkpoint.exists():
        try:
            asr = json.loads(checkpoint.read_text(encoding="utf-8"))
            log.info("reusing saved transcription %s", checkpoint.name)
        except (OSError, ValueError):
            asr = None

    workdir = tempfile.mkdtemp(prefix="ytt_")
    try:
        audio = None
        if asr is None or opts.diarize:
            stage("download")
            audio = whisperx.load_audio(source_audio(opts, settings, workdir))

        if asr is None:
            stage("transcribe")
            model = whisperx.load_model(
                opts.model, device,
                compute_type=settings.compute_type_for(device),
                language=opts.language, threads=settings.threads())
            result = model.transcribe(audio, batch_size=settings.batch_size_for(device),
                                      language=opts.language)
            language = result.get("language") or opts.language or "en"
            del model
            _free()
            if not result.get("segments"):
                raise PipelineError("No speech was detected in the audio.")

            stage("align")
            warnings: list[str] = []
            try:
                align_model, meta = whisperx.load_align_model(language_code=language, device=device)
                aligned = whisperx.align(result["segments"], align_model, meta, audio, device,
                                         return_char_alignments=False)
                del align_model
                _free()
                if aligned.get("segments"):
                    result = aligned
            except Exception as exc:  # alignment is an accuracy bonus, not a requirement
                log.warning("alignment failed, continuing with Whisper timestamps: %s", exc)
                warnings.append("Word alignment failed; timestamps and speaker boundaries "
                                "are coarser than usual.")
                _free()
            asr = {"language": language, "segments": result["segments"], "warnings": warnings}
            if checkpoint is not None:
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                tmp = checkpoint.with_suffix(".tmp")
                tmp.write_text(json.dumps(asr, ensure_ascii=False, default=_json_default),
                                encoding="utf-8")
                tmp.replace(checkpoint)

        language = asr["language"]
        result = {"segments": asr["segments"]}
        for warning in asr.get("warnings", []):
            if warning not in opts.warnings:
                opts.warnings.append(warning)

        if opts.diarize:
            stage("diarize")
            from whisperx.diarize import DiarizationPipeline

            diarizer = DiarizationPipeline(model_name=settings.diarize_model or None,
                                           token=settings.hf_token, device=device)
            diarize_df = diarizer(audio, num_speakers=opts.num_speakers,
                                  min_speakers=opts.min_speakers, max_speakers=opts.max_speakers)
            result = whisperx.assign_word_speakers(diarize_df, result, fill_nearest=True)
            del diarizer
            _free()

        stage("write")
        segments = formatting.clean_segments(result["segments"])
        speakers: list[dict] = []
        if opts.diarize:
            speakers = formatting.relabel_speakers(
                segments,
                formatting.speaker_word(language, settings.speaker_label or None),
                formatting.unknown_word(language))
        else:
            for seg in segments:
                seg["speaker"] = None
        return {
            "video": video, "language": language, "mode": "local", "model": opts.model,
            "device": device, "diarized": opts.diarize, "speakers": speakers,
            "warnings": opts.warnings, "segments": segments,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _free()
