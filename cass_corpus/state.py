# cass_corpus/state.py
# 喂料水位线:持久化 last_synced_ts,让导出做增量而非"取最新 N"。
# 原子写(tmp + os.replace),坏文件/缺文件降级 None(首跑语义)。
import json
import os


def default_state_path():
    return os.environ.get(
        "CASS_FEED_STATE",
        os.path.expanduser("~/.local/state/cass-corpus-feed/watermark.json"),
    )


def load_watermark(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            v = json.load(f).get("last_synced_ts")
        return int(v) if v is not None else None
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_watermark(state_path, ts):
    d = os.path.dirname(state_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{state_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_synced_ts": int(ts)}, f)
    os.replace(tmp, state_path)
