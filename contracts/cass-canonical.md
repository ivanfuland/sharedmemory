# CASS Canonical 读端契约

> 基线 CASS 版本：cass 0.6.13 baseline（见 scripts/cass-version.txt）
> Schema 指纹：见 cass-canonical.fingerprint（覆盖 messages/conversations/agents/workspaces）
> 蒸馏桥启动必须比对指纹，不匹配则拒绝运行 + 告警，绝不猜测式读取。

## Canonical DB
- 路径：`~/.local/share/coding-agent-search/agent_search.db`
- data-dir（cass search 必带）：`~/.local/share/coding-agent-search`

## Schema 是规范化的（非单表）
canonical 不是扁平消息表，而是 messages + conversations + agents + workspaces 规范化结构。
蒸馏桥读取须 JOIN（见 cass-canonical-fields.json 的 `read_sql`）：

- `messages`：id(INTEGER PK) / conversation_id / idx / role / author / created_at / content / extra_json / extra_bin
- `conversations`：id / agent_id / workspace_id(可空) / source_path / external_id / title / ...
- `agents`：id / slug / name / kind（slug 如 claude_code / codex / openclaw/main）
- `workspaces`：id / path / display_name

## 字段映射（读端契约）
| 语义 | 来源 |
| --- | --- |
| **增量游标** | `messages.id`（INTEGER PRIMARY KEY，单调、唯一、无 NULL） |
| role | `messages.role` |
| content | `messages.content`（NOT NULL） |
| timestamp | `messages.created_at`（epoch ms） |
| agent | `agents.slug`（经 conversations.agent_id JOIN） |
| workspace | `workspaces.path`（经 conversations.workspace_id LEFT JOIN，**可空 ~0.05%**） |
| session | `conversations.id`（或 external_id / source_path 区分会话） |
| provenance | `conversations.source_path || ':' || messages.idx`（回指源 jsonl + 行内序号） |

## 读取协议
- 增量：`WHERE m.id > :last_seen ORDER BY m.id ASC`，全局单调游标（id 跨 conversation 全局递增）
- 来源过滤：`WHERE a.slug = ?`（claude_code/codex/openclaw/*）/ `WHERE w.path = ?`
- 已发现 agent slug：claude_code, codex, gemini, openclaw/{alice,clawra,javich,justin,main,wood}, pi_agent

## 决策记录
读端路线 = **canonical**（standard sqlite3 兼容，逐字段非空 + 游标合格，契约成立）。详见 contracts/DECISION-read-path.md。
