import json, os, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
D = os.environ["FAKE_DIR"]; ST = {"qwen-code": "loaded"}
def log(x): open(os.path.join(D, "calls.log"), "a").write(x + "\n")
def setfree(mb): open(os.path.join(D, "free_mb"), "w").write(str(mb))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        st = {"value": ST["qwen-code"]}
        if ST["qwen-code"] == "failed": st = {"value": "unloaded", "failed": True, "exit_code": 1}
        self._send({"data": [{"id": "qwen-code", "status": st}, {"id": "other", "status": {"value": "unloaded"}}]})
    def do_POST(self):
        m = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["model"]
        log(f"LLAMA {self.path} {m}")
        if self.path == "/models/unload": ST[m] = "unloaded"; setfree(23500)
        else:
            ST[m] = "loading"
            def fin():
                time.sleep(3)
                if os.path.exists(os.path.join(D, "load_fails")): ST[m] = "failed"
                else: ST[m] = "loaded"; setfree(300); log(f"LLAMA loaded {m}")
            threading.Thread(target=fin, daemon=True).start()
        self._send({"success": True})
setfree(300); HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
