> ⚠️ **SUPERSEDED（2026-06-27）by v2 单版本架构**：快照隔离整套已作废（plan v2 = fork 单版本独占，无快照）。
> 本文保留仅作**实测证据存档**：VACUUM INTO 事务一致快照 + `quick_check` + fork 写打开/迁移一份活 DB 副本可行
> ——其中「fork 0.6.17 写打开 + 迁移 0.6.13 库副本 → 全量索引跑通」这条证明了 v2 路2全新重摄入/迁移的安全性。

# CASS 一致性快照机制 de-risk verdict（Phase 0，v1 — SUPERSEDED）

> Plan: `cc-workspace/docs/projects/shared-memory/plans/2026-06-26-cass-semantic-production.md`
> 实测于 2026-06-26（生产 DB `/home/ivan/.local/share/coding-agent-search/agent_search.db`，1.26GB）。

## Verdict: ✅ 全绿，进 Phase B 解锁

snapshot 隔离命脉（对活 frankensqlite canonical DB 取**事务一致**快照、fork 可读）已实测可行。
选定机制 = **`sqlite3 VACUUM INTO`**（首选），pause-reflink 降为 fallback（本机用不上）。

## 1. baseline 摄入机制（pause/resume/quiescent 命令）

实测：**无常驻写进程**——
- `systemctl --user list-timers` 无 cass/index/ingest timer
- `crontab -l` 无 cass/index 条目
- `pgrep -af 'cass.*(watch|daemon|index)'` 无命中（仅本会话自身 shell 误命中）
- WAL = **0 bytes**，shm 32KB（无活跃写事务）
- `cass --help` 仅有 `daemon`（语义模型 daemon，非摄入写）

→ 摄入是**手动/批量触发**，DB 多数时间静默。**pause 退化为 no-op**，快照前 `verify_quiescent` 即可。

三条命令（写入 `snapshot-cmd.sh` 注释，本机生效形态）：
- `pause_cmd`：no-op（无常驻 writer）
- `resume_cmd`：no-op
- `verify_quiescent`：`lsof <db> | grep -qE ' [0-9]+[uw] '` → 有写句柄=BUSY，否则 QUIESCENT

## 2. 选定快照机制（实测数字）

| 项 | 结果 |
|---|---|
| 机制 | `sqlite3 -cmd "PRAGMA busy_timeout=60000" <live> "VACUUM INTO <dst>"` |
| rc | **0** |
| 墙钟 | **0.8s**（1.26GB → 659M 快照，VACUUM 顺带去碎片） |
| 一致性 | `PRAGMA quick_check` = **ok**（事务一致，非裸 cp 的 WAL 不一致） |
| fork 可读 | `CASS_DATA_DIR=<snap> cass-infinity status --json` → `opened:True, err:None` |

**推翻 codex R5「frankensqlite 不兼容系统 sqlite3」假设**：系统 sqlite3 (3.45.3) 能完整读写 frankensqlite
canonical DB（魔数 `SQLite format 3`，VACUUM INTO + quick_check 全 ok）。

cass **无** backup/snapshot 顶层命令（子命令列确认；`doctor backups` 只校验旧备份非创建）→ cass-backup 候选已删。

## 3. 隔离铁证（Phase B 首次全量建索引后回填，2026-06-26）

Phase B 在快照上跑了完整 do_snapshot + 词法 force-rebuild + bge-m3 全量 backfill（2670 会话/75939 docs，
~52min）。建索引**前后**复核 baseline 活 DB：

| 项 | 建索引后 |
|---|---|
| `cass status` opened | **True**（err None） |
| conv/msg 计数 | **2670 / 76076**（== 快照 fingerprint `content-v1:2670:2670:76076`，未变） |
| `PRAGMA quick_check` | **ok**（未被 migrate/损坏） |
| WAL 写入 | 无（全量建索引期间活 DB 零写） |

→ **隔离确认**：fork 全程只写快照副本 `$STAGE`，baseline 活 DB 逻辑计数 + integrity 完全不变。**状态：✅ CONFIRMED**

## 4. 一致性 verdict

事务一致机制（VACUUM INTO）**可得且 0.8s 极快**，摄入本就静默 → 无需真暂停服务。
**无 BLOCK，进 Phase A/B。**
