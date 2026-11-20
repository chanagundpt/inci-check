"""ทดสอบหน้าจอในเบราว์เซอร์ (ไม่ต้องเปิดหน้าต่างโปรแกรม): python tools/devserver.py 8773
เรียก Api จริงผ่าน /api/<ชื่อฟังก์ชัน>"""
import json, os, sys, threading, traceback
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import main as appmain  # noqa

API = appmain.Api()
PICK = {"label": None}
SHIM = """<script>
window.pywebview={api:new Proxy({},{get:(_,fn)=>(...args)=>fetch('/api/'+fn,{method:'POST',body:JSON.stringify(args)}).then(r=>r.json())})};
setTimeout(()=>window.dispatchEvent(new Event('pywebviewready')),50);
</script>"""


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(ROOT, "ui"), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8").read()
            html = html.replace("<script>", SHIM + "<script>", 1)
            b = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        super().do_GET()

    def do_POST(self):
        fn = self.path.split("/api/")[-1]
        args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"[]")
        try:
            if fn == "pick_file":
                res = os.environ.get("PICK_" + str(len(args)), None)
                res = PICK.get("next")
            else:
                res = getattr(API, fn)(*args)
        except Exception as e:
            traceback.print_exc()
            res = {"ok": False, "error": repr(e)}
        b = json.dumps(res, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8773
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
