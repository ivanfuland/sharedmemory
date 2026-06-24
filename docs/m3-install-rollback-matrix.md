# M3 安装/回滚矩阵

> 状态截止：2026-06-24（Task 13 收口）。
> **约定**：「已就绪-未激活」= 代码/配置已在仓库，可随时激活，但未对生产数据运行过；「已锁定」= 配置已写入 + 测试通过 + 不再变更。
> **前置**：本矩阵所有组件均运行在 **Ubuntu Hub**（主工作站 `192.168.2.50`）。M2 矩阵中已就绪的组件（pg-memory、gbrain-mcp service、OAuth clients）本文不重复，仅列 M3 新增内容。

---

## 机器说明

| 代号 | 机器 | 角色 |
|------|------|------|
| **Ubuntu Hub** | 主工作站 Ubuntu 22.04 + RTX 4090（`192.168.2.50`） | M3 hub，所有蒸馏桥组件均在此机 |

---

## 1. `distill/` Python 包

| 项目 | 内容 |
|------|------|
| **状态** | ✅ 已就绪（无需安装，`uv run` 直接可用） |
| **路径** | `distill/`（repo root 下，`uv` workspace 管理） |
| **运行** | `uv run python -m distill.run`（或通过 `infra/distill/run-bridge.sh` wrapper） |
| **回滚** | 停止 Inngest function 触发（见下文 §4）即可隔离；包本身不需要卸载 |
| **验证** | `cd ~/projects/sharedmemory && uv run pytest tests/test_m3_*.py -q`（57 passed） |
| **备注** | 依赖 Python ≥3.10；`uv.lock` 已固化所有依赖版本 |

---

## 2. `config/m3-bridge.json` + `infra/distill/config.env`

| 项目 | 内容 |
|------|------|
| **状态** | ✅ 已就绪（`config/m3-bridge.json` 模型锁定 locked；`infra/distill/config.env` 含所有必须环境变量） |
| **路径** | `config/m3-bridge.json`（进 git）；`infra/distill/config.env`（**gitignored**，含 DISTILL_API_KEY 等；仅 `config.example.env` 进 git） |
| **secrets 位置** | `infra/gbrain/clients.env`（**不进 git**，含 `HUB_BRIDGE_CLIENT_ID/SECRET`）；`DISTILL_API_KEY` 在 `infra/distill/config.env`（当前为 LiteLLM test key，生产须更换） |
| **权限** | `infra/distill/config.env` 已存在于 repo；`clients.env` 须保持 `chmod 600` |
| **回滚模型锁** | 编辑 `config/m3-bridge.json`，将 `model_lock.status` 从 `locked` 改为 `pending`，删除 `model` 字段 → 解锁，下次批次会重新跑质量门选模型 |
| **回滚 API key** | 替换 `DISTILL_API_KEY` 值（旧 key 不会有副作用，LLM 调用按 key 计费） |
| **验证** | `uv run python -c "from distill import config; c=config.load(); print(c['model_lock'])"` → `{'status': 'locked', 'model': 'gpt-5.4-mini', ...}` |

---

## 3. Inngest cron function（`distill-bridge-daily`）

| 项目 | 内容 |
|------|------|
| **状态** | ⚠️ 已构建-未部署（`infra/distill/run-bridge.sh` CLI 入口完整；`jarvis-workflow-ts` 侧 route 文件**尚未创建**） |
| **调度** | 每日 **03:30 CST**（`TZ=Asia/Shanghai 30 3 * * *`）；Inngest 侧 concurrency=1；桥内 `flock` 双保护 |
| **安装（route 文件）** | 在 `~/projects/jarvis-workflow-ts/src/inngest/functions/` 创建 `distill-bridge.ts`（Inngest function，调用 `infra/distill/run-bridge.sh`）；在 `src/inngest/index.ts`（或 `router.ts`）注册该函数；`pm2 restart jarvis-workflow` 重新部署 |
| **回滚（停调度）** | 从 `jarvis-workflow-ts` 的 function 注册列表移除 `distill-bridge.ts`（删文件 + 从 index.ts 去掉引用）→ `pm2 restart jarvis-workflow` → 桥 cron 不再触发；state sqlite 和已落库数据不受影响 |
| **回滚（紧急停桥）** | `kill $(cat ~/projects/sharedmemory/infra/distill/.bridge.lock 2>/dev/null)` 或 `fuser -k infra/distill/bridge-state.db`；桥内 flock 保证重启后幂等恢复 |
| **验证（部署后）** | `curl http://localhost:8288/v0/gql -d '{"query":"{functions{slug}}"}'` → 结果含 `distill-bridge-daily`；Inngest dev server UI 可见 + 手动 invoke 一次 |
| **备注** | M4 激活前须先满足 M3-EXIT §M4 激活前置清单 7 项；不得用订阅 OAuth，只走 `DISTILL_API_KEY`（铁律） |

---

## 4. Bridge state sqlite（`infra/distill/bridge-state.db`）

| 项目 | 内容 |
|------|------|
| **状态** | ⚠️ 已定义-未初始化（schema 在 `distill/state.py`；文件在首次 `uv run python -m distill.run` 时自动建立） |
| **路径** | `infra/distill/bridge-state.db`（由 `config.env` `BRIDGE_STATE_DB` 指定；不进 git，在 `.gitignore`） |
| **初始化** | 首次运行 `distill.run` 或直接 `uv run python -c "from distill import state; state.connect('infra/distill/bridge-state.db')"` → 自动建表（`cursor`/`raw_work_item`/`journal`/`source_quarantine`/`replay_ledger`） |
| **回滚（重置游标）** | `rm infra/distill/bridge-state.db` → 下次运行从全量重蒸馏；**幂等不重复落库**（idempotency key UNIQUE 约束 + `INSERT OR IGNORE`）；已落 gbrain 的条目不会重写 compiled truth（只追加 timeline） |
| **回滚（仅重置游标）** | `sqlite3 infra/distill/bridge-state.db "DELETE FROM cursor;"` → 保留 raw_work_item/journal 历史，下次从头读 CASS；比删整库更精细 |
| **备份** | `infra/backup/backup-brain.sh` 每次运行自动备份到 `$BRAIN_BACKUP_DEST/bridge-state.db`，并写 `last-restore-ok` 时间戳 |
| **验证** | `sqlite3 infra/distill/bridge-state.db ".tables"` → 含 `cursor raw_work_item journal source_quarantine replay_ledger` |

---

## 5. 模型锁（`config/m3-bridge.json` model_lock）

| 项目 | 内容 |
|------|------|
| **状态** | ✅ 已锁定（`gpt-5.4-mini`，locked_at=2026-06-24T06:09:52Z，P=0.923/R=0.857） |
| **锁定原理** | `distill/quality_eval.py` 跑 22 样本质量评估，P≥0.9 且 R≥0.8 → 写回 `config/m3-bridge.json`；`distill/distiller.py` 启动时读此文件，若 `status=locked` 直接使用 `model` 字段，跳过动态选模型 |
| **解锁重标** | 编辑 `config/m3-bridge.json`，改 `model_lock.status` → `pending`，删 `model` 字段 → 下次批次 `quality_eval.py` 重跑（需真实 LLM 调用，约 22 次 API 请求） |
| **回滚到旧模型** | 直接改 `config/m3-bridge.json` `model` 字段为目标模型名 + 保持 `status=locked` → 立即生效，无需其他操作 |
| **验证** | `python3 -c "import json; d=json.load(open('config/m3-bridge.json')); print(d['model_lock'])"` |
| **备注** | 模型 key 走 `DISTILL_API_KEY`（LiteLLM 路由层），换模型时确认该 key 对应的 LiteLLM endpoint 支持目标模型名 |

---

## 快速状态汇总（M3 新增，M2 组件不重复）

| 组件 | 状态 | 需激活 |
|------|------|--------|
| `distill/` Python 包 | ✅ 就绪（uv run） | — |
| `config/m3-bridge.json` 模型锁 | ✅ 已锁定（gpt-5.4-mini） | — |
| `infra/distill/config.env` | ✅ 就绪 | 生产须更换真 API key |
| Bridge state sqlite | ⚠️ 定义-未初始化 | 首次 run 自动建立 |
| Inngest cron function | ⚠️ 已构建-未部署 | M4 激活清单全 PASS 后 |
| 备份覆盖（bridge state）| ✅ 就绪（backup-brain.sh 已加） | — |

> **激活清单完整命令** → `contracts/M3-EXIT.md §M4 激活前置清单`
