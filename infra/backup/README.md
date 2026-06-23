# infra/backup — 备份与 Restore Smoke

§11.3 备份覆盖范围 + R4 兜底验证。

## 备份内容清单

| 项目 | 路径/命令 | 说明 |
|------|-----------|------|
| brain repo markdown | `~/projects/brain/` → `$DEST/brain-repo/` | M2=export mirror；P4 起渐成 write-through 真理源 |
| GBRAIN_HOME state | `sandbox/gbrain-pg/.gbrain/` → `$DEST/gbrain-home/` | config.json 等配置 |
| canonical sqlite | `$CASS_CANON_DB` → `$DEST/canonical-YYYYMMDD.db` | CASS 读端（可选，路径以实际为准） |
| Postgres dump | `docker exec pg-memory pg_dump ...` → `$DEST/gbrain-pg-YYYYMMDD.sql` | 完整库 dump，含 source 分区（restore 首选） |

> **backup-brain.sh** 只读不破坏；brain repo 删本地目录即可回滚（推 GitHub 前需 Ivan 确认）。

## 跑法

```bash
# 备份（默认目标 ~/nas/openclaw/brain-backup/，可覆盖）
BRAIN_BACKUP_DEST=/path/to/dest bash infra/backup/backup-brain.sh

# Restore smoke（R4 兜底验证，不影响生产库）
bash infra/backup/restore-smoke.sh

# pytest 包装
uv run pytest tests/test_m2_backup_smoke.py -v
```

## 接入 NAS 备份流程

```bash
# NAS 挂载（已在 /etc/fstab，见 TOOLS.md）
# SMB: //192.168.2.5/openclaw → ~/nas/openclaw/

# 定时备份（Inngest cron 或 crontab）
# 示例 crontab（每天凌晨 3 点）：
# 0 3 * * * bash /home/ivan/projects/sharedmemory/infra/backup/backup-brain.sh >> /tmp/brain-backup.log 2>&1
```

## 已知限制

- `gbrain import` 不保 `source_id` 分区（单 source 导入）
- restore smoke 仅证明「页内容/frontmatter 可重建」，不证 source 分区
- **source 分区恢复**须用 pg dump（`$DEST/gbrain-pg-YYYYMMDD.sql`）：
  ```bash
  docker exec -i pg-memory psql -U gbrain gbrain < /path/to/gbrain-pg-YYYYMMDD.sql
  ```

## 计数说明

- `gbrain stats` 的 `Pages:` 行 = 全库页数（含所有 source）→ restore smoke 用此做基准
- `gbrain list` 只显示默认 source 的页（多 source 库会少计）→ 不用于计数断言

## 最近成功 restore smoke

<!-- restore-smoke.sh PASS 后手动更新 -->
| 日期 | pages | md5 probe slug | 结果 |
|------|-------|---------------|------|
| （待首次运行） | — | — | — |

## GitHub 推送（待 Ivan 确认）

brain repo 当前仅本地（`~/projects/brain`）。确认后执行：

```bash
cd ~/projects/brain
gh repo create ivanfuland/brain --private --source=. --push
```
