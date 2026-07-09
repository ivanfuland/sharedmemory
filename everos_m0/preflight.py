"""M0 preflight: LiteLLM finite-budget key + Infinity reachability.

任一不过 → 不许喂 EverOS。理由见 spec §10：EverOS 无内建预算闸（其 openai_provider
只是塞 key 发 chat），唯一的闸在 LiteLLM key 侧，而 LiteLLM 默认 max_budget=null。

⚠️ 两个 base 不是一个值（对 plan 的偏离，实测）：
  - admin_base: LiteLLM 管理面，`/key/info` 挂在 **root** —— `/v1/key/info` 返回 404
  - EverOS 的 `[llm] base_url`: OpenAI 兼容口，**必须带 `/v1`**
admin_key 只在本地跑这个模块时用，绝不注入 EverOS 进程。
"""

from __future__ import annotations

import httpx

EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def evaluate_budget_info(info: dict) -> tuple[bool, str]:
    """对 LiteLLM /key/info 响应做纯决策（不发请求，可测）。"""
    d = info.get("info", info)
    mb = d.get("max_budget")
    if mb is None:
        return False, "max_budget is null (LiteLLM default = no limit); use a finite virtual key"
    spend = float(d.get("spend") or 0.0)
    if spend >= float(mb):
        return False, f"spend {spend} >= max_budget {mb} (no headroom)"
    return True, f"finite max_budget {mb}, spend {spend}"


def evaluate_infinity_probe(embed_ok: bool, rerank_ok: bool) -> tuple[bool, str]:
    if not embed_ok:
        return False, "infinity embedding probe failed"
    if not rerank_ok:
        return False, "infinity rerank probe failed"
    return True, "infinity embed+rerank ok"


def check_litellm_budget(admin_base: str, admin_key: str, target_key: str) -> tuple[bool, str]:
    """用 admin key 查目标虚拟 key 的预算。虚拟 key 自查会被 403 拒（实测）。"""
    r = httpx.get(
        f"{admin_base.rstrip('/')}/key/info",
        params={"key": target_key},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return evaluate_budget_info(r.json())


def check_infinity(base: str) -> tuple[bool, str]:
    b = base.rstrip("/")
    try:
        e = httpx.post(f"{b}/embeddings", json={"model": EMBED_MODEL, "input": ["ping"]}, timeout=15)
        embed_ok = e.status_code == 200
    except Exception:
        embed_ok = False
    try:
        rr = httpx.post(
            f"{b}/rerank",
            json={"model": RERANK_MODEL, "query": "q", "documents": ["d"]},
            timeout=15,
        )
        rerank_ok = rr.status_code == 200
    except Exception:
        rerank_ok = False
    return evaluate_infinity_probe(embed_ok, rerank_ok)


def preflight(admin_base: str, admin_key: str, target_key: str, infinity_base: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    b_ok, b = check_litellm_budget(admin_base, admin_key, target_key)
    reasons.append(b)
    i_ok, i = check_infinity(infinity_base)
    reasons.append(i)
    return (b_ok and i_ok), reasons


if __name__ == "__main__":
    import os
    import sys

    ok, why = preflight(
        os.environ["LITELLM_ADMIN_BASE"],
        os.environ["LITELLM_ADMIN_KEY"],
        os.environ["EVEROS_M0_KEY"],
        os.environ["INFINITY_BASE"],
    )
    print("PREFLIGHT", "PASS" if ok else "FAIL")
    for r in why:
        print("  -", r)
    sys.exit(0 if ok else 1)
