#!/usr/bin/env python3
"""rerank 证据兜底:透明日志转发代理 127.0.0.1:$RERANK_PROXY_PORT -> $INFINITY_BASE。stdlib only。
仅当 journalctl 拿不到 Infinity access 证据时启用:监听本地端口,收到请求打一行
`ts path` 到 stderr 后原样转发上游。启用时 start 用
EVEROS_RERANK__BASE_URL=http://127.0.0.1:$RERANK_PROXY_PORT。
拓扑一律从环境变量取,不硬编码(PUBLIC 仓铁律)。
"""
import http.server, json, os, sys, time, urllib.request

UP = os.environ["INFINITY_BASE"]

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
    port = int(os.environ["RERANK_PROXY_PORT"])
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
