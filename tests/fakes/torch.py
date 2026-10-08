import os, types, _fk
def set_num_threads(n): _fk.log("threads", n)
def _free():
    try: return int(open(os.path.join(_fk.D, "free_mb")).read())
    except OSError: return 24000
cuda = types.SimpleNamespace(
    is_available=lambda: os.environ.get("FAKE_CUDA") == "1", empty_cache=lambda: None,
    mem_get_info=lambda: (_free() * 1024 * 1024, 24576 * 1024 * 1024))
