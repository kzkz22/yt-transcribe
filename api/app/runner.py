"""Child process that runs one job:  python -m app.runner <job_dir>

Running each job in its own process keeps the HTTP service responsive and
isolates a crash (or a stuck download) to that job.

Reads   <job_dir>/job.json
Writes  <job_dir>/status.json   current stage and device, polled by the server
        <job_dir>/error.txt     message for the user if the job fails
        <result path>/...       transcript files (result.json last)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import cloud, formatting, local, pipeline

log = logging.getLogger("yt-transcribe")


def write_result(path: Path, result: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "transcript.txt").write_text(formatting.to_txt(result), encoding="utf-8")
    (path / "transcript.srt").write_text(formatting.to_srt(result), encoding="utf-8")
    tmp = path / "result.json.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path / "result.json")  # result.json appearing last marks the entry complete


def main(job_dir: Path) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s[job]: %(message)s")
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    settings = pipeline.Settings()
    result_path = Path(job["path"])
    state = {"stage": "starting", "device": None}

    def stage(name: str, device: str | None = None) -> None:
        if device:
            state["device"] = device
        if name == state["stage"] and not device:
            return
        state["stage"] = name
        tmp = job_dir / "status.json.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(job_dir / "status.json")
        log.info("stage: %s (%s)", name, state["device"] or "-")

    try:
        opts = pipeline.Options(**job["options"])
        if opts.mode == "cloud":  # no GPU, no local models: the provider does the work
            state["device"] = "cloud"
            write_result(result_path, cloud.run(opts, job["video"], settings, stage))
        else:
            write_result(result_path, local.run(opts, job["video"], settings, stage))
        return 0
    except pipeline.PipelineError as exc:
        (job_dir / "error.txt").write_text(str(exc), encoding="utf-8")
        log.warning("job failed: %s", exc)
        return 2
    except Exception as exc:
        (job_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        log.exception("job crashed")
        return 3


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
