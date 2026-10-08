"""End-to-end tests: the real service and client, with fakes for models and remote services."""
from __future__ import annotations

import json
import time


def _llama_and_model_calls(stack) -> list[str]:
    return [c for c in stack.calls() if c.startswith(("LLAMA", "transcribe", "diarize", "AAI"))]


def test_local_gpu_borrows_and_returns_the_llm(stack):
    out = stack.client("run", "https://youtu.be/v1", "--language", "hu", "--speakers", "2")
    assert out["status"] == "done" and out["mode"] == "local" and out["device"] == "cuda"
    assert [s["label"] for s in out["speakers"]] == ["Szereplő 1", "Szereplő 2"]
    calls = _llama_and_model_calls(stack)
    assert calls[0] == "LLAMA /models/unload qwen-code"
    assert "LLAMA /models/load qwen-code" in calls and calls[-1] == "LLAMA loaded qwen-code"
    assert any(c.startswith("transcribe cuda") for c in calls)


def test_local_model_choice_and_side_by_side_results(stack):
    a = stack.client("run", "https://youtu.be/v2", "--language", "hu")
    b = stack.client("run", "https://youtu.be/v2", "--language", "hu", "--model", "large-v3-turbo")
    assert (a["model"], b["model"]) == ("large-v3", "large-v3-turbo")
    assert a["files"]["txt"] != b["files"]["txt"]
    assert any(c.startswith("load_model large-v3-turbo") for c in stack.calls())


def test_crash_is_isolated_and_retry_resumes_from_checkpoint(stack):
    stack.flag("crash_once")
    first = stack.client("run", "https://youtu.be/v3")
    assert first["status"] == "error" and "killed" in first["error"]
    assert stack.client("health")["ok"]
    # the LLM must be loaded again even though the job died
    assert "LLAMA /models/load qwen-code" in stack.calls()
    stack.clear_calls()
    second = stack.client("run", "https://youtu.be/v3")
    assert second["status"] == "done"
    assert not any(c.startswith("transcribe") for c in stack.calls()), "Whisper should not rerun"


def test_cloud_hungarian_flow(stack):
    out = stack.client("run", "https://youtu.be/c1", "--mode", "cloud", "--language", "hu", "--speakers", "2")
    assert out["status"] == "done" and out["mode"] == "cloud" and out["model"] == "universal-2"
    assert out["cloud"]["deleted_at_provider"] is True
    assert out["cloud"]["estimated_cost_usd"] == round(0.17 * 754 / 3600, 4)
    calls = stack.calls()
    assert not any(c.startswith("LLAMA") for c in calls), "cloud mode must not touch the LLM"
    upload = next(c for c in calls if c.startswith("AAI upload"))
    assert "magic=fLaC" in upload
    body = json.loads(next(c for c in calls if c.startswith("AAI transcript"))[len("AAI transcript "):])
    assert body["speech_models"] == ["universal-2"] and body["language_code"] == "hu"
    assert body["language_detection"] is False and body["speakers_expected"] == 2
    assert any(c.startswith("AAI delete") for c in calls)
    srt = open(out["files"]["srt"], encoding="utf-8").read()
    assert "[Szereplő 1] Jó napot kívánok." in srt


def test_cloud_model_auto_picks_pro_for_english_and_refuses_pro_for_hungarian(stack):
    en = stack.client("run", "https://youtu.be/c2", "--mode", "cloud", "--language", "en")
    assert en["model"] == "universal-3-5-pro" and en["language"] == "en"
    hu = stack.client("run", "https://youtu.be/c2", "--mode", "cloud", "--language", "hu",
                      "--model", "universal-3.5-pro")
    assert hu["status"] == "error" and "does not support the language 'hu'" in hu["error"]


def test_cloud_budget_blocks_before_upload(stack):
    usage_file = stack.tmp / "data" / "cloud_usage.json"
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps({time.strftime("%Y-%m"): 35700}))
    out = stack.client("run", "https://youtu.be/c3", "--mode", "cloud")
    assert out["status"] == "error" and "monthly cloud budget" in out["error"]
    assert not any(c.startswith("AAI") for c in stack.calls())


def test_cloud_failure_does_not_fall_back_to_local(stack):
    stack.flag("aai_error")
    out = stack.client("run", "https://youtu.be/c4", "--mode", "cloud", "--language", "hu")
    assert out["status"] == "error" and "could not transcribe" in out["error"]
    assert not any(c.startswith(("load_model", "LLAMA")) for c in stack.calls())


def test_cloud_without_speaker_labels_warns(stack):
    stack.flag("aai_no_speakers")
    out = stack.client("run", "https://youtu.be/c5", "--mode", "cloud", "--language", "hu")
    assert out["status"] == "done" and out["diarized"] is False
    assert any("no speaker labels" in w for w in out["warnings"])


def test_local_file_upload_is_reused_across_modes(stack, media):
    a = stack.client("run", str(media["video"]), "--mode", "cloud", "--language", "hu")
    assert a["status"] == "done"
    assert a["title"] == "Interjú felvétel.mp4" and a["duration_s"] == 5
    uploads = list((stack.tmp / "data" / "uploads").iterdir())
    assert len(uploads) == 1
    stored = next(uploads[0].glob("media*"))
    first_write = stored.stat().st_mtime_ns
    b = stack.client("run", str(media["video"]), "--language", "hu", "--no-diarize")
    assert b["status"] == "done" and b["mode"] == "local"
    # the second run found the file on the server, so it was not sent again
    assert stored.stat().st_mtime_ns == first_write


def test_bad_inputs(stack, media):
    assert "no audio track" in stack.client("run", str(media["silent"]), "--mode", "cloud")["error"]
    assert "neither a URL nor an existing file" in stack.client("run", str(stack.tmp / "nope.mp4"))["error"]
    assert "unknown local model" in stack.client("run", "https://youtu.be/x", "--model", "universal-2")["error"]


def test_cloud_not_configured(make_stack):
    s = make_stack(ASSEMBLYAI_API_KEY="")
    out = s.client("run", "https://youtu.be/x", "--mode", "cloud")
    assert out["status"] == "error" and "ASSEMBLYAI_API_KEY is missing" in out["error"]
    assert s.client("health")["cloud"]["available"] is False
