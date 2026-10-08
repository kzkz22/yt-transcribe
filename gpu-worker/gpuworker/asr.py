"""Whisper -> forced alignment -> speaker diarization, for one audio file.

Runs inside the short-lived child process of a job (see job.py), so every byte
of RAM and VRAM is returned when the job ends. Returns raw segments with the
diarizer's speaker ids; naming the speakers and writing the files is the API's job.
"""
from __future__ import annotations

import gc
import hashlib
import json
import logging
from pathlib import Path
from typing import Callable

from .settings import Settings

log = logging.getLogger("gpu-worker")


class JobError(Exception):
    """Error whose message is safe and useful to show to the caller."""


def _free() -> None:
    """Collect garbage and hand cached GPU memory back (callers `del` their models first)."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def json_default(obj):
    """numpy scalars (timestamps from the aligner) are not JSON types."""
    return obj.item() if hasattr(obj, "item") else str(obj)


def checkpoint_path(settings: Settings, audio_sha256: str, model: str, language: str | None) -> Path:
    """Where the transcription (before speaker separation) of this exact audio is kept.

    Whisper is the slow part and does not depend on the speaker options, so a job
    that fails later, or is repeated with another speaker count, reuses it.
    """
    key = hashlib.sha1(json.dumps([audio_sha256, model, language]).encode()).hexdigest()[:16]
    return Path(settings.data_dir) / "asr" / f"{key}.json"


def choose_device(settings: Settings, warnings: list[str]) -> str:
    if settings.device == "cpu":
        return "cpu"
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if settings.device == "cuda":
        warnings.append("DEVICE=cuda but no GPU is visible in the container "
                        "(start it with --gpus all); ran on CPU.")
    return "cpu"


def run(audio_path: str, opts: dict, settings: Settings, device: str, checkpoint: Path,
        stage: Callable[[str], None] = lambda s: None) -> dict:
    """Transcribe, align and (optionally) diarize on `device`; returns the raw result."""
    model_name, language = opts["model"], opts.get("language")
    diarize = bool(opts.get("diarize"))
    warnings: list[str] = []

    import torch
    import whisperx

    torch.set_num_threads(settings.threads())
    asr = None
    if checkpoint.exists():
        try:
            asr = json.loads(checkpoint.read_text(encoding="utf-8"))
            log.info("reusing saved transcription %s", checkpoint.name)
        except (OSError, ValueError):
            asr = None

    try:
        audio = None
        if asr is None or diarize:
            stage("load-audio")
            audio = whisperx.load_audio(audio_path)

        if asr is None:
            stage("transcribe")
            model = whisperx.load_model(model_name, device,
                                        compute_type=settings.compute_type_for(device),
                                        language=language, threads=settings.threads())
            result = model.transcribe(audio, batch_size=settings.batch_size_for(device),
                                      language=language)
            detected = result.get("language") or language or "en"
            del model
            _free()
            if not result.get("segments"):
                raise JobError("No speech was detected in the audio.")

            stage("align")
            align_warnings: list[str] = []
            try:
                align_model, meta = whisperx.load_align_model(language_code=detected, device=device)
                aligned = whisperx.align(result["segments"], align_model, meta, audio, device,
                                         return_char_alignments=False)
                del align_model
                _free()
                if aligned.get("segments"):
                    result = aligned
            except Exception as exc:  # alignment is an accuracy bonus, not a requirement
                log.warning("alignment failed, continuing with Whisper timestamps: %s", exc)
                align_warnings.append("Word alignment failed; timestamps and speaker boundaries "
                                      "are coarser than usual.")
                _free()
            asr = {"language": detected, "segments": result["segments"], "warnings": align_warnings}
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            tmp = checkpoint.with_suffix(".tmp")
            tmp.write_text(json.dumps(asr, ensure_ascii=False, default=json_default), encoding="utf-8")
            tmp.replace(checkpoint)

        warnings += asr.get("warnings", [])
        result = {"segments": asr["segments"]}
        if diarize:
            stage("diarize")
            from whisperx.diarize import DiarizationPipeline

            diarizer = DiarizationPipeline(model_name=settings.diarize_model or None,
                                           token=settings.hf_token, device=device)
            diarize_df = diarizer(audio, num_speakers=opts.get("num_speakers"),
                                  min_speakers=opts.get("min_speakers"),
                                  max_speakers=opts.get("max_speakers"))
            result = whisperx.assign_word_speakers(diarize_df, result, fill_nearest=True)
            del diarizer
            _free()
        return {"language": asr["language"], "segments": result["segments"], "device": device,
                "diarized": diarize, "warnings": warnings}
    finally:
        _free()
