import os, time, _fk
import numpy as np
def load_audio(p): return [0.0]
class _M:
    def __init__(s, dev): s.dev = dev
    def transcribe(s, audio, batch_size=None, language=None):
        _fk.log("transcribe", s.dev, batch_size); time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
        return {"language": language or "hu", "segments": [{"start": 0, "end": 30, "text": "raw"}]}
def load_model(arch, device, compute_type=None, language=None, threads=4):
    _fk.log("load_model", arch, device, compute_type); return _M(device)
def load_align_model(language_code, device): return object(), {}
def align(segs, m, meta, audio, device, return_char_alignments=False):
    return {"segments": [{"start": np.float64(0.5), "end": np.float32(4.0), "text": "Jó napot.", "words": [{"word": "Jó", "start": np.float64(0.5)}]},
                         {"start": 4.2, "end": 6.0, "text": "Köszönöm."}]}
def assign_word_speakers(df, result, fill_nearest=False):
    for s, k in zip(result["segments"], ["SPEAKER_01", "SPEAKER_00"]): s["speaker"] = k
    return result
