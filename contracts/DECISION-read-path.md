# 读端路线决策（M0 Task 3）

## 决策：canonical（standard sqlite3 直读）

`probe-cass-schema.sh` 实测 **COMPAT: OK**——标准 sqlite3 能读 frankensqlite（cass 0.6.13 baseline）。
逐字段非空 + 游标合格全部通过，canonical 路线成立，**不走 raw-jsonl fallback（Task 3b 跳过）**。

## 触发条件复核（spec/plan：任一不成立才转 fallback）
- COMPAT：OK ✓
- 必需字段非空（role/content/timestamp/agent/session/provenance）：✓（JOIN read_sql 首条完整）
- 游标 messages.id：INTEGER PRIMARY KEY，76076 行无 NULL 无重复 ✓
- 读腿一次取齐：✓（test_read_sql_required_fields_present_and_nonempty）

四条全过 → canonical。

## 蒸馏桥读端要点（交接 M3）
- 增量游标 = `messages.id` 全局单调；`WHERE m.id > :last_seen ORDER BY m.id ASC`
- 规范化 schema，须 JOIN messages+conversations+agents+workspaces（read_sql 已固化在 fields.json）
- 噪声/来源过滤用 `agents.slug`（claude_code/codex/openclaw/*）
- workspace 可空（~0.05%），桥须容忍
- canonical DB ~1.2GB，frankensqlite 写、标准 sqlite3 只读兼容
