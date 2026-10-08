"""Child process that runs one transcription:  python -m gpuworker.job <job_dir>

Running each job in its own process means that when it ends - normally or
not - all RAM and VRAM go back to the system, and a crash or an out-of-memory
kill takes down only the job, not the worker's HTTP server.

Reads   <job_dir>/job.json      options, audio file name and its SHA-256
        <job_dir>/audio         the audio to transcribe
Writes  <job_dir>/status.json   current stage and device, polled by the server
        <job_dir>/error.txt     message for the user if the job fails
        <job_dir>/result.json   the raw result, written last
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import asr
from .settings import Settings

log = logging.getLogger("gpu-worker")


def main(job_dir: Path) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s[job]: %(message)s")
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    opts, settings = job["options"], Settings()
    state = {"stage": "starting", "device": None}

    def stage(name: str) -> None:
        state["stage"] = name
        tmp = job_dir / "status.json.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(job_dir / "status.json")
        log.info("stage: %s (%s)", name, state["device"] or "-")

    try:
        warnings: list[str] = []
        device = asr.choose_device(settings, warnings)
        state["device"] = device
        checkpoint = asr.checkpoint_path(settings, job["sha256"], opts["model"], opts.get("language"))
        audio = str(job_dir / "audio")
        try:
            result = asr.run(audio, opts, settings, device, checkpoint, stage)
        except asr.JobError:
            raise
        except Exception as exc:
            if device != "cuda":
                raise
            log.warning("GPU run failed (%s: %s); retrying on CPU", type(exc).__name__, exc)
            warnings.append(f"The GPU run failed ({type(exc).__name__}); finished on CPU.")
            state["device"] = "cpu"
            result = asr.run(audio, opts, settings, "cpu", checkpoint, stage)
        result["warnings"] = warnings + result["warnings"]
        tmp = job_dir / "result.json.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False, default=asr.json_default),
                       encoding="utf-8")
        tmp.replace(job_dir / "result.json")
        return 0
    except asr.JobError as exc:
        (job_dir / "error.txt").write_text(str(exc), encoding="utf-8")
        log.warning("job failed: %s", exc)
        return 2
    except Exception as exc:
        (job_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        log.exception("job crashed")
        return 3


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
