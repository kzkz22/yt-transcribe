"""Pure formatting helpers: speaker relabelling, turns, TXT and SRT output.

No heavy imports here, so this module is unit-testable without torch/whisperx.
"""
from __future__ import annotations

SPEAKER_WORD = {"hu": "Szereplő", "en": "Speaker"}
UNKNOWN_WORD = {"hu": "Ismeretlen", "en": "Unknown"}


def speaker_word(language: str | None, override: str | None = None) -> str:
    if override:
        return override
    return SPEAKER_WORD.get((language or "").lower(), "Speaker")


def unknown_word(language: str | None) -> str:
    return UNKNOWN_WORD.get((language or "").lower(), "Unknown")


def clean_segments(raw_segments: list[dict]) -> list[dict]:
    """Keep only start/end/text/speaker, drop empty text, repair missing times."""
    out: list[dict] = []
    last_end = 0.0
    for seg in raw_segments:
        text = " ".join(str(seg.get("text", "")).split())
        if not text:
            continue
        start = _num(seg.get("start"), last_end)
        end = _num(seg.get("end"), start)
        if end < start:
            end = start
        out.append({"start": start, "end": end, "text": text, "speaker": seg.get("speaker")})
        last_end = end
    return out


def _num(value, fallback: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if f != f else f  # NaN check


def segments_from_words(words: list[dict], max_chars: int = 220, max_seconds: float = 15.0) -> list[dict]:
    """Group a word list (text, start, end in seconds, optional speaker) into sentence-sized segments.

    A segment ends at a speaker change, after sentence-ending punctuation, or when it
    would grow past `max_chars` / `max_seconds`, so subtitle cues stay readable.
    """
    segments: list[dict] = []
    cur: dict | None = None
    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        speaker = word.get("speaker")
        start, end = float(word.get("start") or 0), float(word.get("end") or 0)
        if cur is not None and (
            cur["speaker"] != speaker
            or len(cur["text"]) + 1 + len(text) > max_chars
            or end - cur["start"] > max_seconds
        ):
            segments.append(cur)
            cur = None
        if cur is None:
            cur = {"start": start, "end": end, "text": text, "speaker": speaker}
        else:
            cur["text"] += " " + text
            cur["end"] = end
        if text[-1] in ".!?…":
            segments.append(cur)
            cur = None
    if cur is not None:
        segments.append(cur)
    return segments


def relabel_speakers(segments: list[dict], word: str, unknown: str) -> list[dict]:
    """Rename raw diarization ids (SPEAKER_03, ...) to '<word> 1..N' by first appearance.

    A segment without a speaker inherits the previous segment's speaker (short
    interjections often fall between diarization turns); if there is none yet
    it gets the `unknown` label. Returns the speaker summary list.
    """
    mapping: dict[str, str] = {}
    prev = None
    for seg in segments:
        raw = seg.get("speaker")
        if raw is None:
            seg["speaker"] = prev if prev is not None else unknown
        else:
            if raw not in mapping:
                mapping[raw] = f"{word} {len(mapping) + 1}"
            seg["speaker"] = mapping[raw]
        prev = seg["speaker"]

    totals: dict[str, float] = {}
    for seg in segments:
        totals[seg["speaker"]] = totals.get(seg["speaker"], 0.0) + (seg["end"] - seg["start"])
    spoken = sum(totals.values()) or 1.0
    order = list(mapping.values()) + ([unknown] if unknown in totals else [])
    return [
        {"label": label, "seconds": round(totals.get(label, 0.0), 1),
         "share": round(totals.get(label, 0.0) / spoken, 3)}
        for label in order
    ]


def to_turns(segments: list[dict], max_chars: int = 900, max_gap: float = 20.0) -> list[dict]:
    """Merge consecutive segments of the same speaker into readable paragraphs.

    A new paragraph starts when the speaker changes, when the paragraph would
    grow past `max_chars`, or after a pause longer than `max_gap` seconds, so
    single-speaker videos still get regular timestamps.
    """
    turns: list[dict] = []
    for seg in segments:
        cur = turns[-1] if turns else None
        if (
            cur is not None
            and cur["speaker"] == seg.get("speaker")
            and len(cur["text"]) + 1 + len(seg["text"]) <= max_chars
            and seg["start"] - cur["end"] <= max_gap
        ):
            cur["text"] += " " + seg["text"]
            cur["end"] = seg["end"]
        else:
            turns.append({"start": seg["start"], "end": seg["end"],
                          "speaker": seg.get("speaker"), "text": seg["text"]})
    return turns


def hms(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def srt_time(seconds: float) -> str:
    ms = int(round(max(seconds, 0) * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def to_txt(result: dict) -> str:
    video = result.get("video", {})
    lines = [
        f"# {video.get('title') or video.get('id') or 'transcript'}",
        f"URL: {video.get('url', '')}",
        f"Channel: {video.get('uploader') or '-'}",
        f"Duration: {hms(video.get('duration') or 0)}",
        f"Language: {result.get('language', '?')}",
        f"Transcribed by: {result.get('model', '?')} ({result.get('mode', 'local')})",
    ]
    if result.get("diarized"):
        parts = [f"{s['label']} ({round(s['share'] * 100)}%)" for s in result.get("speakers", [])]
        lines.append(f"Speakers ({len(parts)}): " + ", ".join(parts))
    else:
        lines.append("Speakers: not separated")
    for warning in result.get("warnings", []):
        lines.append(f"Warning: {warning}")
    lines.append("")
    for turn in to_turns(result["segments"]):
        who = f" {turn['speaker']}:" if turn.get("speaker") else ""
        lines.append(f"[{hms(turn['start'])}]{who} {turn['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_srt(result: dict) -> str:
    cues = []
    for i, seg in enumerate(result["segments"], 1):
        end = max(seg["end"], seg["start"] + 0.3)
        who = f"[{seg['speaker']}] " if seg.get("speaker") else ""
        cues.append(f"{i}\n{srt_time(seg['start'])} --> {srt_time(end)}\n{who}{seg['text']}\n")
    return "\n".join(cues)
