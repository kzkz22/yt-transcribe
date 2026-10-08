import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
D = os.environ["FAKE_DIR"]; POLLS = {}
def log(x): open(os.path.join(D, "calls.log"), "a").write(x + "\n")
def flag(n): return os.path.exists(os.path.join(D, n))
W = lambda t, s, e, sp: {"text": t, "start": s, "end": e, "confidence": 0.9, "speaker": sp}
UTT = [
 {"speaker": "B", "start": 500, "end": 4000, "text": "Jó napot kívánok. Üdvözlöm a műsorban!", "words": [W("Jó",500,700,"B"),W("napot",700,1100,"B"),W("kívánok.",1100,1700,"B"),W("Üdvözlöm",2000,2600,"B"),W("a",2600,2700,"B"),W("műsorban!",2700,4000,"B")]},
 {"speaker": "A", "start": 4200, "end": 6000, "text": "Köszönöm a meghívást.", "words": [W("Köszönöm",4200,4900,"A"),W("a",4900,5000,"A"),W("meghívást.",5000,6000,"A")]},
 {"speaker": "B", "start": 6500, "end": 9000, "text": "Kezdjük az elején", "words": [W("Kezdjük",6500,7200,"B"),W("az",7200,7400,"B"),W("elején",7400,9000,"B")]},
]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _auth(self):
        if self.headers.get("authorization") != "key-ok": self._send({"error": "Authentication error, API token missing/invalid"}, 401); return False
        return True
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if not self._auth(): return
        if self.path == "/v2/upload":
            log(f"AAI upload bytes={len(body)} magic={body[:4].decode('latin1')} ctype={self.headers.get('content-type')}")
            return self._send({"upload_url": "https://cdn.fake/upload/abc"})
        req = json.loads(body); log("AAI transcript " + json.dumps(req, sort_keys=True))
        POLLS["t1"] = {"n": 0, "req": req}; self._send({"id": "t1", "status": "queued"})
    def do_GET(self):
        if not self._auth(): return
        st = POLLS["t1"]; st["n"] += 1
        if st["n"] < 2: return self._send({"id": "t1", "status": "processing"})
        if flag("aai_error"): return self._send({"id": "t1", "status": "error", "error": "Audio file is corrupt"})
        model = st["req"]["speech_models"][0]; lang = st["req"].get("language_code") or "hu"
        if flag("aai_no_speakers"):
            words = [dict(w, speaker=None) for u in UTT for w in u["words"]]
            return self._send({"id": "t1", "status": "completed", "utterances": None, "words": words, "language_code": lang, "audio_duration": 754, "speech_model_used": model})
        self._send({"id": "t1", "status": "completed", "utterances": UTT, "words": [], "language_code": "en_us" if lang == "en" else lang, "audio_duration": 754, "speech_model_used": model})
    def do_DELETE(self):
        if not self._auth(): return
        log("AAI delete " + self.path); self._send({"id": "t1", "status": "completed"})
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
