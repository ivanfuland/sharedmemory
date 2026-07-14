#!/usr/bin/env python3
"""rerank 证据兜底:透明日志转发代理 127.0.0.1:7998 -> $INFINITY_BASE。stdlib only。
仅当 journalctl 拿不到 Infinity access 证据时启用:监听 127.0.0.1:7998,收到请求打一行
`ts path` 到 stderr 后原样转发 7997。启用时 start 用 EVEROS_RERANK__BASE_URL=http://127.0.0.1:7998。
"""
import http.server, json, os, sys, time, urllib.request

UP = os.environ.get("INFINITY_BASE", "http://127.0.0.1:7997")

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        print(json.dumps({"ts": time.time(), "path": self.path, "bytes": n}), file=sys.stderr, flush=True)
        req = urllib.request.Request(UP + self.path, data=body,
                                     headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        self.send_response(r.status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", 7998), H).serve_forever()
