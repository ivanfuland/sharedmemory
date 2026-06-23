# M2 安装/回滚矩阵

> 状态截止：2026-06-23（Task8 收口）。
> **约定**：「已安装」= 当前已运行/注册；「已构建-未激活」= 脚本/文件已在仓库，但未执行激活步骤（不修改任何宿主配置）。

## 机器说明

| 代号 | 机器 | 角色 |
|------|------|------|
| **Ubuntu Hub** | 主工作站 Ubuntu 22.04 + RTX 4090（`192.168.2.50`） | M2 hub，所有已安装组件均在此机 |
| **Alienware** | 笔记本 Win11/Ubuntu（`ivancomputer`） | M2 收尾小步目标机，需 window CC，当前未涉及 |

---

## 组件矩阵

### 1. pg-memory Postgres 容器

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub |
| **状态** | ✅ 已安装（运行中） |
| **安装命令** | `cd ~/projects/sharedmemory/infra/pg-memory && docker compose up -d` |
| **回滚命令** | `cd ~/projects/sharedmemory/infra/pg-memory && docker compose down -v`（`-v` 删卷；业务库 postgres:16 不受影响） |
| **验证** | `docker ps \| grep pg-memory`；`psql -h 127.0.0.1 -p 5432 -U postgres -c '\l'` |
| **备注** | 端口 5432（本地）；pgvector + pg_trgm + pgcrypto 扩展已建；Docker registry 走直连（去掉 1ms.run mirror） |

---

### 2. gbrain-mcp systemd user service（`serve --http`）

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub |
| **状态** | ✅ 已安装（运行中，绑 `127.0.0.1:7777`） |
| **安装命令** | 见 `infra/gbrain/serve/` 内的安装脚本；核心：`systemctl --user enable --now gbrain-mcp.service` |
| **回滚命令** | `systemctl --user disable --now gbrain-mcp.service && rm ~/.config/systemd/user/gbrain-mcp.service && systemctl --user daemon-reload` |
| **验证** | `systemctl --user status gbrain-mcp.service`；`curl -s http://127.0.0.1:7777/health` |
| **备注** | EnvironmentFile = `infra/gbrain/serve/env.generated`（shell-safe，由 config.env 生成）；`GBRAIN_HOME=$HOME/projects/sharedmemory/sandbox/gbrain-pg`；KillSignal=SIGINT；自启自愈 |

---

### 3. OAuth clients（6 个 scoped client_credentials）

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub（gbrain serve 注册） |
| **状态** | ✅ 已注册（hub-cc / hub-codex / hub-openclaw / hub-bridge 读写 30d TTL；hub-readonly 只读；hub-shortlived 2s TTL） |
| **安装命令** | `bash infra/gbrain/register-clients.sh`（幂等：检测已存在则跳过） |
| **回滚命令** | `gbrain auth revoke-client <client_id>`（逐个撤销）；或 `gbrain auth list` 查看再选择 |
| **验证** | `gbrain auth list`；`curl -s -X POST http://127.0.0.1:7777/token -d grant_type=client_credentials -d client_id=<id> -d client_secret=<secret>` → `expires_in=2592000`（30d） |
| **备注** | 凭证存 `infra/gbrain/clients.env`（不进 git，已在 `.gitignore`）；生产 hub-cc token_ttl=30d（serviceunit ExecStart 带 `--token-ttl 3600` 为 serve 全局 session_ttl；client TTL 由注册时 `--token-ttl` 参数控制） |

---

### 4. SessionStart hook 脚本（三端 adapter）

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub（脚本在仓库；激活后各宿主读） |
| **状态** | ⚠️ 已构建-未激活（脚本已在 `hooks/`；未接入任何宿主配置） |
| **脚本路径** | `hooks/cc_sessionstart.sh`（CC）、`hooks/codex_sessionstart.sh`（Codex）、`hooks/openclaw_bootstrap.sh`（OpenClaw） |
| **激活（CC）** | 编辑 `~/.claude/settings.json`，在 `hooks.SessionStart` 数组添加 `{"matcher":"**","hooks":[{"type":"command","command":"bash ~/projects/sharedmemory/hooks/cc_sessionstart.sh"}]}`；详见激活清单 §① |
| **激活（Codex）** | 在 Codex 会话启动前手动执行 `bash ~/projects/sharedmemory/hooks/codex_sessionstart.sh`，或接入 Codex 的自动启动钩子；详见激活清单 §② |
| **激活（OpenClaw）** | 在目标 agent 的 AGENTS.md refresh 触发时手动运行，或定时 cron；详见激活清单 §② |
| **回滚（CC）** | 从 `~/.claude/settings.json` 删除 SessionStart hook 条目 |
| **回滚（Codex/OpenClaw）** | 删除 AGENTS.md 中的 `<!-- gbrain-digest:begin -->...<!-- gbrain-digest:end -->` 块；`.gbrain-digest.bak` 存有原始备份 |
| **验证** | `bash hooks/cc_sessionstart.sh CLAUDE_PROJECT_DIR=$PWD` → stdout 含 `[[slug]]`；test_m2_consistency.py 全绿 |

---

### 5. brain repo scaffold（R4 兜底/备份）

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub（本地 git）；GitHub 端未 push（planned：`ivanfuland/brain` private） |
| **状态** | ⚠️ 本地已建-未推送（`~/projects/brain` 已 init + scaffold 目录 + initial commit） |
| **安装命令** | `bash infra/backup/backup-brain.sh`（export + commit + push）；首次 push 见激活清单 §⑤ |
| **回滚命令** | 仅本地 git，无远端影响；`rm -rf ~/projects/brain` 删本地副本；`gbrain` DB 不受影响（brain repo 是 export 镜像，不是主存储） |
| **验证** | `ls ~/projects/brain/`（含 people/projects/decisions/preferences/）；`bash infra/backup/restore-smoke.sh` → 计数匹配 |
| **备注** | `gbrain import` 不保留 source_id 分区（已知限制，R4 兜底级恢复）；export 往返 smoke 经 test_m2_backup_smoke.py 验证 |

---

### 6. 备份脚本

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub |
| **状态** | ✅ 脚本已建，可随时手动跑 |
| **备份命令** | `bash ~/projects/sharedmemory/infra/backup/backup-brain.sh` |
| **恢复命令** | `bash ~/projects/sharedmemory/infra/backup/restore-smoke.sh`（smoke 验证）；完整恢复见 contracts/M2-EXIT.md §回滚 |
| **验证** | `ls ~/projects/brain/` 含各类目录；`restore-smoke.sh` exit 0 + 计数匹配 |
| **备注** | 覆盖范围：canonical brain repo（export）+ raw gbrain DB（`sandbox/gbrain-pg/`）+ pg-memory 数据（docker volume）；不自动调度（P4 可接 cron） |

---

### 7. 会话中 MCP 接线脚本 + token-refresh timer

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub |
| **状态** | ⚠️ 已构建-未激活（`infra/gbrain/mcp/` 内脚本完整，systemd timer 文件已写，未 install） |
| **脚本** | `infra/gbrain/mcp/connect-cc.sh`（re-mint + `gbrain connect --install`）；`infra/gbrain/mcp/token-refresh.sh`（任意 client 刷 token） |
| **激活（一次性 MCP 接线）** | 在 Ivan 终端：`bash ~/projects/sharedmemory/infra/gbrain/mcp/connect-cc.sh`（写 `~/.claude/.mcp.json` + token cache）；详见激活清单 §③ |
| **激活（timer 每日轮转）** | `cp infra/gbrain/mcp/gbrain-token-refresh.{service,timer} ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now gbrain-token-refresh.timer`；详见激活清单 §③ |
| **回滚（MCP 接线）** | 从 `~/.claude/.mcp.json` 删除 gbrain-mcp 条目；`rm ~/.config/gbrain/hub-cc.token` |
| **回滚（timer）** | `systemctl --user disable --now gbrain-token-refresh.timer && rm ~/.config/systemd/user/gbrain-token-refresh.{service,timer}` |
| **验证** | `cat ~/.claude/.mcp.json \| grep gbrain`；`systemctl --user list-timers \| grep gbrain`；`curl -H "Authorization: Bearer $(cat ~/.config/gbrain/hub-cc.token)" http://127.0.0.1:7777/mcp` |

---

### 8. tailscale serve（tailnet 暴露）

| 项目 | 内容 |
|------|------|
| **机器** | Ubuntu Hub |
| **状态** | ⚠️ tailnet-ready（文档 + 路径白名单 + 客户端注册流程 = 已备，serve 命令未执行） |
| **激活命令** | **必须 Ivan 在真终端 sudo 跑**（非 CC，MEMORY.md tailscale 硬坑）：`sudo tailscale serve --bg --https=443 --set-path /mcp http://127.0.0.1:7777/mcp` 等；详见 `infra/gbrain/mcp/tailnet-serve.md` 和激活清单 §④ |
| **回滚命令** | `sudo tailscale serve reset` |
| **验证** | `tailscale serve status`；从 Alienware `curl https://<hostname>.tail567e5a.ts.net/health` |
| **备注** | 不暴露 `/ingest`、`/admin`（缓解 header-trust 写越权面）；M2 收尾小步：Alienware 跨写端到端测，需 window CC |

---

## 快速状态汇总

| 组件 | 状态 | 需激活 |
|------|------|--------|
| pg-memory 容器 | ✅ 运行中 | — |
| gbrain-mcp service | ✅ 运行中 | — |
| OAuth clients（6个） | ✅ 已注册 | — |
| CC SessionStart hook | ⚠️ 未激活 | 激活清单 §① |
| Codex/OpenClaw hook | ⚠️ 未激活 | 激活清单 §② |
| brain repo（本地） | ⚠️ 本地已建 | 激活清单 §⑤（push） |
| MCP 接线 + timer | ⚠️ 未激活 | 激活清单 §③ |
| tailscale serve | ⚠️ 未激活 | 激活清单 §④ |

> **激活清单完整命令** → `contracts/M2-EXIT.md §🔑 激活清单`
