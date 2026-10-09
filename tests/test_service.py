"""End-to-end tests: the real service and client, with fakes for models and remote services."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def _model_calls(stack) -> list[str]:
    return [c for c in stack.calls() if c.startswith(("load_model", "transcribe", "diar", "AAI"))]


def test_local_job_runs_on_the_gpu_worker(stack):
    out = stack.client("run", "https://youtu.be/v1", "--language", "hu", "--speakers", "2")
    assert out["status"] == "done" and out["mode"] == "local" and out["device"] == "cuda"
    assert [s["label"] for s in out["speakers"]] == ["Szereplő 1", "Szereplő 2"]
    calls = _model_calls(stack)
    assert calls[0] == "load_model large-v3 cuda float16"
    assert "transcribe cuda 8" in calls and "diarize cuda 2" in calls
    assert not any(c.startswith("AAI") for c in calls), "local mode must not touch the cloud"
    srt = open(out["files"]["srt"], encoding="utf-8").read()
    assert "[Szereplő 1] Jó napot." in srt and "[Szereplő 2] Köszönöm." in srt


def test_local_model_choice_and_side_by_side_results(stack):
    a = stack.client("run", "https://youtu.be/v2", "--language", "hu")
    b = stack.client("run", "https://youtu.be/v2", "--language", "hu", "--model", "large-v3-turbo")
    assert (a["model"], b["model"]) == ("large-v3", "large-v3-turbo")
    assert a["files"]["txt"] != b["files"]["txt"]
    assert any(c.startswith("load_model large-v3-turbo") for c in stack.calls())


def test_worker_crash_is_isolated_and_retry_resumes_from_checkpoint(stack):
    stack.flag("crash_once")
    first = stack.client("run", "https://youtu.be/v3")
    assert first["status"] == "error" and "killed" in first["error"]
    assert stack.client("health")["ok"]
    stack.clear_calls()
    second = stack.client("run", "https://youtu.be/v3")
    assert second["status"] == "done"
    assert not any(c.startswith("transcribe") for c in stack.calls()), "Whisper should not rerun"
    assert any(c.startswith("diarize") for c in stack.calls())


def test_gpu_failure_finishes_on_cpu_with_a_warning(stack):
    stack.flag("gpu_fail_once")
    out = stack.client("run", "https://youtu.be/v4", "--language", "hu")
    assert out["status"] == "done" and out["device"] == "cpu"
    assert any("finished on CPU" in w for w in out["warnings"])


def test_gpu_server_off_is_an_error_without_cloud_fallback(stack):
    stack.stop_worker()
    assert stack.client("health")["ok"], "/health must not depend on (or wake) the worker"
    out = stack.client("run", "https://youtu.be/v5", "--language", "hu")
    assert out["status"] == "error" and "GPU worker could not be reached" in out["error"]
    assert "nothing was switched to cloud mode" in out["error"]
    assert not any(c.startswith("AAI") for c in stack.calls())


def test_worker_refusal_is_reported(make_stack):
    s = make_stack(worker__HF_TOKEN="")
    out = s.client("run", "https://youtu.be/v6")
    assert out["status"] == "error" and "Hugging Face token" in out["error"]
    assert s.client("run", "https://youtu.be/v6", "--no-diarize")["status"] == "done"


def test_cloud_hungarian_flow(stack):
    out = stack.client("run", "https://youtu.be/c1", "--mode", "cloud", "--language", "hu", "--speakers", "2")
    assert out["status"] == "done" and out["mode"] == "cloud" and out["model"] == "universal-2"
    assert out["cloud"]["deleted_at_provider"] is True
    assert out["cloud"]["estimated_cost_usd"] == round(0.17 * 754 / 3600, 4)
    calls = stack.calls()
    assert not any(c.startswith(("load_model", "transcribe")) for c in calls), \
        "cloud mode must not use the GPU worker"
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
    assert not any(c.startswith(("load_model", "transcribe")) for c in stack.calls())


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


def _worker_busy(stack) -> bool:
    url = f"http://127.0.0.1:{stack.worker_port}/health"
    return json.loads(urllib.request.urlopen(url, timeout=5).read())["busy"]


def test_job_deadline_stops_the_worker_and_frees_the_gpu(make_stack, media):
    s = make_stack(WORKER_TIMEOUT_MIN="0.1")  # 6 s, or 4x the 5 s audio = 20 s
    s.flag("slow_once")
    started = time.time()
    out = s.client("run", str(media["video"]), "--language", "hu")
    assert out["status"] == "error" and "did not finish within" in out["error"]
    assert time.time() - started < 35
    deadline = time.time() + 10
    while _worker_busy(s) and time.time() < deadline:
        time.sleep(0.5)
    assert not _worker_busy(s), "the worker must release the GPU when the caller goes away"
    assert s.client("run", str(media["video"]), "--language", "hu")["status"] == "done"


def test_worker_reads_a_large_body_before_refusing_it(make_stack):
    s = make_stack(worker__HF_TOKEN="")
    req = urllib.request.Request(f"http://127.0.0.1:{s.worker_port}/transcribe?model=large-v3",
                                 data=b"\0" * (30 * 1024 * 1024), method="POST",
                                 headers={"content-type": "audio/flac"})
    try:
        urllib.request.urlopen(req, timeout=60)
        raise AssertionError("expected a refusal")
    except urllib.error.HTTPError as exc:
        assert exc.code == 422 and "Hugging Face token" in exc.read().decode()


def _api_get(stack, path: str):
    with urllib.request.urlopen(stack.url + path, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if "json" in resp.headers.get("Content-Type", "") else body


def test_captions_mode_uses_the_uploaders_own_track(stack):
    out = stack.client("run", "https://youtu.be/capman1", "--mode", "captions")
    assert out["status"] == "done" and out["mode"] == "captions" and out["model"] == "youtube-manual"
    assert out["language"] == "en" and out["diarized"] is False and out["speakers"] == []
    assert out["captions"] == {"kind": "manual", "track": "en-US"}
    calls = stack.calls()
    assert [c for c in calls if c.startswith("captions fetch")] == ["captions fetch captions_manual_en"], \
        "exactly one track, the own one in the video's language"
    assert not any(c.startswith(("load_model", "transcribe", "AAI")) for c in calls)
    srt = open(out["files"]["srt"], encoding="utf-8").read()
    assert "Look at these two identical PETG prints." in srt and "[Music]" not in srt
    assert "The filament dryer they were fed from." in srt


def test_captions_mode_builds_sentences_from_automatic_words(stack):
    out = stack.client("run", "https://youtu.be/capauto1", "--mode", "captions")
    assert out["status"] == "done" and out["model"] == "youtube-auto" and out["language"] == "hu"
    assert out["captions"] == {"kind": "auto", "track": "hu-orig"}
    assert any("automatic captions" in w for w in out["warnings"])
    srt = open(out["files"]["srt"], encoding="utf-8").read()
    assert "Sziasztok, Laci vagyok, a T2-t nézitek, és a mai videóban hoztam nektek tíz kütyüt." in srt
    assert "00:00:09,520 --> " in srt and "Ez a kedvencem." in srt


def test_captions_mode_without_a_track_fails_without_fallback(stack):
    out = stack.client("run", "https://youtu.be/nocap1", "--mode", "captions")
    assert out["status"] == "error" and "no captions in its language ('de')" in out["error"]
    assert "mode=local" in out["error"]
    assert not any(c.startswith(("load_model", "transcribe", "AAI", "captions fetch")) for c in stack.calls())


def test_captions_mode_reports_youtube_refusals(stack):
    stack.flag("captions_429")
    out = stack.client("run", "https://youtu.be/capman2", "--mode", "captions")
    assert out["status"] == "error" and "HTTP Error 429" in out["error"]


def test_captions_requests_ignore_speaker_options(stack):
    a = stack.client("run", "https://youtu.be/capman4", "--mode", "captions")
    b = stack.client("run", "https://youtu.be/capman4", "--mode", "captions", "--speakers", "3")
    assert a["status"] == b["status"] == "done" and b["cached"] is True


def test_captions_mode_needs_a_url(stack, media):
    out = stack.client("run", str(media["video"]), "--mode", "captions")
    assert out["status"] == "error" and "captions mode needs a video URL" in out["error"]


def test_finished_results_can_be_listed_and_fetched(stack):
    first = stack.client("run", "https://youtu.be/capman3", "--mode", "captions")
    time.sleep(0.05)
    second = stack.client("run", "https://youtu.be/v7", "--language", "hu", "--no-diarize")
    assert first["status"] == second["status"] == "done"
    listed = _api_get(stack, "/results?since=0")["results"]
    assert [r["mode"] for r in listed] == ["captions", "local"]
    assert listed[0]["video"]["id"] == "youtube_capman3" and listed[0]["captions"]["kind"] == "manual"
    newer = _api_get(stack, f"/results?since={listed[0]['finished_at']}&after={listed[0]['result_id']}")
    assert [r["result_id"] for r in newer["results"]] == [listed[1]["result_id"]]
    page = _api_get(stack, "/results?since=0&limit=1")
    assert page["more"] is True and [r["result_id"] for r in page["results"]] == [listed[0]["result_id"]]
    full = _api_get(stack, f"/results/{listed[1]['result_id']}")
    assert full["mode"] == "local" and len(full["segments"]) == listed[1]["segments"]
    assert "Jó napot." in _api_get(stack, f"/results/{listed[1]['result_id']}?format=txt")
    for bad in ("youtube_v7/..", "a.b/c", "youtube_v7/nope"):
        try:
            _api_get(stack, f"/results/{bad}")
            raise AssertionError(f"{bad} should be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
