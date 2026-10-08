"""Test harness: runs the real API and the real GPU worker against stand-ins.

`tests/fakes` replaces torch, whisperx and yt_dlp (put first on PYTHONPATH of
both processes) and provides a small HTTP stand-in for AssemblyAI. The API
talks to the worker directly here; in production llama-swap sits between them
and only forwards the request. The fakes record what they were asked to do in
`<state>/calls.log`, and behave differently when flag files exist in `<state>`
(crash_once, gpu_fail_once, aai_error, aai_no_speakers).

Needs: pytest, fastapi, uvicorn, numpy, and ffmpeg/ffprobe on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes"
API = ROOT / "api"
WORKER = ROOT / "gpu-worker"
CLIENT = ROOT / "hermes-skill" / "yt-transcribe" / "scripts" / "yt_transcribe.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are needed for these tests")
    d = tmp_path_factory.mktemp("media")
    files = {
        "audio": d / "yt_audio.webm",
        "video": d / "Interjú felvétel.mp4",
        "silent": d / "silent.mp4",
    }
    run = lambda *a: subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a], check=True)
    run("-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "libopus", str(files["audio"]))
    run("-f", "lavfi", "-i", "testsrc=duration=5:size=160x120:rate=5", "-f", "lavfi", "-i",
        "sine=frequency=300:duration=5", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(files["video"]))
    run("-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=5", "-c:v", "mpeg4", str(files["silent"]))
    return files


class Stack:
    """The API, the GPU worker and fake AssemblyAI, all as subprocesses."""

    def __init__(self, tmp: Path, media: dict[str, Path], **env_overrides: str):
        self.tmp, self.state = tmp, tmp / "state"
        self.state.mkdir()
        self.procs: list[subprocess.Popen] = []
        self.aai_port, self.worker_port, self.port = _free_port(), _free_port(), _free_port()
        base = {**os.environ, "FAKE_DIR": str(self.state), "PYTHONUNBUFFERED": "1",
                "FAKE_SLEEP": "0"}
        self._spawn([sys.executable, str(FAKES / "fake_aai.py")], {**base, "PORT": str(self.aai_port)})
        worker_env = {k: v for k, v in {
            **base,
            "PYTHONPATH": f"{FAKES}{os.pathsep}{WORKER}",
            "DATA_DIR": str(tmp / "worker-data"),
            "HF_TOKEN": "hf_fake",
            "FAKE_CUDA": "1",
            **{k[len("worker__"):]: v for k, v in env_overrides.items() if k.startswith("worker__")},
        }.items() if v is not None}
        self._spawn([sys.executable, "-m", "uvicorn", "gpuworker.server:app", "--host", "127.0.0.1",
                     "--port", str(self.worker_port), "--log-level", "warning"], worker_env, cwd=WORKER)
        self.env = {k: v for k, v in {
            **base,
            "PYTHONPATH": f"{FAKES}{os.pathsep}{API}",
            "DATA_DIR": str(tmp / "data"),
            "FAKE_MEDIA": str(media["audio"]),
            "WORKER_URL": f"http://127.0.0.1:{self.worker_port}",
            "ASSEMBLYAI_API_KEY": "key-ok",
            "ASSEMBLYAI_BASE_URL": f"http://127.0.0.1:{self.aai_port}",
            "CLOUD_MONTHLY_HOURS": "10",
            **{k: v for k, v in env_overrides.items() if not k.startswith("worker__")},
        }.items() if v is not None}
        self._spawn([sys.executable, "-m", "uvicorn", "app.server:app", "--host", "127.0.0.1",
                     "--port", str(self.port), "--log-level", "warning"], self.env, cwd=API)
        _wait_http(f"http://127.0.0.1:{self.worker_port}/health")
        _wait_http(f"{self.url}/health")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _spawn(self, cmd, env, cwd=None):
        log = open(self.tmp / f"{Path(cmd[3] if 'uvicorn' in cmd else cmd[1]).name}.log", "ab")
        self.procs.append(subprocess.Popen(cmd, env=env, cwd=cwd, stdout=log, stderr=log))

    def client(self, *args: str, out: str = "out") -> dict:
        res = subprocess.run([sys.executable, str(CLIENT), *args, "--server", self.url,
                              *(["--out-dir", str(self.tmp / out)] if args[0] != "health" else [])],
                             capture_output=True, text=True, timeout=120)
        return json.loads(res.stdout)

    def calls(self) -> list[str]:
        f = self.state / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def clear_calls(self) -> None:
        (self.state / "calls.log").write_text("")

    def flag(self, name: str) -> None:
        (self.state / name).touch()

    def stop_worker(self) -> None:
        """Simulates a GPU server that is switched off."""
        self.procs[1].terminate()
        self.procs[1].wait(timeout=5)

    def stop(self) -> None:
        for p in reversed(self.procs):
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture
def stack(tmp_path, media):
    s = Stack(tmp_path, media)
    yield s
    s.stop()


@pytest.fixture
def make_stack(tmp_path, media):
    """For tests that need non-default service settings."""
    made: list[Stack] = []

    def _make(**env) -> Stack:
        """Keyword arguments are environment variables of the API; prefix one with
        `worker__` to set it on the GPU worker instead (worker__HF_TOKEN="")."""
        s = Stack(tmp_path / f"stack{len(made)}", media, **env) if made else Stack(tmp_path, media, **env)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.stop()
