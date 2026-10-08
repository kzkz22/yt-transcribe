import os, _fk
class DiarizationPipeline:
    def __init__(self, model_name=None, token=None, device="cpu"):
        self.device = device; _fk.log("diar_init", device)
    def __call__(self, audio, num_speakers=None, min_speakers=None, max_speakers=None):
        if _fk.once("crash_once"): os.kill(os.getpid(), 9)
        if self.device == "cuda" and _fk.once("gpu_fail_once"): raise RuntimeError("CUDA out of memory")
        _fk.log("diarize", self.device, num_speakers); return "DF"
