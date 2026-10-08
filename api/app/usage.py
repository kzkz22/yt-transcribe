"""Monthly budget for cloud transcription.

Keeps a small JSON file with the seconds of audio sent to the cloud provider
per calendar month, and refuses new cloud jobs once the configured cap
(CLOUD_MONTHLY_HOURS) would be exceeded. It is a safety net against surprise
bills, not an invoice: the provider's own dashboard is the source of truth.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _file(data_dir: str) -> Path:
    return Path(data_dir) / "cloud_usage.json"


def month_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(now))


def _load(data_dir: str) -> dict:
    try:
        return json.loads(_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def used_seconds(data_dir: str) -> float:
    return float(_load(data_dir).get(month_key(), 0))


def add(data_dir: str, seconds: float) -> None:
    data = _load(data_dir)
    data[month_key()] = round(float(data.get(month_key(), 0)) + max(float(seconds), 0), 1)
    path = _file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def over_budget_message(data_dir: str, monthly_hours: float, duration_s: float) -> str | None:
    """None if a job of `duration_s` still fits this month's cap, otherwise the reason."""
    if monthly_hours <= 0:
        return None
    used = used_seconds(data_dir)
    if used + max(duration_s, 0) <= monthly_hours * 3600:
        return None
    return (f"The monthly cloud budget would be exceeded: {used / 3600:.1f} of {monthly_hours:g} "
            f"hours are used this month and this job needs {duration_s / 3600:.1f} more. "
            "Use mode=local, or raise CLOUD_MONTHLY_HOURS on the container.")
