# cass_mcp/diversify.py
# cass_search 读侧会话多样化（best-effort）+ 返回体元数据修正。纯逻辑，无 fastmcp / 无 bearer 依赖，便于单测。
# 移植自 agentmemory src/state/hybrid-search.ts diversifyBySession。
from cass_mcp import contract

_MAX_PER_SESSION = 3
_LIMIT_MAX = 50


def overfetch_limit(limit):
    """clamp public limit 到 [1,50]，返回 (user_limit, overfetch)；overfetch 恒 ≥ user_limit。"""
    user_limit = max(1, min(int(limit), _LIMIT_MAX))
    return user_limit, min(user_limit * 3, 150)


def diversify_by_session(hits, limit, max_per_session=_MAX_PER_SESSION):
    """best-effort：分数序遍历，单 source_path 软上限；不足 limit 则回填剩余（回填可超软上限）。"""
    selected, counts = [], {}
    for h in hits:
        sp = h.get("source_path")
        if counts.get(sp, 0) >= max_per_session:
            continue
        selected.append(h)
        counts[sp] = counts.get(sp, 0) + 1
        if len(selected) >= limit:
            return selected
    if len(selected) < limit:                       # 回填分支
        chosen = {id(h) for h in selected}
        for h in hits:
            if len(selected) >= limit:
                break
            if id(h) not in chosen:
                selected.append(h)
    return selected


def apply_search_postprocess(r, user_limit):
    """就地多样化 r["hits"] + 重写 count/limit。错误/无 hits 直通。
    ⚠ 不动 hits_clamped/total_matches：hits_clamped 是 CASS 的 token-budget 截断语义。"""
    if not isinstance(r, dict) or not isinstance(r.get("hits"), list):
        return r
    raw = contract.extract_search_hits(r)           # 复用单一来源，不硬编码键路径
    div = diversify_by_session(raw, user_limit)
    r["hits"] = div
    r["count"] = len(div)
    r["limit"] = user_limit
    return r
