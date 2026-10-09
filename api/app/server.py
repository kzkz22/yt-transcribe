"""Small job-queue HTTP API around the transcription pipeline.

GET  /uploads/{sha256}     is this file already on the server?
PUT  /uploads/{sha256}     upload a local audio/video file (raw body, ?name=file.mp4)
POST /jobs                 submit a video URL or an upload_id   -> {job_id, status, ...}
GET  /jobs/{id}            poll                                 -> {status, stage, elapsed_s, ...}
GET  /jobs/{id}/result     ?format=txt|srt|json                 -> transcript
GET  /results?since=T      finished results newer than T (unix time), oldest first
GET  /results/{video}/{key}?format=txt|srt|json                  -> one finished result
GET  /health

Each job says how it should be transcribed: mode (local | cloud | captions),
model and language; anything left out takes the service's default. Local jobs
are sent to the GPU worker through llama-swap (local.py), cloud jobs to
AssemblyAI (cloud.py), captions jobs fetch YouTube's own subtitles (captions.py). One job runs at a time, each in its own child process (see
runner.py). Finished results are cached on disk under DATA_DIR per video and
option set, so asking again returns immediately, and the same video can be
kept in both modes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from . import cloud, pipeline, usage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("yt-transcribe")

SETTINGS = pipeline.Settings()
DATA_DIR = Path(SETTINGS.data_dir)
CACHE_DIR = DATA_DIR / "transcripts"
JOBS_DIR = DATA_DIR / "jobs"
UPLOADS_DIR = DATA_DIR / "uploads"
MAX_UPLOAD_BYTES = int(float(os.environ.get("MAX_UPLOAD_GB", "8")) * 1024 ** 3)
UPLOAD_TTL_S = int(float(os.environ.get("UPLOAD_TTL_DAYS", "7")) * 86400)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()


class JobRequest(BaseModel):
    url: str | None = Field(default=None, description="Video URL (anything yt-dlp can read)")
    upload_id: str | None = Field(default=None, description="Id returned by PUT /uploads/{sha256}")
    mode: str | None = Field(default=None, description="local, cloud or captions; default from DEFAULT_MODE")
    model: str | None = Field(default=None, description="Depends on mode; see GET /health")
    language: str | None = Field(default=None, description="ISO code such as hu or en; omit to auto-detect")
    diarize: bool = True
    num_speakers: int | None = Field(default=None, ge=1, le=20)
    min_speakers: int | None = Field(default=None, ge=1, le=20)
    max_speakers: int | None = Field(default=None, ge=1, le=20)
    force: bool = False

    @model_validator(mode="after")
    def _check(self):
        if bool(self.url) == bool(self.upload_id):
            raise ValueError("give exactly one of url and upload_id")
        if self.url and not self.url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if self.upload_id and not _is_sha256(self.upload_id):
            raise ValueError("upload_id is not valid")
        self.mode = (self.mode or SETTINGS.default_mode).strip().lower()
        if self.mode not in ("local", "cloud", "captions"):
            raise ValueError("mode must be local, cloud or captions")
        if self.mode == "captions" and not self.url:
            raise ValueError("captions mode needs a video URL; uploaded files have no captions")
        if self.language is not None:
            self.language = self.language.strip().lower() or None
            if self.language == "auto":
                self.language = None
        if self.min_speakers and self.max_speakers and self.min_speakers > self.max_speakers:
            raise ValueError("min_speakers must not be greater than max_speakers")
        if self.mode == "captions":  # no speakers: keep equivalent requests on one cache entry
            self.diarize, self.num_speakers, self.min_speakers, self.max_speakers = False, None, None, None
        self.model = _resolve_model(self.mode, self.model, self.language)
        return self


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _resolve_model(mode: str, model: str | None, language: str | None) -> str:
    """Validate the model for the mode and return the name the job will actually use."""
    model = (model or "").strip().lower() or None
    if mode == "captions":
        if model not in (None, "auto", "youtube"):
            raise ValueError("captions mode has no model choice; leave model empty")
        return "youtube"
    if mode == "cloud":
        if not SETTINGS.assemblyai_api_key:
            raise ValueError("cloud mode is not set up: ASSEMBLYAI_API_KEY is missing on the service")
        try:
            return cloud.resolve_model(model or SETTINGS.cloud_default_model, language)
        except pipeline.PipelineError as exc:
            raise ValueError(str(exc)) from exc
    model = model or SETTINGS.whisper_model
    allowed = {SETTINGS.whisper_model, *SETTINGS.local_models}
    if model not in allowed:
        raise ValueError(f"unknown local model '{model}'; choose one of: {', '.join(sorted(allowed))}")
    return model


def _cache_path(video_id: str, req: JobRequest) -> Path:
    key = json.dumps([req.mode, req.model, req.language, req.diarize, req.num_speakers,
                      req.min_speakers, req.max_speakers], sort_keys=True)
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    safe_id = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "_" for c in video_id)[:150]
    return CACHE_DIR / safe_id / digest


def _public(job: dict) -> dict:
    out = {k: job[k] for k in ("job_id", "status", "stage", "device", "video", "error", "cached")
           if job.get(k) is not None}
    start = job.get("started_at")
    if start:
        out["elapsed_s"] = int((job.get("finished_at") or time.time()) - start)
    if job["status"] == "queued":
        out["queue_position"] = sum(
            1 for j in _jobs.values()
            if j["status"] in ("queued", "running") and j["created_at"] < job["created_at"])
    if job["status"] == "done":
        out.update(job.get("summary", {}))
    return out


def _summary(result: dict) -> dict:
    return {
        "language": result["language"], "diarized": result["diarized"],
        "mode": result.get("mode", "local"), "model": result.get("model"),
        "device": result.get("device"), "cloud": result.get("cloud"),
        "captions": result.get("captions"),
        "speakers": result["speakers"], "warnings": result.get("warnings", []),
        "segments": len(result["segments"]),
        "characters": sum(len(s["text"]) for s in result["segments"]),
    }


def _fail_message(job_dir: Path, returncode: int) -> str:
    try:
        return (job_dir / "error.txt").read_text(encoding="utf-8")
    except OSError:
        pass
    if returncode < 0 or returncode == 137:
        return ("The job process was killed by the system (signal "
                f"{abs(returncode) if returncode < 0 else 9}), most likely because this machine ran "
                "out of memory. Submitting again resumes from the finished steps.")
    return f"The job process exited unexpectedly (code {returncode}); see the service log."


def _run_job(job: dict) -> None:
    """Run one job in a child process and mirror its stage into the job record."""
    job_dir = JOBS_DIR / job["job_id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        (job_dir / "job.json").write_text(json.dumps(
            {"path": job["path"], "video": job["video"], "options": job["options"]}), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "app.runner", str(job_dir)],
                                cwd=str(Path(__file__).resolve().parent.parent))
        while proc.poll() is None:
            try:
                state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
                job.update(stage=state.get("stage", job["stage"]), device=state.get("device"))
            except (OSError, ValueError):
                pass
            time.sleep(1)
        result_file = Path(job["path"]) / "result.json"
        if proc.returncode == 0 and result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            job.update(status="done", stage="done", summary=_summary(result))
            log.info("job %s done: %s", job["job_id"], job["video"].get("title"))
        else:
            job.update(status="error", error=_fail_message(job_dir, proc.returncode))
            log.warning("job %s failed: %s", job["job_id"], job["error"])
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def _job_loop() -> None:
    while True:
        job_id = _queue.get()
        job = _jobs[job_id]
        job.update(status="running", stage="starting", started_at=time.time())
        try:
            _run_job(job)
        except Exception as exc:  # keep the worker alive whatever happens
            job.update(status="error", error=f"{type(exc).__name__}: {exc}")
            log.exception("job %s crashed", job_id)
        finally:
            job["finished_at"] = time.time()
            _queue.task_done()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(JOBS_DIR, ignore_errors=True)  # leftovers of jobs cut short by a restart
    _expire_uploads()
    threading.Thread(target=_job_loop, daemon=True, name="jobs").start()
    log.info("ready: default-mode=%s local-model=%s worker=%s cloud=%s",
             SETTINGS.default_mode, SETTINGS.whisper_model, SETTINGS.worker_url,
             "on" if SETTINGS.assemblyai_api_key else "off (no ASSEMBLYAI_API_KEY)")
    yield


app = FastAPI(title="yt-transcribe", version="2.0.0", lifespan=_lifespan)


def _expire_uploads() -> None:
    """Uploaded media is only needed until its jobs have run; drop what is older than the TTL."""
    now = time.time()
    for folder in UPLOADS_DIR.glob("*"):
        try:
            if folder.is_dir() and now - folder.stat().st_mtime > UPLOAD_TTL_S:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "default_mode": SETTINGS.default_mode,
        # The worker is not contacted here: through llama-swap that would take the GPU.
        "local": {"default_model": SETTINGS.whisper_model,
                  "models": sorted({SETTINGS.whisper_model, *SETTINGS.local_models}),
                  "worker_url": SETTINGS.worker_url},
        "captions": {"available": True, "source": "YouTube subtitles through yt-dlp"},
        "cloud": {"available": bool(SETTINGS.assemblyai_api_key), "provider": "assemblyai",
                  "default_model": SETTINGS.cloud_default_model, "models": cloud.choices(),
                  "hours_used_this_month": round(usage.used_seconds(str(DATA_DIR)) / 3600, 2),
                  "monthly_hours_limit": SETTINGS.cloud_monthly_hours},
        "jobs_waiting": sum(1 for j in _jobs.values() if j["status"] in ("queued", "running")),
    }


@app.get("/uploads/{sha256}")
def upload_status(sha256: str) -> dict:
    if not _is_sha256(sha256):
        raise HTTPException(status_code=422, detail="not a sha256 hex digest")
    folder = UPLOADS_DIR / sha256
    exists = (folder / "meta.json").exists()
    if exists:
        os.utime(folder)  # still in use: restart its expiry clock
    return {"upload_id": sha256, "exists": exists}


@app.put("/uploads/{sha256}")
async def upload(sha256: str, request: Request, name: str = "upload") -> dict:
    """Store a local audio/video file sent as the raw request body, addressed by its SHA-256."""
    if not _is_sha256(sha256):
        raise HTTPException(status_code=422, detail="not a sha256 hex digest")
    folder = UPLOADS_DIR / sha256
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = Path(name.replace("\\", "/")).name or "upload"
    suffix = "".join(c for c in Path(safe_name).suffix if c.isalnum() or c == ".")[:10]
    target, tmp = folder / f"media{suffix}", folder / "incoming.part"
    digest, size = hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file is larger than the "
                                        f"MAX_UPLOAD_GB limit ({MAX_UPLOAD_BYTES / 1024 ** 3:g} GB)")
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="the upload was empty")
        if digest.hexdigest() != sha256:
            raise HTTPException(status_code=422, detail="the file arrived damaged (checksum "
                                "mismatch); upload it again")
        for old in folder.glob("media*"):
            old.unlink()
        tmp.replace(target)
        (folder / "meta.json").write_text(json.dumps({"name": safe_name, "size": size}), encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)
    return {"upload_id": sha256, "exists": True, "size": size}


@app.post("/jobs")
def submit(req: JobRequest) -> dict:
    try:
        if req.upload_id:
            video = pipeline.describe_upload(SETTINGS, req.upload_id)
        else:
            video = pipeline.probe(req.url, SETTINGS)
    except pipeline.PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    path = _cache_path(video["id"], req)
    with _lock:
        for job in _jobs.values():  # same request already in flight -> reuse it
            if job["path"] == str(path) and job["status"] in ("queued", "running"):
                return _public(job)
        job_id = uuid.uuid4().hex[:12]
        job = {"job_id": job_id, "status": "queued", "stage": "queued", "video": video,
               "path": str(path), "created_at": time.time(),
               "options": {"url": req.url, "upload_id": req.upload_id, "mode": req.mode,
                           "model": req.model, "language": req.language, "diarize": req.diarize,
                           "num_speakers": req.num_speakers, "min_speakers": req.min_speakers,
                           "max_speakers": req.max_speakers}}
        cached = path / "result.json"
        if cached.exists() and not req.force:
            result = json.loads(cached.read_text(encoding="utf-8"))
            job.update(status="done", stage="done", cached=True, summary=_summary(result))
            _jobs[job_id] = job
            return _public(job)
        if req.mode == "cloud":  # refuse before queueing, so the caller hears about it at once
            problem = usage.over_budget_message(str(DATA_DIR), SETTINGS.cloud_monthly_hours,
                                                video.get("duration") or 0)
            if problem:
                raise HTTPException(status_code=422, detail=problem)
        _jobs[job_id] = job
        _queue.put(job_id)
    return _public(job)


def _job(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id (the service may have "
                            "restarted). Submit the URL again; finished work is cached.")
    return job


@app.get("/jobs/{job_id}")
def status(job_id: str) -> dict:
    return _public(_job(job_id))


@app.get("/jobs/{job_id}/result")
def result(job_id: str, format: Literal["txt", "srt", "json"] = "txt"):
    job = _job(job_id)
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}, not done.")
    path = Path(job["path"])
    if format == "json":
        return JSONResponse(json.loads((path / "result.json").read_text(encoding="utf-8")))
    return PlainTextResponse((path / f"transcript.{format}").read_text(encoding="utf-8"))


# Finished results, whoever submitted them (web UI, Hermes, ...). The web UI polls this to
# copy new transcripts into its library. Results are ordered by (finished_at, result_id);
# the caller passes back the last pair it has seen as since=<finished_at>&after=<result_id>.
# A rerun with force=true rewrites a result, so it is listed again: upsert by result_id.
# finished_at is the file's mtime, so a backwards clock step can hide a result.
_SAFE_PART = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _result_meta(result_file: Path, mtime: float) -> dict | None:
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        "result_id": f"{result_file.parent.parent.name}/{result_file.parent.name}",
        "finished_at": mtime,
        "video": result.get("video"), "language": result.get("language"),
        "mode": result.get("mode", "local"), "model": result.get("model"),
        "diarized": result.get("diarized"), "speakers": result.get("speakers", []),
        "captions": result.get("captions"), "cloud": result.get("cloud"),
        "segments": len(result.get("segments") or []),
    }


@app.get("/results")
def list_results(since: float = 0, after: str = "", limit: int = 200) -> dict:
    limit = max(1, min(limit, 1000))
    found = []
    for result_file in CACHE_DIR.glob("*/*/result.json"):
        try:
            mtime = result_file.stat().st_mtime
        except OSError:
            continue
        result_id = f"{result_file.parent.parent.name}/{result_file.parent.name}"
        if mtime > since or (mtime == since and result_id > after):
            found.append((mtime, result_id, result_file))
    found.sort()
    items = [m for m in (_result_meta(f, t) for t, _id, f in found[:limit]) if m]
    return {"results": items, "more": len(found) > limit}


@app.get("/results/{video_key}/{result_key}")
def get_result(video_key: str, result_key: str, format: Literal["txt", "srt", "json"] = "json"):
    if not (_SAFE_PART.match(video_key) and _SAFE_PART.match(result_key)):
        raise HTTPException(status_code=404, detail="Unknown result.")
    path = CACHE_DIR / video_key / result_key
    if not (path / "result.json").exists():
        raise HTTPException(status_code=404, detail="Unknown result.")
    if format == "json":
        return JSONResponse(json.loads((path / "result.json").read_text(encoding="utf-8")))
    return PlainTextResponse((path / f"transcript.{format}").read_text(encoding="utf-8"))
