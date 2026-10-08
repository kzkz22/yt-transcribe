import os, shutil
class YoutubeDL:
    def __init__(self, opts): self.opts = opts
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=False):
        if "private" in url: raise RuntimeError("Private video")
        if download: shutil.copy(os.environ["FAKE_MEDIA"], self.opts["outtmpl"].replace("%(ext)s", "webm"))
        vid = url.rsplit("/", 1)[-1]
        return {"extractor_key": "Youtube", "id": vid, "title": "Teszt " + vid, "uploader": "Csatorna",
                "duration": 840, "webpage_url": url}
