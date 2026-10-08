"""Test harness: runs the real service against stand-ins for the heavy or external parts.

`tests/fakes` replaces torch, whisperx and yt_dlp (put first on PYTHONPATH of the
service process), and provides small HTTP stand-ins for llama-server (router
mode) and AssemblyAI. The fakes record what they were asked to do in
`<state>/calls.log`, and behave differently when flag files exist in `<state>`
(crash_once, gpu_fail_once, load_fails, aai_error, aai_no_speakers).

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
SERVICE = ROOT / "service"
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
    """The service plus fake llama-server and fake AssemblyAI, all as subprocesses."""

    def __init__(self, tmp: Path, media: dict[str, Path], **env_overrides: str):
        self.tmp, self.state = tmp, tmp / "state"
        self.state.mkdir()
        self.procs: list[subprocess.Popen] = []
        self.llama_port, self.aai_port, self.port = _free_port(), _free_port(), _free_port()
        base = {**os.environ, "FAKE_DIR": str(self.state), "PYTHONUNBUFFERED": "1"}
        for script, port in (("fake_llama.py", self.llama_port), ("fake_aai.py", self.aai_port)):
            self._spawn([sys.executable, str(FAKES / script)], {**base, "PORT": str(port)})
        self.env = {
            **base,
            "PYTHONPATH": f"{FAKES}{os.pathsep}{SERVICE}",
            "DATA_DIR": str(tmp / "data"),
            "HF_TOKEN": "hf_fake",
            "FAKE_CUDA": "1",
            "FAKE_SLEEP": "0",
            "FAKE_MEDIA": str(media["audio"]),
            "LLAMA_SERVER_URL": f"http://127.0.0.1:{self.llama_port}",
            "ASSEMBLYAI_API_KEY": "key-ok",
            "ASSEMBLYAI_BASE_URL": f"http://127.0.0.1:{self.aai_port}",
            "CLOUD_MONTHLY_HOURS": "10",
            **env_overrides,
        }
        self.env = {k: v for k, v in self.env.items() if v is not None}
        self._spawn([sys.executable, "-m", "uvicorn", "app.server:app", "--host", "127.0.0.1",
                     "--port", str(self.port), "--log-level", "warning"], self.env, cwd=SERVICE)
        _wait_http(f"{self.url}/health")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _spawn(self, cmd, env, cwd=None):
        log = open(self.tmp / f"{Path(cmd[1] if len(cmd) > 1 else cmd[0]).name}.log", "ab")
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
        s = Stack(tmp_path / f"stack{len(made)}", media, **env) if made else Stack(tmp_path, media, **env)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.stop()
