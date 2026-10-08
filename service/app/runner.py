"""Child process that runs one job:  python -m app.runner <job_dir>

Running each job in its own process means that when it ends - normally or
not - all RAM and VRAM go back to the system, and a crash or an out-of-memory
kill takes down only the job, not the HTTP service.

Reads   <job_dir>/job.json
Writes  <job_dir>/status.json   current stage, polled by the server
        <job_dir>/error.txt     message for the user if the job fails
        <job_dir>/unloaded.json LLM models unloaded to free the GPU (the server reloads them)
        <result path>/...       transcript files (result.json last)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import cloud, formatting, gpu, pipeline

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

    def stage(name: str) -> None:
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
            return 0

        checkpoint = pipeline.asr_checkpoint_path(result_path.parent, opts.model, opts.language)

        stage("prepare-gpu")
        device = gpu.choose_device(
            settings, opts.warnings,
            on_unload=lambda names: (job_dir / "unloaded.json").write_text(
                json.dumps(names), encoding="utf-8"))
        state["device"] = device
        try:
            result = pipeline.run(opts, job["video"], settings, device, stage, checkpoint)
        except pipeline.PipelineError:
            raise
        except Exception as exc:
            if device != "cuda":
                raise
            # Typically the LLM came back while we were working and took the VRAM.
            log.warning("GPU run failed (%s: %s); retrying on CPU", type(exc).__name__, exc)
            opts.warnings.append(f"The GPU run failed ({type(exc).__name__}); finished on CPU.")
            state["device"] = "cpu"
            result = pipeline.run(opts, job["video"], settings, "cpu", stage, checkpoint)
        write_result(result_path, result)
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
