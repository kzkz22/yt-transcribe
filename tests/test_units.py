"""Pure-logic tests: no service process, no fakes needed."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app import captions, cloud, formatting, local, pipeline, usage  # noqa: E402


def test_relabel_by_first_appearance_and_inherit_missing():
    segs = formatting.clean_segments([
        {"start": 0, "end": 2, "text": " Szia ", "speaker": "SPEAKER_03"},
        {"start": 2, "end": 3, "text": "Igen.", "speaker": None},
        {"start": 3, "end": 5, "text": "Helló", "speaker": "SPEAKER_00"},
        {"start": 5, "end": 5, "text": "   ", "speaker": "SPEAKER_00"},
    ])
    speakers = formatting.relabel_speakers(segs, "Szereplő", "Ismeretlen")
    assert [s["speaker"] for s in segs] == ["Szereplő 1", "Szereplő 1", "Szereplő 2"]
    assert [s["label"] for s in speakers] == ["Szereplő 1", "Szereplő 2"]
    assert abs(sum(s["share"] for s in speakers) - 1) < 0.01


def test_clean_segments_repairs_missing_times():
    segs = formatting.clean_segments([{"start": 1, "end": 2, "text": "a"},
                                      {"start": float("nan"), "end": None, "text": "b"}])
    assert segs[1]["start"] == 2 and segs[1]["end"] == 2


def test_turns_merge_same_speaker_and_split_long_monologues():
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9, "text": "x" * 200, "speaker": "S"} for i in range(12)]
    turns = formatting.to_turns(segs)
    assert len(turns) == 3 and all(len(t["text"]) <= 900 for t in turns)  # 4 x 200 chars per turn


def test_segments_from_words_split_rules():
    words = [{"text": f"w{i}", "start": i * 1.0, "end": i * 1.0 + 0.9, "speaker": "A"} for i in range(40)]
    segs = formatting.segments_from_words(words)
    assert all(s["end"] - s["start"] <= 15.9 and len(s["text"]) <= 220 for s in segs)
    assert " ".join(s["text"] for s in segs) == " ".join(w["text"] for w in words)
    mixed = [{"text": "Igen.", "start": 0, "end": 1, "speaker": "A"},
             {"text": "Nem", "start": 1, "end": 2, "speaker": "B"},
             {"text": "hiszem.", "start": 2, "end": 3, "speaker": "B"}]
    assert [(s["speaker"], s["text"]) for s in formatting.segments_from_words(mixed)] == \
        [("A", "Igen."), ("B", "Nem hiszem.")]


def test_srt_and_txt_shapes():
    result = {"video": {"title": "T", "url": "u", "duration": 754}, "language": "hu",
              "mode": "cloud", "model": "universal-2", "diarized": True,
              "speakers": [{"label": "Szereplő 1", "share": 1.0, "seconds": 1}],
              "segments": [{"start": 3700.0, "end": 3700.0, "text": "Szia.", "speaker": "Szereplő 1"}]}
    assert "01:01:40,000 --> 01:01:40,300\n[Szereplő 1] Szia." in formatting.to_srt(result)
    txt = formatting.to_txt(result)
    assert "Duration: 00:12:34" in txt and "Transcribed by: universal-2 (cloud)" in txt
    assert "[01:01:40] Szereplő 1: Szia." in txt


@pytest.mark.parametrize("requested,language,expected", [
    ("auto", "hu", "universal-2"),
    (None, "en", "universal-3-5-pro"),
    ("auto", None, "universal-2"),
    ("auto", "en-GB", "universal-3-5-pro"),
    ("universal-3.5-pro", None, "universal-3-5-pro"),
    ("Universal-2", "hu", "universal-2"),
])
def test_cloud_model_resolution(requested, language, expected):
    assert cloud.resolve_model(requested, language) == expected


@pytest.mark.parametrize("requested,language", [("universal-3.5-pro", "hu"), ("whisper", "en")])
def test_cloud_model_resolution_rejects(requested, language):
    with pytest.raises(pipeline.PipelineError):
        cloud.resolve_model(requested, language)


def test_cost_estimate():
    assert cloud.estimate_cost("universal-3-5-pro", 3600, True) == 0.23
    assert cloud.estimate_cost("universal-2", 1800, False) == 0.075


def test_monthly_budget(tmp_path):
    d = str(tmp_path)
    assert usage.over_budget_message(d, 1, 3500) is None
    usage.add(d, 3000)
    assert usage.used_seconds(d) == 3000
    assert usage.over_budget_message(d, 1, 700)
    assert usage.over_budget_message(d, 0, 10 ** 6) is None  # 0 = no limit


def test_worker_query_leaves_out_unset_options():
    opts = pipeline.Options(url="u", model="large-v3-turbo", language="hu", diarize=False, num_speakers=3)
    assert local._query(opts) == "model=large-v3-turbo&language=hu&diarize=false&num_speakers=3"
    assert local._query(pipeline.Options(url="u")) == "model=large-v3&diarize=true"


_T = [{"ext": "json3", "url": "u"}]


@pytest.mark.parametrize("info,language,expected", [
    ({"language": "en-US", "subtitles": {"en-US": _T, "nl-NL": _T}, "automatic_captions": {"it-orig": _T}},
     None, ("manual", "en-US")),
    ({"language": "en-US", "subtitles": {"en": _T}}, None, ("manual", "en")),
    ({"language": "hu", "automatic_captions": {"hu-en": _T, "hu": _T, "hu-orig": _T}}, None, ("auto", "hu-orig")),
    ({"language": "hu", "automatic_captions": {"hu-en": _T, "hu": _T}}, None, ("auto", "hu")),
    ({"automatic_captions": {"de-orig": _T, "en": _T}}, None, ("auto", "de-orig")),
    ({"language": "en", "subtitles": {"hu": _T}, "automatic_captions": {"en-orig": _T}}, "hu", ("manual", "hu")),
    ({"language": "de", "automatic_captions": {"en": _T, "de-en": _T}}, None, None),
    ({"subtitles": {"live_chat": _T}}, None, None),
    # next to en-orig, plain "hu" is a machine translation: never use it
    ({"language": "en", "automatic_captions": {"en-orig": _T, "en": _T, "hu": _T}}, "hu", None),
    ({"language": "en", "automatic_captions": {"en-orig": _T, "en": _T, "hu": _T}}, None, ("auto", "en-orig")),
    # the script subtag must match
    ({"language": "zh-Hans", "subtitles": {"zh-Hant": _T}}, None, None),
    ({"language": "zh-Hans", "subtitles": {"zh-Hant": _T, "zh-Hans": _T}}, None, ("manual", "zh-Hans")),
    ({"language": "sr-Latn", "subtitles": {"sr": _T}}, None, None),
])
def test_caption_track_choice(info, language, expected):
    got = captions.choose_track(info, language)
    assert (got[0], got[1]) == expected if expected else got is None


def test_json3_words_get_end_times_and_annotations_are_dropped():
    data = {"events": [{"tStartMs": 1000, "dDurationMs": 3000, "segs": [
        {"utf8": "Szia"}, {"utf8": " [Zene]", "tOffsetMs": 500}, {"utf8": " világ.", "tOffsetMs": 900}]},
        {"tStartMs": 3000, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]}]}
    words = captions.words_from_json3(data, "auto")
    assert [w["text"] for w in words] == ["Szia", "világ."]
    assert words[0]["start"] == 1.0 and words[0]["end"] == 1.5 and words[1]["end"] <= 3.9
