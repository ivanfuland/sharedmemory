#!/usr/bin/env python3
"""共享 SessionStart digest builder（hub 本地 gbrain CLI 读路径）。
fail-soft：任何异常 → 注空 + 状态行，绝不抛给 hook。

接口：
  build_digest(query_text, *, threshold=None, max_tokens=1500, gbrain_home=None) -> dict
    {"context": str, "status": str, "injected": bool, "hits": int}

  build_digest_from_raw(raw, *, threshold=None, max_tokens=1500) -> dict
    同上，但接受已有 gbrain query 原始输出（供测试/单元复用）。
"""
import json
import os
import re
import subprocess

DEFAULT_THRESHOLD = 0.75  # 宁高勿低（漏注优于污染）；Task4 标定可覆盖
DEFAULT_MAX_TOKENS = 1500  # §2.8 硬上限初值

# 解析 `gbrain query --no-expand` 输出行：[score] slug (可选 stale) -- head
_LINE = re.compile(r"^\[([0-9.]+)\]\s+(\S+)(?:\s+\(stale\))?\s+--\s*(.*)$")
# 检测 stale 标记行（独立 regex 以便 _is_stale 快速判断）
_STALE = re.compile(r"^\[[0-9.]+\]\s+\S+\s+\(stale\)")


def _load_threshold() -> float:
    """阈值解析优先级：GBRAIN_DIGEST_THRESHOLD 环境变量 > config/m2-thresholds.json > DEFAULT_THRESHOLD。
    GBRAIN_DIGEST_THRESHOLD 用于调优/测试（如一致性测试置 0.0 确保种子页必中）；生产环境不设。"""
    env_val = os.environ.get("GBRAIN_DIGEST_THRESHOLD")
    if env_val is not None:
        try:
            return float(env_val)
        except ValueError:
            pass  # 畸形值跌入下一层
    try:
        here = os.path.dirname(__file__)
        cfg = os.path.join(here, "..", "config", "m2-thresholds.json")
        return float(json.load(open(cfg))["query_threshold"])
    except Exception:
        return DEFAULT_THRESHOLD


def parse_query(raw: str) -> list:
    """解析 `gbrain query --no-expand` 输出 → [(score, slug, head), ...]。"""
    out = []
    for ln in raw.splitlines():
        m = _LINE.match(ln.rstrip())
        if m:
            out.append((float(m.group(1)), m.group(2), m.group(3).strip()))
    return out


def _is_stale(raw_line: str) -> bool:
    return bool(_STALE.match(raw_line.strip()))


def _run_query(query_text: str, gbrain_home=None) -> str:
    """执行 gbrain query <text> --no-expand，返回 stdout。失败抛 RuntimeError。"""
    env = {**os.environ}
    if gbrain_home:
        env["GBRAIN_HOME"] = gbrain_home
    env["PATH"] = os.path.expanduser("~/.bun/bin") + ":" + env.get("PATH", "")
    r = subprocess.run(
        ["gbrain", "query", query_text, "--no-expand"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"gbrain query rc={r.returncode}: {r.stderr.strip()[:200]}"
        )
    return r.stdout


def build_digest_from_raw(
    raw: str,
    *,
    threshold=None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """从 gbrain query 原始输出构建 digest（阈值过滤 + stale 降级 + 截断）。"""
    threshold = threshold if threshold is not None else _load_threshold()

    # 先收集 stale slug 集合（遍历原始行）
    stale_slugs: set = set()
    for ln in raw.splitlines():
        if _is_stale(ln):
            parsed = parse_query(ln)
            if parsed:
                stale_slugs.add(parsed[0][1])

    # 过滤出分值达标的命中
    hits = [h for h in parse_query(raw) if h[0] >= threshold]
    if not hits:
        return {
            "context": "",
            "status": "记忆层：无相关结论（阈值未过）",
            "injected": False,
            "hits": 0,
        }

    lines = []
    for score, slug, head in hits:
        if slug in stale_slugs:
            lines.append(f"- [[{slug}]] (stale：有新证据待整编) {head}")
        else:
            lines.append(f"- [[{slug}]] {head}")

    ctx = "## 记忆层相关结论\n" + "\n".join(lines)
    # 截断：中文粗算 1 token ≈ 4 bytes，按字符数上限
    if len(ctx) > max_tokens * 4:
        ctx = ctx[: max_tokens * 4].rsplit("\n", 1)[0]

    return {
        "context": ctx,
        "status": f"记忆层：注入 {len(hits)} 条",
        "injected": True,
        "hits": len(hits),
    }


def build_digest(
    query_text: str,
    *,
    threshold=None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    gbrain_home=None,
) -> dict:
    """完整 digest 构建：调 gbrain CLI → 解析 → 过滤 → 构建。
    任何异常 fail-soft → 返回注空 + 状态行，绝不抛。"""
    try:
        raw = _run_query(query_text, gbrain_home=gbrain_home)
    except Exception as e:
        return {
            "context": "",
            "status": f"记忆层：不可用（{type(e).__name__}）",
            "injected": False,
            "hits": 0,
        }
    return build_digest_from_raw(raw, threshold=threshold, max_tokens=max_tokens)


if __name__ == "__main__":  # CLI：传 query，输出 JSON
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GBRAIN_QUERY", "")
    print(json.dumps(build_digest(q), ensure_ascii=False))
