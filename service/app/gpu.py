"""Decide whether a job can use the GPU, and borrow it from the LLM if needed.

One GPU cannot hold a large LLM and the speech models at the same time. When
LLAMA_SERVER_URL points at a llama-server running in router mode, this module
asks it to unload its models, waits for the VRAM to come back, and lets the
job run on the GPU. The names of the unloaded models are reported back so the
server can load them again once the job's process has exited and released the
VRAM (see load_llm); this does not rely on llama-server's own autoload.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("yt-transcribe")


def free_vram_mb() -> int:
    import torch
    free, _total = torch.cuda.mem_get_info()
    return int(free // (1024 * 1024))


def _call(settings, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if settings.llama_server_api_key:
        headers["Authorization"] = f"Bearer {settings.llama_server_api_key}"
    req = urllib.request.Request(settings.llama_server_url + path, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def unload_llm(settings) -> list[str]:
    """Ask the llama-server router to unload every loaded model; returns their names."""
    models = _call(settings, "/models").get("data", [])
    busy = [m["id"] for m in models
            if (m.get("status") or {}).get("value") in ("loaded", "loading")]
    for name in busy:
        _call(settings, "/models/unload", {"model": name})
        log.info("asked llama-server to unload %s", name)
    return busy


def load_llm(settings, names: list[str], timeout_s: int = 600) -> list[str]:
    """Load the given models again and wait until they are ready; returns problems as text."""
    problems: list[str] = []
    pending: list[str] = []
    for name in names:
        try:
            _call(settings, "/models/load", {"model": name})
            pending.append(name)
            log.info("asked llama-server to load %s again", name)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(f"Could not ask llama-server to load {name} again ({exc}).")
    deadline = time.time() + timeout_s
    while pending and time.time() < deadline:
        time.sleep(2)
        try:
            status = {m["id"]: (m.get("status") or {}) for m in _call(settings, "/models").get("data", [])}
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            continue
        for name in list(pending):
            state = status.get(name, {})
            if state.get("value") == "loaded":
                pending.remove(name)
                log.info("llama-server has %s loaded again", name)
            elif state.get("failed"):
                pending.remove(name)
                problems.append(f"llama-server failed to load {name} again "
                                f"(exit code {state.get('exit_code')}).")
    problems += [f"llama-server did not finish loading {name} within {timeout_s} s." for name in pending]
    return problems


def choose_device(settings, warnings: list[str], on_unload=lambda names: None,
                  wait_s: int = 90) -> str:
    """Return "cuda" or "cpu" for this job, freeing the GPU first when that is possible.

    `on_unload(names)` is called as soon as models have been unloaded, so the
    caller can record them for reloading even if the job dies afterwards.
    """
    if settings.device == "cpu":
        return "cpu"
    import torch
    if not torch.cuda.is_available():
        if settings.device == "cuda":
            warnings.append("DEVICE=cuda but no GPU is visible in the container "
                            "(start it with --runtime=nvidia); ran on CPU.")
        return "cpu"

    need = settings.min_free_vram_mb
    free = free_vram_mb()
    if free >= need:
        return "cuda"

    if not settings.llama_server_url:
        warnings.append(f"Only {free} MB of VRAM was free (need {need} MB) and LLAMA_SERVER_URL "
                        "is not set, so the LLM could not be unloaded; ran on CPU.")
        return "cpu"
    try:
        unloaded = unload_llm(settings)
        if unloaded:
            on_unload(unloaded)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        warnings.append(f"Could not ask llama-server to unload its model ({exc}); ran on CPU.")
        return "cpu"

    deadline = time.time() + wait_s
    while time.time() < deadline:
        free = free_vram_mb()
        if free >= need:
            log.info("GPU is free (%d MB) after unloading %s", free, unloaded or "nothing")
            return "cuda"
        time.sleep(1)
    warnings.append(f"Only {free} MB of VRAM was free after unloading {unloaded or 'no models'} "
                    f"(need {need} MB); ran on CPU. Something else is using the GPU.")
    return "cpu"
