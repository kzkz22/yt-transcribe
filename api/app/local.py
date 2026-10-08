"""Local mode: the audio goes to the GPU worker on the GPU server, through llama-swap.

llama-swap gives the GPU to one program at a time. The whole job is one HTTP
request to  WORKER_URL/transcribe : llama-swap first lets the LLM finish its
running requests, stops it, starts the worker, and keeps queued LLM requests
waiting until this request ends. The worker streams its progress as JSON lines
(see gpu-worker/gpuworker/server.py) and returns raw segments with speaker ids;
naming the speakers and writing the files happen here.

There is no fallback to cloud mode: if the worker cannot be reached, the job fails.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from . import formatting
from .pipeline import Options, PipelineError, Settings, source_audio, to_flac

log = logging.getLogger("yt-transcribe")

UNREACHABLE = ("The GPU worker could not be reached at {url} ({exc}). Local transcription needs "
               "the GPU server to be on and llama-swap to be running; nothing was switched to "
               "cloud mode.")


def _query(opts: Options) -> str:
    params = {"model": opts.model, "language": opts.language,
              "diarize": "true" if opts.diarize else "false",
              "num_speakers": opts.num_speakers, "min_speakers": opts.min_speakers,
              "max_speakers": opts.max_speakers}
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def _http_error(exc: urllib.error.HTTPError) -> PipelineError:
    detail = exc.read().decode("utf-8", "replace")[:600]
    try:
        parsed = json.loads(detail)
        detail = parsed.get("detail") or parsed.get("error") or detail
        if isinstance(detail, dict):  # llama-swap wraps its own errors
            detail = detail.get("message", detail)
    except (ValueError, AttributeError):
        pass
    return PipelineError(f"The GPU worker answered HTTP {exc.code}: {detail}")


def job_deadline_s(settings: Settings, audio_seconds: float) -> int:
    """How long a local job may take in all: WORKER_TIMEOUT_MIN, or 4x the audio length when
    that is more (enough for a run that falls back to CPU)."""
    return int(max(settings.worker_timeout_s, 4 * (audio_seconds or 0)))


def transcribe_on_worker(flac: str, opts: Options, settings: Settings,
                         stage: Callable[..., None], deadline_s: int) -> dict:
    """Send the audio to the worker and follow its progress; returns the worker's raw result.

    The worker sends a line at least every 10 s, so a silent connection means trouble; the
    overall deadline stops a job that keeps sending heartbeats but never finishes. Closing
    the connection makes the worker stop the job and free the GPU.
    """
    url = f"{settings.worker_url}/transcribe?{_query(opts)}"
    deadline = time.time() + deadline_s
    with open(flac, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="POST", headers={
            "content-type": "audio/flac", "content-length": str(os.path.getsize(flac))})
        try:
            # Generous: the first byte comes only after llama-swap has freed the GPU.
            resp = urllib.request.urlopen(req, timeout=deadline_s)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionResetError, BrokenPipeError)):
                raise PipelineError("The GPU worker closed the connection while receiving the "
                                    f"audio ({reason}); see the worker log.") from exc
            raise PipelineError(UNREACHABLE.format(url=settings.worker_url, exc=exc)) from exc
    try:
        with resp:
            for raw in resp:
                if time.time() > deadline:
                    raise PipelineError(f"The GPU worker did not finish within {deadline_s // 60} "
                                        "minutes; the job was stopped.")
                if not raw.strip():
                    continue
                msg = json.loads(raw)
                if "result" in msg:
                    return msg["result"]
                if "error" in msg:
                    raise PipelineError(msg["error"])
                if msg.get("stage"):
                    stage(msg["stage"], msg.get("device"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"The connection to the GPU worker broke during the job ({exc}). "
                            "Submitting again resumes from the saved transcription.") from exc
    raise PipelineError("The GPU worker stopped before the transcript was ready (it may have been "
                        "stopped or restarted). Submitting again resumes from the saved transcription.")


def run(opts: Options, video: dict, settings: Settings,
        stage: Callable[..., None] = lambda *a: None) -> dict:
    """Transcribe one video in local mode and return the result dict."""
    workdir = tempfile.mkdtemp(prefix="ytt_")
    try:
        stage("download")
        source = source_audio(opts, settings, workdir)
        stage("prepare-audio")
        flac = to_flac(source, workdir)
        stage("waiting-for-gpu")
        raw = transcribe_on_worker(flac, opts, settings, stage,
                                   job_deadline_s(settings, video.get("duration") or 0))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    stage("write")
    language = raw.get("language") or opts.language or "en"
    warnings = list(opts.warnings) + [w for w in raw.get("warnings", []) if w not in opts.warnings]
    segments = formatting.clean_segments(raw.get("segments") or [])
    if not segments:
        raise PipelineError("No speech was detected in the audio.")
    diarized = bool(raw.get("diarized"))
    speakers: list[dict] = []
    if diarized:
        speakers = formatting.relabel_speakers(
            segments, formatting.speaker_word(language, settings.speaker_label or None),
            formatting.unknown_word(language))
    else:
        for seg in segments:
            seg["speaker"] = None
    return {
        "video": video, "language": language, "mode": "local", "model": opts.model,
        "device": raw.get("device"), "diarized": diarized, "speakers": speakers,
        "warnings": warnings, "segments": segments,
    }
