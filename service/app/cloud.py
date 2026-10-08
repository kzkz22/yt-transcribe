"""Cloud mode: transcription and speaker labels from AssemblyAI's pre-recorded API.

The provider does not accept YouTube links, only an audio file, so the audio
is fetched here exactly as in local mode, reduced to a small mono FLAC, and
uploaded. Nothing in this mode touches the GPU.

API used (https://www.assemblyai.com/docs):
  POST   /v2/upload            raw audio            -> {upload_url}
  POST   /v2/transcript        {audio_url, ...}     -> {id, status}
  GET    /v2/transcript/{id}                        -> status, words, utterances, ...
  DELETE /v2/transcript/{id}   removes the transcript and the uploaded audio
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable

from . import formatting, usage
from .pipeline import Options, PipelineError, Settings, source_audio

log = logging.getLogger("yt-transcribe")

# Names callers use -> identifiers the API expects.
MODELS = {"universal-2": "universal-2", "universal-3.5-pro": "universal-3-5-pro"}
# Languages of Universal-3.5 Pro per the provider's supported-languages page (2026-10).
# Universal-2 covers 99 languages, Hungarian among them.
PRO_LANGUAGES = frozenset({"en", "es", "fr", "de", "it", "pt", "ar", "da", "nl", "fi", "he", "hi",
                           "ja", "zh", "no", "sv", "tr", "vi"})
# USD per hour of audio, from the provider's price list (2026-10). Used for an estimate only.
PRICE_PER_HOUR = {"universal-2": 0.15, "universal-3-5-pro": 0.21}
DIARIZATION_PER_HOUR = 0.02


def choices() -> list[str]:
    return ["auto", *MODELS]


def resolve_model(requested: str | None, language: str | None) -> str:
    """Turn the caller's model choice into the API identifier, enforcing language support.

    auto: Universal-3.5 Pro when the stated language is one it supports; otherwise
    Universal-2 (also when the language is to be detected, as that may be Hungarian).
    """
    requested = (requested or "auto").lower()
    lang = (language or "").split("-")[0].split("_")[0].lower() or None
    if requested == "auto":
        return MODELS["universal-3.5-pro"] if lang in PRO_LANGUAGES else MODELS["universal-2"]
    if requested not in MODELS:
        raise PipelineError(f"Unknown cloud model '{requested}'. Choose one of: {', '.join(choices())}.")
    if requested == "universal-3.5-pro" and lang is not None and lang not in PRO_LANGUAGES:
        raise PipelineError(
            f"universal-3.5-pro does not support the language '{lang}'. "
            "Use model=universal-2 (or model=auto) for this language.")
    return MODELS[requested]


def estimate_cost(api_model: str, seconds: float, diarize: bool) -> float:
    rate = PRICE_PER_HOUR.get(api_model, 0) + (DIARIZATION_PER_HOUR if diarize else 0)
    return round(rate * max(seconds, 0) / 3600, 4)


def _api(settings: Settings, method: str, path: str, body=None, headers=None, timeout: int = 60) -> dict:
    data = body
    hdrs = {"authorization": settings.assemblyai_api_key, **(headers or {})}
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        hdrs["content-type"] = "application/json"
    req = urllib.request.Request(settings.assemblyai_base_url + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        hint = " Check ASSEMBLYAI_API_KEY." if exc.code in (401, 403) else ""
        raise PipelineError(f"AssemblyAI answered HTTP {exc.code}: {detail}.{hint}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PipelineError(f"AssemblyAI could not be reached ({exc}). Cloud mode needs internet "
                            "access; nothing was switched to local mode.") from exc


def _to_flac(src: str, workdir: str) -> str:
    """Mono 16 kHz FLAC: lossless for speech models and a fraction of a video's size."""
    dst = os.path.join(workdir, "upload.flac")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src, "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "flac", dst],
                       check=True, capture_output=True, timeout=3600)
    except subprocess.CalledProcessError as exc:
        raise PipelineError("ffmpeg could not extract the audio: "
                            + exc.stderr.decode("utf-8", "replace")[-300:]) from exc
    return dst


def _request_body(opts: Options, api_model: str, upload_url: str) -> dict:
    body: dict = {"audio_url": upload_url, "speech_models": [api_model],
                  "speaker_labels": bool(opts.diarize), "punctuate": True, "format_text": True}
    if opts.language:
        body["language_code"] = opts.language
        body["language_detection"] = False
    else:
        body["language_detection"] = True
    if opts.diarize:
        if opts.num_speakers:
            body["speakers_expected"] = opts.num_speakers
        elif opts.min_speakers or opts.max_speakers:
            body["speaker_options"] = {
                k: v for k, v in (("min_speakers_expected", opts.min_speakers),
                                  ("max_speakers_expected", opts.max_speakers)) if v}
    return body


def _words(transcript: dict) -> list[dict]:
    """The provider's words with times in seconds. Utterances carry the speaker labels."""
    source = []
    if transcript.get("utterances"):
        for utt in transcript["utterances"]:
            for word in utt.get("words") or []:
                source.append({**word, "speaker": word.get("speaker") or utt.get("speaker")})
    else:
        source = transcript.get("words") or []
    return [{"text": w.get("text", ""), "start": (w.get("start") or 0) / 1000,
             "end": (w.get("end") or 0) / 1000, "speaker": w.get("speaker")} for w in source]


def run(opts: Options, video: dict, settings: Settings,
        stage: Callable[[str], None] = lambda s: None) -> dict:
    """Transcribe one video in the cloud and return the same result shape as local mode."""
    if not settings.assemblyai_api_key:
        raise PipelineError("Cloud mode needs ASSEMBLYAI_API_KEY to be set on the container.")
    api_model = opts.model
    problem = usage.over_budget_message(settings.data_dir, settings.cloud_monthly_hours,
                                        video.get("duration") or 0)
    if problem:
        raise PipelineError(problem)

    workdir = tempfile.mkdtemp(prefix="ytt_")
    transcript_id = None
    try:
        stage("download")
        source = source_audio(opts, settings, workdir)
        stage("prepare-audio")
        flac = _to_flac(source, workdir)

        stage("upload")
        with open(flac, "rb") as fh:
            upload = _api(settings, "POST", "/v2/upload", body=fh, timeout=1800, headers={
                "content-type": "application/octet-stream",
                "content-length": str(os.path.getsize(flac))})

        stage("transcribe")
        created = _api(settings, "POST", "/v2/transcript",
                       body=_request_body(opts, api_model, upload["upload_url"]))
        transcript_id = created["id"]
        deadline = time.time() + settings.cloud_timeout_s
        transcript = created
        while transcript.get("status") not in ("completed", "error"):
            if time.time() > deadline:
                raise PipelineError("AssemblyAI did not finish within "
                                    f"{settings.cloud_timeout_s // 60} minutes.")
            time.sleep(3)
            transcript = _api(settings, "GET", f"/v2/transcript/{transcript_id}")
        if transcript["status"] == "error":
            raise PipelineError(f"AssemblyAI could not transcribe the audio: {transcript.get('error')}")

        deleted = False
        if settings.cloud_delete_after:
            try:  # also removes the uploaded audio on the provider's side
                _api(settings, "DELETE", f"/v2/transcript/{transcript_id}")
                deleted = True
            except PipelineError as exc:
                log.warning("could not delete the transcript at the provider: %s", exc)
            transcript_id = None  # handled; the finally block must not try again

        stage("write")
        seconds = float(transcript.get("audio_duration") or video.get("duration") or 0)
        usage.add(settings.data_dir, seconds)
        language = (transcript.get("language_code") or opts.language or "en").split("_")[0].lower()
        segments = formatting.clean_segments(formatting.segments_from_words(_words(transcript)))
        if not segments:
            raise PipelineError("No speech was detected in the audio.")

        warnings = list(opts.warnings)
        diarized = opts.diarize and any(seg.get("speaker") for seg in segments)
        if opts.diarize and not diarized:
            warnings.append("The provider returned no speaker labels (speaker separation may not "
                            "be available for this language); the transcript has no speakers.")
        speakers: list[dict] = []
        if diarized:
            speakers = formatting.relabel_speakers(
                segments, formatting.speaker_word(language, settings.speaker_label or None),
                formatting.unknown_word(language))
        else:
            for seg in segments:
                seg["speaker"] = None
        used_model = transcript.get("speech_model_used") or api_model
        return {
            "video": video, "language": language, "mode": "cloud", "model": used_model,
            "device": "cloud", "diarized": diarized, "speakers": speakers, "warnings": warnings,
            "cloud": {"provider": "assemblyai", "audio_seconds": seconds,
                      "estimated_cost_usd": estimate_cost(used_model, seconds, opts.diarize),
                      "deleted_at_provider": deleted},
            "segments": segments,
        }
    finally:
        if transcript_id and settings.cloud_delete_after:  # the job failed after submitting
            try:
                _api(settings, "DELETE", f"/v2/transcript/{transcript_id}")
            except PipelineError as exc:
                log.warning("could not delete the transcript at the provider: %s", exc)
        shutil.rmtree(workdir, ignore_errors=True)
