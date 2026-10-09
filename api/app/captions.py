"""Captions mode: the video's own subtitles from YouTube, through yt-dlp. No GPU, no cost.

Exactly ONE track is requested, in the video's language:
  1. the uploader's own subtitles in that language (`subtitles`),
  2. else YouTube's automatic captions of the original audio (`automatic_captions`,
     key `<lang>-orig`; a plain `<lang>` key only when there is no `-orig` key at all,
     because next to an `-orig` track every plain key is a machine translation of it).
Machine translations (`hu`, `de` next to `en-orig`; `hu-en` of own subtitles) and the
`-orig` tracks of auto-dubbed audio in other languages are never used. Asking for many
tracks at once makes YouTube answer HTTP 429.

The track is fetched as `json3`: own subtitles come as timed lines, automatic captions as
timed words (the VTT form repeats every line). Both become sentence-sized segments with the
same grouping as cloud mode. There are no speakers. Without a suitable track the job fails;
the caller decides whether to use local or cloud mode instead.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable

from . import formatting
from .pipeline import Options, PipelineError, Settings, _ydl_opts

log = logging.getLogger("yt-transcribe")

_ANNOTATION = re.compile(r"^\[[^\]]*\]$")  # [Music], [Zene], [Applause]


def _base(code: str) -> str:
    return code.split("-")[0].lower()


def _script(code: str) -> str | None:
    """The script subtag (zh-Hans, sr-Latn), which must not be swapped for another one."""
    parts = code.split("-")
    return parts[1].title() if len(parts) > 1 and len(parts[1]) == 4 and parts[1].isalpha() else None


def choose_track(info: dict, language: str | None = None) -> tuple[str, str, list] | None:
    """Return (kind, code, formats) of the track to use, or None. kind is "manual" or "auto".

    `language` (the caller's) wins over the video's own `language` field.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    lang = (language or info.get("language") or "").strip()
    if not lang:
        origs = [k for k in auto if k.endswith("-orig")]
        if len(origs) == 1:
            lang = origs[0][: -len("-orig")]
        elif len(manual) == 1:
            lang = next(iter(manual))
        else:
            return None
    want = _base(lang)
    for code in manual:
        if code.lower() == lang.lower():
            return "manual", code, manual[code]
    for code in manual:
        if _base(code) == want and _script(code) == _script(lang):
            return "manual", code, manual[code]
    origs = [code for code in auto if code.endswith("-orig")]
    for code in origs:
        if code[: -len("-orig")].lower() in (lang.lower(), want):
            return "auto", code, auto[code]
    if not origs:  # older videos: the recognised track has no -orig key
        for code in auto:
            if code.lower() in (lang.lower(), want):
                return "auto", code, auto[code]
    return None


def words_from_json3(data: dict, kind: str) -> list[dict]:
    """Timed units from a json3 subtitle file: whole lines (own subtitles) or words (automatic)."""
    units: list[dict] = []
    for event in data.get("events") or []:
        segs = event.get("segs")
        if not segs or event.get("aAppend"):
            continue
        start = int(event.get("tStartMs") or 0)
        end = start + int(event.get("dDurationMs") or 0)
        if kind == "auto":
            for seg in segs:
                text = (seg.get("utf8") or "").strip()
                if text:
                    units.append({"text": text, "start": (start + int(seg.get("tOffsetMs") or 0)) / 1000,
                                  "event_end": end / 1000})
        else:
            text = " ".join("".join(seg.get("utf8") or "" for seg in segs).split())
            if text and not _ANNOTATION.match(text):
                units.append({"text": text, "start": start / 1000, "end": end / 1000})
    for i, unit in enumerate(units):  # a word lasts until the next one starts (at most 2 s)
        if "end" not in unit:
            nxt = units[i + 1]["start"] if i + 1 < len(units) else unit["event_end"]
            unit["end"] = max(unit["start"], min(nxt, unit["start"] + 2.0, unit["event_end"]))
        unit.pop("event_end", None)
    return [u for u in units if not _ANNOTATION.match(u["text"])]


def run(opts: Options, video: dict, settings: Settings,
        stage: Callable[..., None] = lambda *a: None) -> dict:
    """Fetch the captions of one video and return the same result shape as the other modes."""
    if not opts.url:
        raise PipelineError("Captions mode needs a video URL; uploaded files have no captions.")
    import yt_dlp

    stage("download")
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(settings)) as ydl:
            info = ydl.extract_info(opts.url, download=False)
            track = choose_track(info, opts.language)
            if track is None:
                lang = opts.language or info.get("language") or "unknown"
                raise PipelineError(
                    f"This video has no captions in its language ('{lang}'). "
                    "Use mode=local or mode=cloud to transcribe the audio instead.")
            kind, code, formats = track
            fmt = next((f for f in formats if f.get("ext") == "json3" and f.get("url")), None)
            if fmt is None:
                raise PipelineError(f"The '{code}' captions are not offered in json3 format.")
            log.info("captions: %s track %s", kind, code)
            raw = ydl.urlopen(fmt["url"]).read()
    except PipelineError:
        raise
    except Exception as exc:  # yt-dlp and network errors
        raise PipelineError(f"yt-dlp could not download the captions: {exc}") from exc

    stage("write")
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except ValueError as exc:
        raise PipelineError("The captions file could not be read.") from exc
    segments = formatting.clean_segments(formatting.segments_from_words(words_from_json3(data, kind)))
    if not segments:
        raise PipelineError("The captions track is empty.")
    for seg in segments:
        seg["speaker"] = None
    warnings = list(opts.warnings)
    if kind == "auto":
        warnings.append("Made from YouTube's automatic captions: expect recognition errors and "
                        "no speaker labels. For a better transcript use mode=local or mode=cloud.")
    return {
        "video": video, "language": _base(code), "mode": "captions", "model": f"youtube-{kind}",
        "device": None, "diarized": False, "speakers": [], "warnings": warnings,
        "captions": {"kind": kind, "track": code}, "segments": segments,
    }
