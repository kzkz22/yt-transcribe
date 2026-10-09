import os, shutil, _fk

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def _tracks(names):
    return [{"ext": "vtt", "url": "fake://vtt"}] + [{"ext": "json3", "url": f"fake://{n}"} for n in names]


# Caption tracks as yt-dlp reports them, keyed by a word in the test URL (shapes copied from
# real videos: an English video with its own subtitles and auto-dubbed "-orig" tracks, and a
# Hungarian video with automatic captions plus machine translations).
CAPTIONS = {
    "capman": {"language": "en-US",
               "subtitles": {"nl-NL": _tracks(["x"]), "en-US": _tracks(["captions_manual_en"]),
                             "live_chat": [{"ext": "json", "url": "fake://chat"}]},
               "automatic_captions": {"it-orig": _tracks(["x"]), "en": _tracks(["x"]), "hu-en": _tracks(["x"])}},
    "capauto": {"language": "hu", "subtitles": {},
                "automatic_captions": {"hu-orig": _tracks(["captions_auto_hu"]), "hu": _tracks(["captions_auto_hu"]),
                                       "en": _tracks(["x"]), "en-hu": _tracks(["x"])}},
    "nocap": {"language": "de", "subtitles": {}, "automatic_captions": {"en": _tracks(["x"])}},
}


class _Resp:
    def __init__(self, data): self.data = data
    def read(self): return self.data


class YoutubeDL:
    def __init__(self, opts): self.opts = opts
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=False):
        if "private" in url: raise RuntimeError("Private video")
        if download: shutil.copy(os.environ["FAKE_MEDIA"], self.opts["outtmpl"].replace("%(ext)s", "webm"))
        vid = url.rsplit("/", 1)[-1]
        info = {"extractor_key": "Youtube", "id": vid, "title": "Teszt " + vid, "uploader": "Csatorna",
                "duration": 840, "webpage_url": url}
        for key, caps in CAPTIONS.items():
            if key in url:
                info.update(caps)
        return info
    def urlopen(self, url):
        name = url.split("://", 1)[1]
        _fk.log("captions fetch", name)
        if _fk.once("captions_429"): raise RuntimeError("HTTP Error 429: Too Many Requests")
        with open(os.path.join(FIXTURES, name + ".json3"), "rb") as fh:
            return _Resp(fh.read())
