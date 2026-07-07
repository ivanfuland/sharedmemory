# cass_mcp/runner.py
# NOTE: 语义检索依赖 CASS_DATA_DIR / CASS_INFINITY_URL 在进程 env 中（由 cass_mcp.config import 时
# setdefault 设好；server.py 已 import config，故 run_cass 调用时 env 已就绪）。
import json, os, subprocess, time

DEFAULT_MAX_BYTES = 262144   # 256KB(~64k token) 单次 MCP 工具返回上限

class CircuitBreaker:
    def __init__(self, threshold=5, cooldown_s=300):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.fails, self.open_until = 0, 0.0
    def allow(self, now): return now >= self.open_until
    def record(self, ok, now):
        if ok: self.fails = 0
        else:
            self.fails += 1
            if self.fails >= self.threshold: self.open_until = now + self.cooldown_s

def run_cass(subcmd, args, *, want_json=True, cass_bin=None, timeout_s=30, max_bytes=DEFAULT_MAX_BYTES, breaker=None, _now=None, oversize_is_failure=True):
    # max_bytes 256KB(~64k token)：单次工具返回上限。够 timeline 7d(实测204KB)/pack/sessions；
    # 更宽窗口超此回 result_too_large+narrow 提示（见下）。search 实测仅 7-10KB，远不触。
    now = _now if _now is not None else time.monotonic()
    if breaker and not breaker.allow(now):
        return {"error": "unavailable"}
    cass_bin = cass_bin or os.environ.get("CASS_BIN", os.path.expanduser("~/.local/bin/cass"))
    argv = [cass_bin, subcmd, *args] + (["--json"] if want_json else [])   # argv 直 exec 不过 shell；export 等文本命令不加 --json
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout_s)   # text=False 取 bytes 便于截断
    except subprocess.TimeoutExpired:
        if breaker: breaker.record(False, now)
        return {"error": "timeout"}
    if p.returncode != 0:
        if breaker: breaker.record(False, now)
        return {"error": "cass_exit", "code": p.returncode, "stderr": p.stderr.decode("utf-8", "replace")[:500]}
    # NOTE: 成功只在「干净解析/返回」后记 True；bad_json / result_too_large 也记 False（P1-2 fix）
    out = p.stdout
    if len(out) > max_bytes:
        if want_json:
            if breaker and oversize_is_failure: breaker.record(False, now)   # 仅当视为失败才记熔断
            return {"error": "result_too_large", "bytes": len(out),
                    "hint": "narrow query: lower limit or max_content_length"}
        # 文本(export)截断不算失败（有意 cap，仍返回可用文本）
        if breaker: breaker.record(True, now)
        return {"truncated": True, "text": out[:max_bytes].decode("utf-8", "ignore")}
    text = out.decode("utf-8", "replace")
    if not want_json:
        if breaker: breaker.record(True, now)
        return {"text": text}                            # export markdown 文本，不 json.loads
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if breaker: breaker.record(False, now)
        return {"error": "bad_json", "raw": text[:500]}
    if breaker: breaker.record(True, now)
    return parsed
