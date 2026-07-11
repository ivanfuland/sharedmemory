"""合成 CASS data_dir 工厂 + 攻击构造①–⑦。

给 `infra/backup/backup-cass.sh`（Task 9 起）的五腿门测试提供地基：
  - `make_session_jsonl` / `build_data_dir`：隔离 HOME 下用真 cass 自建一份完整的迷你
    data_dir（真 schema、真 raw-mirror、真水位），全部内容 nonsense。
  - `attack1`..`attack7`：spec 附录 A / §9.1 的七个攻击构造，直接在 data_dir 的 sqlite
    副本上执行，供各腿的「构造已生效」回归测试使用。

PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 偏好 / 基建拓扑 / 真实会话内容。
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sqlite3
import subprocess

CASS_BIN = os.environ.get("CASS_BIN", "cass")

# 真 cass 只在 index 时产出这四个水位键；spec §5.5(a) 的必需水位键清单里其余的
# （4 个连接器 + last_embedded_message_id）需要 build_data_dir 事后补齐。
_CASS_PRODUCED_META_KEYS = (
    "last_scan_ts",
    "last_scan_ts:connector:claude",
    "last_indexed_at",
    "schema_version",
)
_CONNECTORS_TO_BACKFILL = ("codex", "gemini", "openclaw", "pi_agent")

# spec §5.5(a) 完整必需水位键清单（供测试断言复用，避免在测试里重复手写）。
REQUIRED_META_KEYS: tuple[str, ...] = _CASS_PRODUCED_META_KEYS + tuple(
    f"last_scan_ts:connector:{connector}" for connector in _CONNECTORS_TO_BACKFILL
) + ("last_embedded_message_id",)

_BASE_TS = datetime.datetime(2025, 12, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)


def _iso_ts(offset_seconds: int) -> str:
    ts = _BASE_TS + datetime.timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch_ms(offset_seconds: int) -> int:
    ts = _BASE_TS + datetime.timedelta(seconds=offset_seconds)
    return int(ts.timestamp() * 1000)


def _run_cass(args: list[str], home: pathlib.Path, timeout: int = 180) -> subprocess.CompletedProcess:
    """subprocess env 白名单只给 {PATH, HOME}——不让宿主的其它 env（XDG_DATA_HOME 等）
    渗进隔离 HOME 的语义。"""
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return subprocess.run(
        [CASS_BIN, *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def make_session_jsonl(path: pathlib.Path, n_msgs: int = 6, salt: str = "") -> pathlib.Path:
    """写一份能被真 cass claude_code connector 摄入的最小合成会话（全 nonsense 文本）。

    最小可摄入字段集（逆向自 franken_agent_detection claude_code connector 源码
    rev 77951e8——与 `cass 0.6.17` 的 Cargo.lock 锁定版本一致——并用真 `cass index`
    实测验证）：每条消息行只需
      `type`（"user"/"assistant"）+ `timestamp`（ISO8601 字符串）+
      `message: {role, content}`
    即可被摄入并计入 conversation/message。`sessionId`/`cwd` 非必需，但保留以贴近
    真实会话结构。首行写一条 `type:"summary"` 记录，验证「非 user/assistant 行不
    参与摄入、也不干扰摄入」（connector 对未知 type 直接 continue 跳过）。
    """
    session_id = f"synth-{salt}-{path.stem}"
    lines = [
        json.dumps({
            "type": "summary",
            "sessionId": session_id,
            "leafUuid": f"leaf-{salt}",
        })
    ]
    for i in range(n_msgs):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(json.dumps({
            "type": role,
            "timestamp": _iso_ts(i),
            "sessionId": session_id,
            "cwd": f"/synthetic-workspace/{salt}",
            "message": {"role": role, "content": f"lorem-{salt}-{i}"},
        }))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_data_dir(home: pathlib.Path) -> pathlib.Path:
    """隔离 HOME 下用真 `cass index` 自建完整 data_dir（真 schema 23 表 + raw-mirror
    manifests/blobs + 水位），再 INSERT 补齐 spec §5.5(a) 必需水位键中 cass 未产出的
    连接器水位 + `last_embedded_message_id`；补一条 `sources` 行（腿 3 §5.4 part 2 的
    「2→1 丢一半」测试需要 ≥2 行，真 cass 只自动登记 1 条本机 source）；补一张
    legacy `fts_messages` FTS5 表（生产库带着它，腿 3 §5.4 part 1 必需清单硬编码
    含它，合成库须对齐形态）。"""
    proj_dir = home / ".claude" / "projects" / "synthproj"
    make_session_jsonl(proj_dir / "session1.jsonl", n_msgs=6, salt="factory")

    result = _run_cass(["index", "--json"], home)
    if result.returncode != 0:
        raise RuntimeError(
            f"cass index 失败 rc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

    data_dir = home / ".local" / "share" / "coding-agent-search"
    db_path = data_dir / "agent_search.db"
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT value FROM meta WHERE key='last_scan_ts'").fetchone()
        if row is None:
            raise RuntimeError("cass index 未产出 last_scan_ts 水位，无法补齐连接器水位键")
        last_scan_ts = row[0]
        for connector in _CONNECTORS_TO_BACKFILL:
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (f"last_scan_ts:connector:{connector}", last_scan_ts),
            )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_embedded_message_id', '0')"
        )
        con.execute(
            "INSERT INTO sources(id, kind, host_label, machine_id, platform, config_json,"
            " created_at, updated_at) VALUES (?, 'synthetic', 'synth-host', 'synth-machine',"
            " 'synthetic', NULL, ?, ?)",
            ("synth-second-source", _epoch_ms(0), _epoch_ms(0)),
        )
        con.execute("CREATE VIRTUAL TABLE fts_messages USING fts5(content)")
        con.commit()
    finally:
        con.close()
    return data_dir


def attack1(db: pathlib.Path) -> None:
    """攻击库①：删 `meta` 表 + 它的 autoindex 的 schema 条目（spec 附录 A 逐字照抄，
    `.dbconfig defensive off` 对应 python sqlite3 的 `PRAGMA defensive=OFF`）。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA defensive=OFF")
        con.execute("PRAGMA writable_schema=ON")
        con.execute(
            "DELETE FROM sqlite_master WHERE name IN ('meta', 'sqlite_autoindex_meta_1')"
        )
        con.execute("PRAGMA writable_schema=RESET")
        con.commit()
    finally:
        con.close()


def attack2(db: pathlib.Path) -> None:
    """攻击库②：清空最多 1000 条 `messages.content`（spec 附录 A 逐字照抄；
    `LIMIT 1000` 在行数 < 1000 的合成库上天然退化为「清空全部」）。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "UPDATE messages SET content='' WHERE id IN "
            "(SELECT id FROM messages WHERE content<>'' ORDER BY id LIMIT 1000)"
        )
        con.commit()
    finally:
        con.close()


def attack3(db: pathlib.Path) -> None:
    """攻击库③：`agents` 表清空，不动 schema（§9.1 V5a）。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM agents")
        con.commit()
    finally:
        con.close()


_ATTACK4_AUTHOR = "attacker-author"


def attack4(db: pathlib.Path) -> None:
    """攻击库④：只改 `messages.author` 一列，其余列不动（§9.1 V5b）。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute("UPDATE messages SET author=?", (_ATTACK4_AUTHOR,))
        con.commit()
    finally:
        con.close()


def attack5(db: pathlib.Path, n_rows: int | None = None) -> None:
    """攻击库⑤：净缩尾——删掉 id 最大的一段连续尾部行（§9.1 V5c）。

    真实攻击删的是「上一份备份 max_id 之后」新增的 1000 行；本合成库没有基线概念，
    默认按比例删尾部 N/3 行，效果等价：MAX(id) 与 COUNT 同步下降、gap 仍为 0——这
    正是 spec 里「任何百分比行数阈值都会放行，只有单调性判据能拦」的那个构造。

    `n_rows`：显式指定删掉的尾行数（V5c 小幅净缩尾测试用 `n_rows=1`，删幅 <1%
    贴近 spec 真实场景 1000/213195≈0.47%）；None 时保持原有 N/3 行为。
    """
    con = sqlite3.connect(str(db))
    try:
        n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        k = n_rows if n_rows is not None else n // 3
        if k > 0:
            ids = [
                row[0]
                for row in con.execute(
                    "SELECT id FROM messages ORDER BY id DESC LIMIT ?", (k,)
                )
            ]
            con.executemany("DELETE FROM messages WHERE id=?", [(i,) for i in ids])
            con.commit()
    finally:
        con.close()


def attack6(db: pathlib.Path) -> None:
    """攻击库⑥：`meta.last_scan_ts` 改小（§9.1 V5d）。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute("UPDATE meta SET value='1' WHERE key='last_scan_ts'")
        con.commit()
    finally:
        con.close()


def attack7(db: pathlib.Path) -> None:
    """攻击库⑦：删掉 `meta` 里 `last_scan_ts` 整行（§9.1 V5d4）。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM meta WHERE key='last_scan_ts'")
        con.commit()
    finally:
        con.close()


def inject_separator_bytes(db: pathlib.Path) -> None:
    """腿 4 V5d3③ Tier A：往 `messages` 插一行 `extra_bin` 含 `0x1F`/`0x1E`/`0x1D`
    字节的 blob，验证编码器对「真实数据里就会出现的分隔符字节」的处置（spec §5.5：
    这些字节在生产 `extra_bin`——msgpack 二进制——里天然存在，不是理论问题）。

    用 `MAX(id)+1` 追加而非改写已有行，保持 gap=0（id 连续，见 spec §2.13）。
    """
    con = sqlite3.connect(str(db))
    try:
        max_id, first_conv = con.execute(
            "SELECT MAX(id), (SELECT id FROM conversations ORDER BY id LIMIT 1) FROM messages"
        ).fetchone()
        new_id = max_id + 1
        con.execute(
            "INSERT INTO messages (id, conversation_id, idx, role, author, created_at,"
            " content, extra_json, extra_bin) VALUES (?, ?, ?, 'assistant', 'synth-injector',"
            " 0, 'separator-bytes-probe', NULL, ?)",
            (new_id, first_conv, new_id, b"\x1f\x1e\x1d"),
        )
        con.commit()
    finally:
        con.close()
