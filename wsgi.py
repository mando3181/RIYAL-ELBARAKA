"""محوّل WSGI لتشغيل النظام على PythonAnywhere (أو أي خادم WSGI مثل gunicorn).

يعيد استخدام نفس منطق server.py دون تعديل: كل طلب يُمرَّر إلى Handler عبر مقابس وهمية.
"""
import io
import os
import sys
from email.message import Message

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QUIET", "1")
import server  # noqa: E402

server.os.makedirs(server.DATA_DIR, exist_ok=True)
server.init_db(seed_sample=os.environ.get("SEED_SAMPLE") == "1")


class _Req(server.Handler):
    def __init__(self, environ):  # لا نستدعي __init__ الأصلي (يعتمد على socket)
        n = int(environ.get("CONTENT_LENGTH") or 0)
        self.rfile = io.BytesIO(environ["wsgi.input"].read(n) if n else b"")
        self.wfile = io.BytesIO()
        self.headers = Message()
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                self.headers[k[5:].replace("_", "-").title()] = v
        if environ.get("CONTENT_TYPE"):
            self.headers["Content-Type"] = environ["CONTENT_TYPE"]
        self.headers["Content-Length"] = str(n)
        self.client_address = (environ.get("HTTP_X_FORWARDED_FOR", environ.get("REMOTE_ADDR", "0")).split(",")[0].strip(), 0)
        self.path = environ.get("PATH_INFO", "/") + ("?" + environ["QUERY_STRING"] if environ.get("QUERY_STRING") else "")
        self.request_version = "HTTP/1.0"
        self.requestline = ""
        self.command = environ["REQUEST_METHOD"]


def application(environ, start_response):
    req = _Req(environ)
    req.route(environ["REQUEST_METHOD"])
    raw = req.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ", 1)[1]
    headers = []
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        if k.lower() not in ("server", "date"):
            headers.append((k, v.strip()))
    start_response(status, headers)
    return [body]
