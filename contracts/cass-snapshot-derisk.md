# CASS 一致性快照机制 de-risk verdict（Phase 0）

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

## 3. 隔离铁证（Phase A 后回填）

> 待 Phase A Infinity 就绪 + Phase B 首次建索引后补：记活 DB 逻辑指纹（conv/msg 计数）→ 快照上跑
> do_snapshot+backfill → 复核活 DB 计数 + integrity 不变 = 隔离确认。

**状态：PENDING（Phase B Step 2 verify 回填）**

## 4. 一致性 verdict

事务一致机制（VACUUM INTO）**可得且 0.8s 极快**，摄入本就静默 → 无需真暂停服务。
**无 BLOCK，进 Phase A/B。**
