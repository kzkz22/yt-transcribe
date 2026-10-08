#!/usr/bin/env python3
"""Client for the yt-transcribe service. Standard library only.

  yt_transcribe.py run SOURCE [--mode local|cloud] [--model NAME] [--language hu|en]
                              [--speakers N] [--no-diarize] [--force]
  yt_transcribe.py wait JOB_ID
  yt_transcribe.py health

SOURCE is a video URL or the path of a local audio/video file; a local file is
uploaded to the service first (skipped if the service already has it).

`run` submits the job and blocks until the transcript is ready or --wait
seconds have passed (default 570, just under a 600 s terminal limit). If the
job is still running then, it exits with status "running" and a job_id; call
`wait JOB_ID` to continue. When done, the transcript files are saved locally
and their paths are printed as JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = os.environ.get("YT_TRANSCRIBE_URL", "http://localhost:8765")
DEFAULT_OUT = os.environ.get("YT_TRANSCRIBE_OUT", os.path.join("~", "yt-transcripts"))


def _request(server: str, path: str, payload: dict | None = None, timeout: int = 120):
    url = server.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
            if isinstance(detail, list):  # request validation errors
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
        except (ValueError, AttributeError):
            pass
        _fail(f"service answered HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError) as exc:
        _fail(f"cannot reach the yt-transcribe service at {server}: {exc}. "
              "Check that the container is running and that the URL is right.")
    return json.loads(body) if "json" in ctype else body


def _upload(server: str, path: str) -> str:
    """Send a local file to the service unless it is already there; returns its upload id."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    upload_id = digest.hexdigest()
    if _request(server, f"/uploads/{upload_id}").get("exists"):
        return upload_id
    name = urllib.parse.quote(os.path.basename(path))
    url = f"{server.rstrip('/')}/uploads/{upload_id}?name={name}"
    with open(path, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="PUT", headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(os.path.getsize(path))})
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            _fail(f"upload failed with HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            _fail(f"upload to {server} failed: {exc}")
    return upload_id


def _fail(message: str) -> None:
    print(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=1))
    sys.exit(1)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _save(server: str, job: dict, out_dir: str) -> dict:
    video = job.get("video", {})
    # One folder per video and per mode/model, so a local and a cloud run can be compared.
    variant = "".join(c if c.isalnum() or c in "-." else "-"
                      for c in f"{job.get('mode') or 'local'}_{job.get('model') or 'default'}")
    target = os.path.join(os.path.expanduser(out_dir), str(video.get("id") or job["job_id"]), variant)
    os.makedirs(target, exist_ok=True)
    files = {}
    for fmt, name in (("txt", "transcript.txt"), ("srt", "transcript.srt"), ("json", "result.json")):
        content = _request(server, f"/jobs/{job['job_id']}/result?format={fmt}")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=1)
        path = os.path.join(target, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        files[fmt] = path.replace("\\", "/")  # forward slashes also work on Windows and survive copying
    return files


def _follow(server: str, job: dict, wait_s: int, out_dir: str) -> None:
    deadline = time.time() + wait_s
    while job["status"] in ("queued", "running") and time.time() < deadline:
        time.sleep(min(5, max(0.5, deadline - time.time())))
        job = _request(server, f"/jobs/{job['job_id']}")
    if job["status"] == "error":
        _fail(job.get("error", "unknown error"))
    video = job.get("video", {})
    base = {"status": job["status"], "job_id": job["job_id"], "title": video.get("title"),
            "channel": video.get("uploader"), "duration_s": video.get("duration")}
    if job["status"] != "done":
        base.update(stage=job.get("stage"), device=job.get("device"),
                    elapsed_s=job.get("elapsed_s", 0),
                    next=f"still working - run: wait {job['job_id']}")
        if "queue_position" in job:
            base["queue_position"] = job["queue_position"]
        _emit(base)
        return
    base.update(language=job.get("language"), diarized=job.get("diarized"),
                mode=job.get("mode"), model=job.get("model"), device=job.get("device"),
                speakers=job.get("speakers", []), warnings=job.get("warnings", []),
                characters=job.get("characters"), cached=bool(job.get("cached")),
                elapsed_s=job.get("elapsed_s", 0), files=_save(server, job, out_dir))
    if job.get("cloud"):
        base["cloud"] = job["cloud"]
    _emit(base)


def main() -> None:
    # Windows consoles default to a legacy code page; force UTF-8 so accented titles never crash.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=DEFAULT_SERVER, help="service base URL")
    common.add_argument("--out-dir", default=DEFAULT_OUT, help="where transcripts are saved")
    common.add_argument("--wait", type=int, default=570,
                        help="seconds to wait before returning status 'running'")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", parents=[common], help="submit a video and wait")
    run.add_argument("source", help="video URL, or path of a local audio/video file")
    run.add_argument("--mode", choices=["local", "cloud"],
                     help="where to transcribe (default: the service's default, normally local)")
    run.add_argument("--model", help="local: large-v3 | large-v3-turbo; "
                                     "cloud: auto | universal-2 | universal-3.5-pro")
    run.add_argument("--language", help="hu, en, ... (default: auto-detect)")
    run.add_argument("--speakers", type=int, help="exact number of speakers, if known")
    run.add_argument("--min-speakers", type=int)
    run.add_argument("--max-speakers", type=int)
    run.add_argument("--no-diarize", action="store_true", help="skip speaker separation (faster)")
    run.add_argument("--force", action="store_true", help="ignore the cached result")

    wait = sub.add_parser("wait", parents=[common], help="keep waiting for a submitted job")
    wait.add_argument("job_id")

    sub.add_parser("health", parents=[common], help="check that the service is reachable")

    args = parser.parse_args()
    if args.cmd == "health":
        _emit(_request(args.server, "/health", timeout=15))
    elif args.cmd == "run":
        local_path = os.path.expanduser(args.source)
        is_url = args.source.lower().startswith(("http://", "https://"))
        if not is_url and not os.path.isfile(local_path):
            _fail(f"'{args.source}' is neither a URL nor an existing file.")
        source = {"url": args.source} if is_url else {"upload_id": _upload(args.server, local_path)}
        payload = {**source, "mode": args.mode, "model": args.model,
                   "language": args.language, "diarize": not args.no_diarize,
                   "num_speakers": args.speakers, "min_speakers": args.min_speakers,
                   "max_speakers": args.max_speakers, "force": args.force}
        _follow(args.server, _request(args.server, "/jobs", payload), args.wait, args.out_dir)
    else:
        _follow(args.server, _request(args.server, f"/jobs/{args.job_id}"), args.wait, args.out_dir)


if __name__ == "__main__":
    main()
