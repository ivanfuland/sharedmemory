# RESTORE-cass — CASS `data_dir` 灾难恢复 runbook

> 设计契约：`~/projects/cc-workspace/docs/projects/shared-memory/specs/2026-07-09-cass-data-dir-backup-design.md` §4.3 / §9.4。
> 恢复脚本：`infra/backup/restore-cass.sh`（**持锁 wrapper**，非一串文档命令）。
> 备份产物来源：`backup-cass.sh` 每晚发布到 `~/nas/openclaw/backups/cass/`。

## 什么时候用

- 工作站磁盘全损 / `~/.local/share/coding-agent-search` 不可读或损坏，需要从 NAS 备份重建。
- CASS 库损坏且手留回滚点（`.prev*` / `.corrupt-bak`）都不可用。
- **演练**（定期验证备份真能恢复，spec §9.4 V21–V26）。

> ⚠ 只有 CASS 索引层（`data_dir`）损坏、而会话源 jsonl（`~/.claude/projects` 等）完好时，
> **不需要**恢复会话源——直接从备份的 `db` + 共享 blob 池重建即可。只有源 jsonl 也丢了才 `--sessions-into-source`。

## 为什么必须是持锁 wrapper（别改成文档命令堆）

`flock -n 9 9>lock || exit 1` 在复合命令**结束时立即释放锁**，之后整个 restore 无锁保护。
正解：`exec 9>lock` 在**同一 shell 内持锁全程**。且 `exec 9>` 的 fd 会被子进程继承、bash 不设
`O_CLOEXEC`——**每个子进程调用都要 `9>&-`**，否则收尾 `systemctl --user start cass-mcp` 拉起的
常驻服务会**永久持有** `.cass-write.lock` → 此后每小时的 `index-pull.sh` 全部静默跳过（抢不到锁 exit 0 不告警）。

## preflight（脚本自动跑，缺一即停）

1. `uv run python -c "import blake3"`（restore 硬依赖；裸 `python3` 无 blake3）——须在仓根跑
2. `systemctl --user stop cass-mcp`（它是 CASS 写者）
3. `pgrep -x cass-infinity` 无输出（daemon 也是写者）
4. 抢到 `~/.local/share/.cass-write.lock` 写锁（fd 9，全程持有）

## 用法

```bash
# 演练（V25 零生产改动）：目标是临时目录，不写回生产会话源；可 --skip-semantic 快速冒烟
infra/backup/restore-cass.sh --data-dir /tmp/cc-restore-drill --skip-semantic

# 演练（完整，含 ≈2h semantic 重建）
infra/backup/restore-cass.sh --data-dir /tmp/cc-restore-drill

# 真灾难：恢复到全新目录（默认取 latest 含 COMPLETE 的备份）
infra/backup/restore-cass.sh --data-dir ~/.local/share/coding-agent-search.new

# 指定备份 + 也恢复会话源（源 jsonl 也丢了才用，写回生产源须 --yes）+ 重扫全史
infra/backup/restore-cass.sh --data-dir <新目录> --backup cass-20260712-162753-1982094 \
    --sessions-into-source --yes --rescan-history
```

参数：
- `--data-dir <目录>`（**必填**）：恢复目标，**必须是全新/空目录**（脚本拒绝非空，防误覆盖生产）。**且不能等于 live canonical**（`$CASS_CANONICAL_DIR`，默认 `~/.local/share/coding-agent-search`）——强制 staging + swap（恢复到 `<canonical>.new`，验证全过后人工 swap）；直接落 canonical 会让失败后 cleanup 重启的 cass-mcp 读到半恢复库（codex R10，spec V25 零生产改动）。
- `--backup <cass-<stamp>|latest>`：默认 `latest`（NAS 上含 `COMPLETE` 的最新 `cass-*/`）。
- `--sessions-into-source`：把 `$DEST/sessions/<alias>/` 的 jsonl 写回**生产源根**（`~/.claude/projects` 等）。**只用于源 jsonl 全丢的空目录场景**——若任一源根已有 `.jsonl` 则 **fail-closed 中止**（`--append` 会从错误 offset 拼坏、`--ignore-existing` 会静默跳过截断文件报成功，两者对 sessions 都不安全）。**改 live 数据 → 强制要 `--yes`**。**演练/有残留会话勿用**：改 `--sessions-into <staging>` 恢复到暂存区、人工核对后再合入。
- `--sessions-into <目录>`：把会话源恢复到指定前缀（演练用，不碰生产源）。
- **会话恢复 fail-closed 门**（任一 `--sessions-into[-source]`，复制前跑，codex R10）：校验所选备份 `sessions.tsv` 与 `digest.sessions_tsv_sha256` 自洽，且清单每条会话在共享池 `$DEST/sessions/<relpath>` **存在 + size + blake3 相符**；池缺失/少文件/腐烂即 **FATAL**（不再 rsync 残缺集合还报成功）。走可测模块 `cass/restore_sessions_check.py`。
- `--rescan-history`：置 `meta.last_scan_ts:* = 0`，强制重扫全部历史（否则 >1 GiB 库缺水位会 bootstrap 成"当前时刻"、跳过历史）。
- `--skip-semantic`：只重建 lexical（跳过 ≈2h semantic），供快速冒烟。

环境：`CASS_BACKUP_DEST`（默认 `~/nas/openclaw/backups/cass`）· `CASS_BIN`（默认 `~/.local/bin/cass-infinity`）· `CASS_INFINITY_URL`（默认 `http://127.0.0.1:7997`）· `CASS_CANONICAL_DIR`（默认 `~/.local/share/coding-agent-search`，canonical guard 用它判 `--data-dir` 是否落生产库）。

## 脚本做的 8 步（spec §4.3）

| 步 | 做什么 | 判据 / 陷阱 |
|---|---|---|
| 0 | preflight（见上） | 缺一即停 |
| 1 | 目标全新空目录 | 非空即拒（防覆盖生产）；**且 `--data-dir` ≠ live canonical**（否则失败后 cleanup 重启 cass-mcp 读半恢复库，codex R10） |
| 2 | 校验产物：`COMPLETE` + `db.sha256` + `manifests.sha256sum` | **`dd iflag=direct` 读**（否则校验的是页缓存）；管道查 `PIPESTATUS` |
| 3 | `db` → `<target>/agent_search.db` | **绝不拷 `-wal`/`-shm`**（`.backup` 产物本无；陈旧 WAL 会读到半事务，§9.4 V26） |
| 4 | raw-mirror：blob 取**共享池** `$DEST/raw-mirror/v1/blobs`，manifest 取**本备份快照** `$BK/manifests`；可选会话源恢复（`--sessions-into[-source]`） | `chmod 700 raw-mirror`；manifest 用别时刻的会不自洽；会话恢复**复制前**过 fail-closed 门（`cass/restore_sessions_check.py`：sessions.tsv↔digest 自洽 + 池内每条 存在/size/blake3 相符），池缺/腐烂即 FATAL（codex R10） |
| 5 |（可选，脚本默认不跑）`restore-from-mirror.py` | 仅当需重建源清单 `sources.toml`；它落 staging fake-HOME、不落 DB |
| 6 | 重建 Tier 2：删 `index/`、`vector_index/`，lexical（`--force-rebuild`，分钟级）+ semantic（`models backfill` 循环，≈2h） | **绝不用 `index --full` / `index --semantic` 全量**（上游 #244/#258 死锁/stall）；重建期 `CASS_INDEX_STALL_ABORT_SECS=0` |
| 7 | meta 水位 | 别删 `last_scan_ts:*`；`--rescan-history` 才置 0 |
| 8 | 验证：doctor `raw_mirror.summary.*` 全 0 **且 `verified_blob_count>0`**（走可测模块 `cass/restore_verify.py`）+ `cass search` 有命中（semantic；`--skip-semantic` 时验 **lexical**，不整跳） | **必须额外断言 `verified_blob_count>0`**（零错误与没检查在计数器上同形）；semantic 门用**生产 cass-mcp 真依赖路由** `--mode semantic --daemon --model bge-m3 --rerank`（== `cass_mcp/config.py` `SEMANTIC_FLAGS`，一致性由集成测试守，codex R9）；`fts_messages_config` abort 是良性（§2.5），不算失败 |
| 收尾 | `systemctl --user start cass-mcp 9>&-` | **`9>&-` 关键**，否则常驻服务永久持锁 |

真灾难收尾：把恢复出的目录切成 canonical（移开损坏库后 `mv`/symlink），核对 `cass search` / `cass-mcp` 指向新库。
`cass mirror prune` 后缺失的 blob 只能从更早备份找回（脚本不 prune）。

## 演练验收（V21–V26，spec §9.4 — **✅ 2026-07-13 全绿，DoD 达成**）

2026-07-13 分两段真跑（① `--skip-semantic` 快速冒烟 → ② 完整 semantic 演练），恢复到 `/tmp` 暂存目录，实测停 cass-mcp **97min**（全程）。各项：

- **V21** 跑 `restore-cass.sh`（持锁 wrapper）；preflight 的 `uv run python -c "import blake3"` 必须真跑（裸 `python3` 会 FATAL）。
- **V22** 演练全程另一 shell 的 `flock -n` 必须**失败**（锁被持有）。
- **V22a** wrapper 退出后（含 `systemctl start cass-mcp`）另一 shell 的 `flock -n` 必须**成功** + `fuser .cass-write.lock` 无输出（验 `9>&-` 真释放）。
- **V23** 演练后 doctor `raw_mirror.summary.*` 全 0 且 `verified_blob_count>0`；`cass search` 命中一条已知会话。
- **V24** 显式经过 Tier 2 semantic 重建（`--skip-semantic` **不加**），把**实测耗时**回填本文档。
- **V25** 全程零生产改动（生产 `data_dir` 的 mtime 不变；用 `--data-dir /tmp/...`、不 `--sessions-into-source`）。
- **V26** 造一个**非空** stale `-wal` 放恢复出的 db 旁边，观察并记录后果（NAS 上 7-04 那份 `-wal` 是 0 字节，证明不了，别用作夹具）。

> **实测耗时**（2026-07-13 完整演练回填，2677 conv / 244711 msg）：**lexical ~21s / semantic ~85min（全程 restore 97min）**。
> step8 semantic 验证用生产路由 `--mode semantic --daemon --model bge-m3 --rerank` 命中 3 条；V25 生产 `data_dir` mtime 全程零变。
> 停 cass-mcp 期间 hourly index-pull 靠 `flock -n` 干净 skip（`exit 0`，无告警、无丢数据；新会话下个 hourly 补齐）。
