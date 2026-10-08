"""GPU worker of yt-transcribe: turns one audio file into a raw, speaker-tagged transcript.

Runs on the GPU server as a container that llama-swap starts and stops, so it
only holds the GPU while llama-swap has given it to the worker. The API (on
another machine) reaches it through llama-swap:  /upstream/<name>/transcribe

GET  /health
POST /transcribe?model=large-v3&language=hu&diarize=true&num_speakers=2
     body: the audio (16 kHz mono FLAC from the API; any format ffmpeg reads works)
     answer: application/x-ndjson, one JSON object per line, sent as the job goes:
        {"stage": "transcribe", "device": "cuda"}   progress, and every 10 s as a heartbeat
        {"result": {...}}                            last line on success
        {"error": "..."}                             last line on failure

The whole job is one HTTP request on purpose: while it is open, llama-swap
keeps the GPU for the worker and queues LLM requests behind it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gpu-worker")

SETTINGS = Settings()
JOBS_DIR = Path(SETTINGS.data_dir) / "jobs"
HEARTBEAT_S = 10
_gpu = asyncio.Lock()  # one job at a time


def _expire_checkpoints() -> None:
    limit = time.time() - SETTINGS.asr_cache_days * 86400
    for path in (Path(SETTINGS.data_dir) / "asr").glob("*.json"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    shutil.rmtree(JOBS_DIR, ignore_errors=True)  # leftovers of jobs cut short by a restart
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _expire_checkpoints()
    log.info("ready: device=%s models=%s diarization=%s", SETTINGS.device, ",".join(SETTINGS.models),
             "on" if SETTINGS.hf_token else "OFF (no HF_TOKEN)")
    yield


app = FastAPI(title="yt-transcribe GPU worker", version="2.0.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "device": SETTINGS.device, "models": list(SETTINGS.models),
            "diarization_available": bool(SETTINGS.hf_token), "busy": _gpu.locked()}


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _fail_message(job_dir: Path, returncode: int) -> str:
    try:
        return (job_dir / "error.txt").read_text(encoding="utf-8")
    except OSError:
        pass
    if returncode < 0 or returncode == 137:
        return ("The transcription process was killed by the system (signal "
                f"{abs(returncode) if returncode < 0 else 9}), most likely because the GPU server "
                "ran out of memory. Finished steps are saved; submitting again resumes from them.")
    return f"The transcription process exited unexpectedly (code {returncode}); see the worker log."


def _read_status(job_dir: Path) -> dict:
    try:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def _run(job_dir: Path):
    """Run the job's child process and stream its progress; always cleans up."""
    proc = None
    try:
        while True:  # wait for the GPU, saying so now and then
            try:
                await asyncio.wait_for(_gpu.acquire(), timeout=HEARTBEAT_S)
                break
            except asyncio.TimeoutError:
                yield _line({"stage": "waiting-for-gpu"})
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "gpuworker.job", str(job_dir),
                cwd=str(Path(__file__).resolve().parent.parent))
            sent, sent_at = None, 0.0
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                state = _read_status(job_dir)
                now = time.time()
                if state and (state != sent or now - sent_at >= HEARTBEAT_S):
                    yield _line(state)
                    sent, sent_at = state, now
                elif not state and now - sent_at >= HEARTBEAT_S:
                    yield _line({"stage": "starting"})
                    sent_at = now
            result_file = job_dir / "result.json"
            if proc.returncode == 0 and result_file.exists():
                yield _line({"result": json.loads(result_file.read_text(encoding="utf-8"))})
            else:
                message = _fail_message(job_dir, proc.returncode)
                log.warning("job %s failed: %s", job_dir.name, message)
                yield _line({"error": message})
        finally:
            # Shielded: when the caller has gone away, the stream is cancelled, and that
            # cancellation would otherwise hit every await here and skip the release.
            with anyio.CancelScope(shield=True):
                if proc is not None and proc.returncode is None:
                    log.warning("job %s: caller disconnected, stopping the job", job_dir.name)
                    proc.kill()
                    await proc.wait()
                _gpu.release()
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def _receive(request: Request, target: Path) -> tuple[str, int, bool]:
    """Save the whole request body; returns its SHA-256, size, and whether it was too large.

    The body is always read to the end, even when it is too large or the request will be
    refused: answering before that makes the caller see a reset connection, not the reason.
    """
    digest, size, too_large = hashlib.sha256(), 0, False
    with open(target, "wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            if size > SETTINGS.max_audio_bytes:
                too_large = True
            if not too_large:
                digest.update(chunk)
                await asyncio.to_thread(fh.write, chunk)  # keep the event loop (heartbeats) free
    return digest.hexdigest(), size, too_large


@app.post("/transcribe")
async def transcribe(request: Request, model: str, language: str | None = None,
                     diarize: bool = True, num_speakers: int | None = None,
                     min_speakers: int | None = None, max_speakers: int | None = None):
    job_dir = JOBS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True)
    try:
        sha256, size, too_large = await _receive(request, job_dir / "audio")
        model = model.strip().lower()
        if model not in SETTINGS.models:
            raise HTTPException(status_code=422, detail=f"unknown model '{model}'; this worker "
                                f"has: {', '.join(SETTINGS.models)}")
        if diarize and not SETTINGS.hf_token:
            raise HTTPException(status_code=422, detail=(
                "Speaker separation needs a Hugging Face token: set HF_TOKEN on the GPU worker "
                "container and accept the terms of pyannote/speaker-diarization-community-1. "
                "Or submit with diarize=false."))
        if too_large:
            raise HTTPException(status_code=413, detail="audio is larger than the worker's "
                                f"MAX_AUDIO_MB limit ({SETTINGS.max_audio_bytes // 1024 ** 2} MB)")
        if size == 0:
            raise HTTPException(status_code=422, detail="no audio was sent")
        language = (language or "").strip().lower() or None
        (job_dir / "job.json").write_text(json.dumps({
            "sha256": sha256,
            "options": {"model": model, "language": language, "diarize": diarize,
                        "num_speakers": num_speakers, "min_speakers": min_speakers,
                        "max_speakers": max_speakers}}), encoding="utf-8")
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    log.info("job %s: %d bytes, model=%s language=%s diarize=%s", job_dir.name, size, model,
             language or "auto", diarize)
    return StreamingResponse(_run(job_dir), media_type="application/x-ndjson")
