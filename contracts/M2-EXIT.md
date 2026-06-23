# M2 出口确认（P2 hub-complete + tailnet-ready，P2 未完成）

> **第一行 framing（binding）**：M2 = P2 hub-complete + tailnet-ready，P2 未完成。
> 两条 P2 验收——Alienware 经 tailnet 真跨写可见、rsync stale source 告警——依赖第二台机 + window CC，
> 显式移交 M2 收尾小步（= P2-tail）。本出口确认不等于「P2 完成」。

---

## 出口判据逐条（spec §5 P2 + §2.5.3/§2.8/§2.9 + Ivan 三决策）

- [x] **serve --http systemd 单例常驻**：绑 `127.0.0.1:7777`（非 `0.0.0.0`）；自启自愈（Restart=on-failure）；`/health` 200 可达（Task2；test_m2_service 4 测绿）
- [x] **三端 scoped client（client_credentials）写隔离/读联邦真跑成立**：
  - hub-cc/codex/openclaw/bridge 换 token，写落对应 source（DB source_id = token-bound，不可客户端覆盖）
  - hub-readonly 写拒（403）；无/bogus token 拒（401）；bridge 越权写拒
  - federated_read ALL 客户端可见其他 source 条目（Task1；test_m2_oauth_scoping 8 测绿）
- [x] **三端 SessionStart 自动 digest（hub 本地 CLI）**：阈值过滤（GBRAIN_DIGEST_THRESHOLD env override + config JSON + 保守默认 0.75）；stale 降级注 `(stale：有新证据待整编)`；≤1500 token 截断；fail-soft（服务 down→注空不崩，exit 0）（Task3；test_m2_digest 7 测绿）
- [x] **阈值标定 harness + 保守初值**：`config/m2-thresholds.json` 已写，status=`uncalibrated_default`；方法说明 P4 重标（Task4；test_m2_threshold 3 测绿）
- [x] **brain repo scaffold（people/projects/decisions/preferences）**：本地 `~/projects/brain` 已 init；export/sync 管道；restore smoke 通（计数匹配，Task5；test_m2_backup_smoke 1 测绿）
- [x] **lookup protocol + 分拣协议进三端记忆文件 + hook 真接线**：`protocols/lookup-protocol.md`（gbrain query → fallback → MCP pull 三步）；codex/openclaw 写入 AGENTS.md（不写 memories/ 目录，经实测验证）；路径覆盖 env var（`CODEX_AGENTS_FILE` / `OPENCLAW_AGENTS_FILE`）（Task6；test_m2_protocol_wired 11 测绿）
- [x] **会话中 pull/写 MCP 接线 + token 生命周期 + tailnet-ready**：`connect-cc.sh`（re-mint + `--install`）；`token-refresh.sh` + timer unit；token_ttl=30d 实测（`expires_in=2592000`）；expiry→remint recovery 通；tailnet-serve.md 文档 + 路径白名单；客户端注册流程（Task7；test_m2_mcp_pull 5 测绿）
- [x] **★ P2 正例：三端同问一实体答案一致**：`test_m2_consistency.py` 真跑三端 adapter（cc/codex/openclaw）→ top slug 一致且命中种子页（Task8；1 测绿）
- [x] **负例矩阵全过**：服务 down→注空（test_m2_digest `test_fail_soft_on_query_error`）；read-only 写拒（test_m2_oauth_scoping `test_readonly_client_write_denied`）；无/bogus token 拒（`test_no_token_and_bogus_rejected`）；malformed query 不崩（`test_fail_soft_on_query_error`）；绑 127.0.0.1（`test_not_bound_to_wildcard`）
- [x] **安装/回滚矩阵完整**：`docs/m2-install-rollback-matrix.md`，每组件有安装/回滚/验证命令（Task8）
- [x] **全部 `uv run pytest tests/test_m2_*.py` 绿**：45/45（含正例+负例矩阵，2026-06-23 实跑）
- [ ] **rsync stale source 告警**：跨机数据面（§3.3 Alienware rsync），M2 hub 单边无远端 rsync 源——移交 P2-tail
- [ ] **Alienware 经 tailnet 真跨写可见**：依赖第二台机 + tailscale serve + window CC——移交 P2-tail

---

## 实测结论

### /mcp 写隔离（DB source_id 证据）
`/mcp put_page` 的 `source_id` 字段由 **token 绑定**，不接受客户端传参覆盖（serve 端 SERVER-STAMPED）。实测：hub-cc token 写入的页面 `source_id=ubuntu-cc`；hub-bridge token 写入的页面 `source_id=ubuntu-bridge`；用 hub-cc token 尝试写 `source_id=ubuntu-bridge` → 服务端忽略客户端值，实际落库仍为 `ubuntu-cc`。`test_m2_oauth_scoping::test_mcp_write_source_is_token_bound_not_overridable` 验证（DB 直查 source_id）。

### /ingest 在 M2 部署态结论
`/ingest` 是 **header-trust** 写入路径（接受 `X-GBrain-Source` header 直接指定 source，无 OAuth 验证）。M2 部署态结论：**inert（入队但不执行），而非已证明安全**。
- M2 不运行 jobs worker → 入队后页永不落库
- `/ingest` 未接任何客户端（三端 adapter 全走 `/mcp`）
- tailscale serve 路径白名单不暴露 `/ingest`（远端无法触达）
- `test_m2_oauth_scoping::test_ingest_inert_without_jobs_worker_in_m2`：POST /ingest 返回入队状态；DB 计数不增

**缓解 = 三重 "不"**（不跑 worker + 不接 client + tailnet 不暴露），**非已证明安全路径**。引入 jobs worker / 暴露 /ingest 时须重新评估。

### token 生命周期（方案 C：大 TTL + slow rotation）
选定方案 C：`token_ttl=30d`（registration `--token-ttl 2592000`）。
- 实测：`expires_in=2592000`（token exchange 响应，test_m2_mcp_pull::test_prod_clients_have_30d_ttl）
- **expiry→remint recovery 实测通**（test_m2_mcp_pull::test_token_expiry_then_remint_recovers）：注入 shortlived（TTL=2s）client，等待过期，确认 401，重新换 token，新 token 写/读成功
- **方案 A（discovery）**：gbrain 无 `expires_at` 字段（`gbrain auth list` 无时间戳）→ 难实现
- **方案 B（wrapper）**：connect-cc.sh 是同等效果的 re-mint wrapper，`gbrain-token-refresh.timer` 每日运行 = slow rotation
- **生产实务**：30d TTL + 每日轮转（timer）→ 在线 token 始终距过期 29-30d，会话内不会 401

### 宿主 hook 契约真实格式（三端差异）
- **CC**：`~/.claude/settings.json` `hooks.SessionStart` → stdout JSON `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}`
- **Codex**：`~/.codex/AGENTS.md` prepend `<!-- gbrain-digest:begin -->..<!-- gbrain-digest:end -->` 块；`memories/` 目录不被自动加载（实测）——必须写 AGENTS.md
- **OpenClaw**：无内置 hook 系统（`openclaw.json` 无 hooks 字段）；写入 agent 专属 `~/.openclaw/agents/<name>/agent/codex-home/AGENTS.md`；静态注入，无自动刷新触发器——须手动/定时运行 adapter

### export 镜像往返限制
`gbrain export` → Markdown 文件；`gbrain import` 重载。**已知限制**：`gbrain import` 不保留 source_id 分区（所有导入页落为 import_default 或 null source）。restore smoke 验证内容+计数级往返，不验证 source_id 还原。R4 兜底级恢复：有内容/无精确 source 分区，接受。

### 阈值现状
`config/m2-thresholds.json` status=`uncalibrated_default`：标定集 30 条但 positive_labels=0（M2 seed corpus 无标注正例）→ 退保守默认 0.75（宁漏注勿污染）。P4 内容灌入后重跑 `calibrate_threshold.py`。

---

## 🔑 激活清单（DEFERRED — P4 brain 有内容后执行）

> 以下 5 步全部 DEFERRED。**当前一键激活零步骤**——hub 已在跑，hook/MCP/tailnet 激活有意推后到 P4（brain 有内容才有注入价值）。

### ① CC SessionStart hook 接线

```bash
# 编辑 ~/.claude/settings.json，在 hooks 段添加：
# {
#   "hooks": {
#     "SessionStart": [{
#       "matcher": "**",
#       "hooks": [{
#         "type": "command",
#         "command": "bash /home/ivan/projects/sharedmemory/hooks/cc_sessionstart.sh"
#       }]
#     }]
#   }
# }
# 验证：
bash /home/ivan/projects/sharedmemory/hooks/cc_sessionstart.sh CLAUDE_PROJECT_DIR=$HOME/projects/sharedmemory
# 应输出 JSON 含 [[slug]]
```

**回滚**：从 `~/.claude/settings.json` 删除 SessionStart hook 条目。

---

### ② Codex/OpenClaw AGENTS.md 接线 + refresh 计划

```bash
# Codex（手动或在 Codex 启动前执行）：
bash /home/ivan/projects/sharedmemory/hooks/codex_sessionstart.sh
# → 写 ~/.codex/AGENTS.md 头部，.gbrain-digest.bak 存原始备份

# OpenClaw（对目标 agent 执行，OPENCLAW_AGENT 默认 main）：
OPENCLAW_AGENT=main bash /home/ivan/projects/sharedmemory/hooks/openclaw_bootstrap.sh
# → 写 ~/.openclaw/agents/main/agent/codex-home/AGENTS.md 头部

# 验证：
grep -c "gbrain-digest:begin" ~/.codex/AGENTS.md       # → 1
grep -c "gbrain-digest:begin" ~/.openclaw/agents/main/agent/codex-home/AGENTS.md  # → 1
```

**refresh 计划建议**：OpenClaw 无内置 hook，可加 Inngest cron（`TZ=Asia/Shanghai 0 8 * * *`）或手动在新项目开工前跑。

**回滚**：删 AGENTS.md 中的 `<!-- gbrain-digest:begin -->...<!-- gbrain-digest:end -->` 块；`.gbrain-digest.bak` 含原始内容。

---

### ③ 会话中 MCP pull/写 + token-refresh timer

```bash
# 一次性 MCP 接线（在 Ivan 终端执行）：
bash /home/ivan/projects/sharedmemory/infra/gbrain/mcp/connect-cc.sh
# → re-mint hub-cc token → 写 ~/.config/gbrain/hub-cc.token → gbrain connect --install

# 验证 MCP 接线：
cat ~/.claude/.mcp.json | python3 -m json.tool | grep gbrain   # → 含 gbrain-mcp 条目

# 安装 token 每日轮转 timer：
cp /home/ivan/projects/sharedmemory/infra/gbrain/mcp/gbrain-token-refresh.{service,timer} \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gbrain-token-refresh.timer

# 验证 timer：
systemctl --user list-timers | grep gbrain   # → 含 gbrain-token-refresh.timer
```

**回滚（MCP）**：从 `~/.claude/.mcp.json` 删除 gbrain-mcp 条目；`rm ~/.config/gbrain/hub-cc.token`。
**回滚（timer）**：`systemctl --user disable --now gbrain-token-refresh.timer && rm ~/.config/systemd/user/gbrain-token-refresh.{service,timer}`。

---

### ④ tailscale serve（tailnet 暴露）

**必须 Ivan 在真终端 sudo 跑**（CC 不可代跑，MEMORY.md tailscale serve 硬坑）：

```bash
# 暴露安全路径（绝不暴露 /ingest / /admin）：
sudo tailscale serve --bg --https=443 --set-path /mcp       http://127.0.0.1:7777/mcp
sudo tailscale serve --bg --https=443 --set-path /token     http://127.0.0.1:7777/token
sudo tailscale serve --bg --https=443 --set-path /health    http://127.0.0.1:7777/health
sudo tailscale serve --bg --https=443 --set-path /.well-known http://127.0.0.1:7777/.well-known

# 验证：
tailscale serve status
curl https://<hostname>.tail567e5a.ts.net/health   # 从 Alienware 跑
```

**回滚**：`sudo tailscale serve reset`

详见 `infra/gbrain/mcp/tailnet-serve.md`（路径白名单 + 远程 client 注册 + 公网 URL 注意事项）。

---

### ⑤ brain repo push 到 GitHub

```bash
cd ~/projects/brain
# 首次创建 remote（如未创建）：
gh repo create ivanfuland/brain --private --source=. --push

# 后续 sync（export 最新 + commit + push）：
bash /home/ivan/projects/sharedmemory/infra/backup/backup-brain.sh

# 验证：
gh repo view ivanfuland/brain   # → 可见私有 repo
```

---

## P2-tail 收尾小步（需 window CC + Alienware）

以下两项是 P2 验收的剩余部分，M2 hub 单边无法单独完成：

1. **Alienware 经 tailnet 真跨写并在 hub 可见**
   - 在 Alienware 注册 `alien-codex` client（hub 跑 `gbrain auth register-client alien-codex ...`）
   - Alienware Codex 换 token（打 `https://<hostname>.ts.net/token`）
   - 写一个 page（`source=alien-codex`）→ 打 `https://<hostname>.ts.net/mcp` put_page
   - Hub CC 读联邦可见（`source_id=alien-codex`，federated-read ALL）
   - 详细步骤见 `infra/gbrain/mcp/tailnet-serve.md §M2 收尾：Alienware 跨写测`

2. **rsync stale source 告警（§3.3 数据面）**
   - 从 Alienware 向 hub brain repo 执行 rsync
   - 注入一个 stale-flagged 页面
   - 验证 SessionStart digest 注入 `(stale：有新证据待整编)` 标记
   - 详见 spec §3.3 + hooks/gbrain_digest.py `_is_stale` 分支

---

## 服务起停 / 回滚

```bash
# gbrain-mcp service
systemctl --user start gbrain-mcp.service
systemctl --user stop gbrain-mcp.service
systemctl --user restart gbrain-mcp.service
systemctl --user status gbrain-mcp.service

# pg-memory
cd ~/projects/sharedmemory/infra/pg-memory
docker compose up -d      # 起
docker compose down       # 停（保留卷）
docker compose down -v    # 回滚（删卷 — 所有 gbrain 向量数据丢失）

# 完整回滚（gbrain serve + Postgres）
systemctl --user disable --now gbrain-mcp.service
rm ~/.config/systemd/user/gbrain-mcp.service && systemctl --user daemon-reload
cd ~/projects/sharedmemory/infra/pg-memory && docker compose down -v
```

---

## 复现命令

```bash
cd ~/projects/sharedmemory && export PATH="$HOME/.bun/bin:$PATH"

# 环境变量
set -a
source infra/gbrain/config.env
source infra/pg-memory/.env
set +a

# 确认 hub 服务在线
curl -s http://127.0.0.1:7777/health | python3 -m json.tool

# 全套 M2 测试（45 测）
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" uv run pytest tests/test_m2_*.py -v

# 一致性正例单跑
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" uv run pytest tests/test_m2_consistency.py -v

# 手动三端 adapter 探针（不碰宿主文件）
TMP_CODEX=$(mktemp /tmp/test-codex-XXXXX.md) && TMP_OC=$(mktemp /tmp/test-oc-XXXXX.md)
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" CLAUDE_PROJECT_DIR="$PWD" bash hooks/cc_sessionstart.sh
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" CODEX_PROJECT_DIR="$PWD" CODEX_AGENTS_FILE="$TMP_CODEX" bash hooks/codex_sessionstart.sh
GBRAIN_HOME="$PWD/sandbox/gbrain-pg" OPENCLAW_WORKSPACE="sharedmemory" OPENCLAW_AGENTS_FILE="$TMP_OC" bash hooks/openclaw_bootstrap.sh
rm -f "$TMP_CODEX" "$TMP_OC"
```

---

## 对 M3 的修正（M2 实测发现）

1. **蒸馏桥写路径**：用 `hub-bridge` scoped client（`source=distill-bridge`），经 `/mcp put_page`（OAuth scoped，不走 `/ingest` header-trust）。token 换取打 `http://127.0.0.1:7777/token`；写工具名 `put_page`，参数 `{slug, content}`；MCP endpoint `http://127.0.0.1:7777/mcp`（会话中 Bearer header）。

2. **token 刷新接法**：`bash infra/gbrain/mcp/token-refresh.sh HUB_BRIDGE` → atomic write to `~/.config/gbrain/hub-bridge.token`；蒸馏桥每次启动读此 token 或按需 re-mint。

3. **timeline-add 写法**：MCP tool `timeline_add`，参数 `{slug, date, entry}`；不走 `/ingest`（inert in M2，不走 header-trust 路径）。

4. **/ingest 在蒸馏桥中的位置**：M2 已确认 /ingest inert（无 jobs worker），M3 引入 jobs worker 时须重新评估 header-trust 越权面——建议 M3 继续不走 /ingest，全走 OAuth `/mcp`。

5. **阈值 re-cal 时机**：P4 内容灌入后，先跑 `calibrate_threshold.py` 更新 `config/m2-thresholds.json`，再激活 CC hook。避免 0.75 默认阈值在真实 brain 下误滤有效结论（当前 brain 实测得分 0.84-0.93，已高于 0.75）。

6. **GBRAIN_DIGEST_THRESHOLD env override**：`hooks/gbrain_digest.py` 已支持（优先级：env > config JSON > 默认）。CI / 阈值调试场景可设 0.0 确保种子页必中；生产不设（走 config JSON 0.75）。

---

## gbrain 版本锁

`gbrain@0.42.37`（`scripts/gbrain-version.txt`）。跨 Task 所有实测基于此版本。升级前须重跑全套 `test_m2_*.py`。
