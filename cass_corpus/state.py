# cass_corpus/state.py
# 喂料水位线:持久化复合游标 (last_synced_ts, last_synced_id),让导出做严格 keyset 增量。
# 复合游标(非单 ts)根治"同毫秒 ts ≥cap 条会话"时水位线 wedge(codex PR#27 P0)。
# 文件不存在 → None(真首跑);文件存在但坏(非法 JSON / 缺 last_synced_ts / 类型错)→ raise
# (fail loud,别静默把坏文件当首跑重播种、跳过 backlog,codex PR#27 P1-B)。
import json
import os


def default_state_path():
    return os.environ.get(
        "CASS_FEED_STATE",
        os.path.expanduser("~/.local/state/cass-corpus-feed/watermark.json"),
    )


def load_cursor(state_path):
    """返回 (ts, id) 复合游标。文件不存在 → None(真首跑)。
    文件存在但坏(非法 JSON / 缺 last_synced_ts / 类型错)→ 异常冒泡(fail loud)。"""
    try:
        f = open(state_path, encoding="utf-8")
    except FileNotFoundError:
        return None
    with f:
        data = json.load(f)                                    # 坏 JSON → JSONDecodeError 冒泡
    return (int(data["last_synced_ts"]), int(data.get("last_synced_id", 0)))  # 缺 ts / 类型错 → 冒泡


def save_cursor(state_path, ts, cid):
    d = os.path.dirname(state_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{state_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_synced_ts": int(ts), "last_synced_id": int(cid)}, f)
    os.replace(tmp, state_path)
