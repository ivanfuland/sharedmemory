# cass-mcp 部署 + 注册（M4 Phase B 读侧 connector）

把 CASS 语义检索暴露成 8 个 MCP 工具：`cass_search`(语义+rerank) / `cass_expand` / `cass_context` / `cass_export` / `cass_triage` / `cass_pack`(lexical,agent-handoff包) / `cass_sessions`(列最近会话) / `cass_timeline`(时间段活动)。

**前置**：PR #21 已合并到 master（`cass_mcp/` 在 `~/projects/sharedmemory`）；`cc-infinity.service` active；canonical 库就绪。

## 1. 本机部署（systemd user service）

```bash
cd ~/projects/sharedmemory
bash infra/cass-mcp/deploy.sh
```
幂等：生成 `cass-mcp.env`（随机 bearer，gitignored）→ 装 `cass-mcp.service`（127.0.0.1:7788 + enable + linger 自启）→ smoke 验 401。打印出**注册三端用的 bearer**。

运维：`systemctl --user {status,restart,stop} cass-mcp.service`；日志 `journalctl --user -u cass-mcp.service`；访问审计 `infra/cass-mcp/cass_audit.log`（query 已脱敏为 sha12+len）。

## 2. 外网暴露（tailscale serve，需真终端 sudo）

本机的 CC/Codex/OpenClaw 走 `http://127.0.0.1:7788/mcp` 即可，**无需**这步。
仅当 Alienware/Mac 等**远程机**要查这台的记忆时做。

⚠ `tailscale serve` 必须在**真终端 sudo** 跑（CC 的 Bash 会话 operator 不生效）；被代理服务已绑 127.0.0.1（满足要求）；用 `--bg` 才持久：

```bash
sudo tailscale serve --bg --https=7788 127.0.0.1:7788
```
验证：`tailscale serve status` 应见 `https://ivancomputer.<tailnet>.ts.net:7788 → 127.0.0.1:7788`。
远程机注册时 URL 用 `https://ivancomputer.<tailnet>.ts.net:7788/mcp` + 同一 bearer。

## 3. 注册三端（非侵入，标准 MCP 扩展）

bearer = deploy.sh 输出值。本机 URL `http://127.0.0.1:7788/mcp`。

- **Claude Code**：`.mcp.json` 加 `cass-mcp` server（http transport + `Authorization: Bearer <bearer>`）。
- **Codex**：codex mcp 配置加同一 server。
- **OpenClaw**：`openclaw config set`（禁直接 Edit openclaw.json）加 MCP server 条目。

各端注册后真调一次 `cass_search` 验通，记入 M4-EXIT。

## 设计约束（勿违背）
- 绑 127.0.0.1；外网只经 tailscale serve（bearer 是唯一闸，缺/错 token → 401）。
- `cass_search` 走语义（`--mode semantic --daemon --model bge-m3 --rerank`）+ 查询前三就绪校验。
- `cass_pack` 仅 lexical/hybrid（CASS 上游未支持 pack 语义）——概念召回用 `cass_search`。
- 升级 cass fork 后无需动 cass-mcp（契约 `contracts/cass-semantic-prod.md` 稳定）。
