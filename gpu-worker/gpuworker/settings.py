"""Settings of the GPU worker, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _physical_cores() -> int:
    """Physical core count (hyper-threads do not help Whisper on CPU); falls back to logical."""
    try:
        cores = set()
        for cpu in os.sched_getaffinity(0):
            path = f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
            with open(path, encoding="ascii") as fh:
                cores.add(fh.read().strip())
        if cores:
            return len(cores)
    except (OSError, AttributeError):
        pass
    return os.cpu_count() or 4


@dataclass
class Settings:
    device: str = os.environ.get("DEVICE", "auto").lower()           # auto | cpu | cuda
    models: tuple = tuple(m.strip() for m in os.environ.get(
        "WHISPER_MODELS", "large-v3,large-v3-turbo").split(",") if m.strip())
    compute_type: str = os.environ.get("COMPUTE_TYPE", "")           # empty = by device
    batch_size: int = int(os.environ.get("BATCH_SIZE", "0"))         # 0 = by device
    cpu_threads: int = int(os.environ.get("CPU_THREADS", "0"))       # 0 = physical cores
    hf_token: str = os.environ.get("HF_TOKEN", "")
    diarize_model: str = os.environ.get("DIARIZE_MODEL", "")         # empty = whisperx default
    data_dir: str = os.environ.get("DATA_DIR", "/data")
    asr_cache_days: float = float(os.environ.get("ASR_CACHE_DAYS", "14"))
    max_audio_bytes: int = int(float(os.environ.get("MAX_AUDIO_MB", "2048")) * 1024 ** 2)

    def compute_type_for(self, device: str) -> str:
        return self.compute_type or ("float16" if device == "cuda" else "int8")

    def batch_size_for(self, device: str) -> int:
        return self.batch_size or (8 if device == "cuda" else 4)

    def threads(self) -> int:
        return self.cpu_threads or _physical_cores()
