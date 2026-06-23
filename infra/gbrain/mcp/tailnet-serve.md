# tailnet-serve.md — Tailnet Exposure via `tailscale serve`

## 概述

将 gbrain `127.0.0.1:7777` 通过 Tailscale HTTPS 暴露给 Alienware 等远程客户端。
**只暴露安全路径，绝不暴露 `/ingest` 和 `/admin`（缓解 write-scope 越权面）。**

## ★ 必须 Ivan 在真终端 sudo 跑（CC 不能代跑）

`tailscale serve` 需要 sudo/root 或 tailscale operator 权限。
MEMORY.md 硬坑：`tailscale set --operator=ivan` 对非登录会话（如 CC 的 Bash 工具）不生效，
CC 跑 serve 永远 `Access denied`。

```
# Ivan 在终端跑（非 CC 执行）：
sudo tailscale serve --bg --https=443 --set-path /mcp       http://127.0.0.1:7777/mcp
sudo tailscale serve --bg --https=443 --set-path /token     http://127.0.0.1:7777/token
sudo tailscale serve --bg --https=443 --set-path /health    http://127.0.0.1:7777/health
sudo tailscale serve --bg --https=443 --set-path /.well-known http://127.0.0.1:7777/.well-known
```

- `--bg`：后台持久运行（看到 "Serve started and running in the background" = 成功）。
  不加 `--bg` 会显示 "Press Ctrl+C to exit"，退出即清规则。
- 被代理的服务已绑 `127.0.0.1:7777`（Task2 保证），不会与 tailscale 网卡冲突。
- **不暴露 `/ingest`、`/admin`**（远程 write-scoped client 够不到 header-trust 的 /ingest 越权面）。

## 路径白名单说明

| 路径 | 暴露 | 理由 |
|------|------|------|
| `/mcp` | ✅ | OAuth scoped MCP，远程 agent pull/write 入口 |
| `/token` | ✅ | OAuth token endpoint，远程 client 换 token |
| `/health` | ✅ | 无状态 probe |
| `/.well-known/` | ✅ | OAuth metadata discovery（`/token` 的配套） |
| `/ingest` | ❌ | header-trust 写入，不对远程暴露 |
| `/admin` | ❌ | 管理端点，不对远程暴露 |

## 验证暴露状态

```bash
# 查看当前 serve 规则
tailscale serve status

# 从远端机器（Alienware）验证
curl https://<hostname>.tail567e5a.ts.net/health
curl https://<hostname>.tail567e5a.ts.net/mcp -H "Authorization: Bearer <tok>"
```

## 远程 client 注册（在 hub 跑）

为 Alienware 的 Codex 注册一个 federated client（在 Ubuntu hub 上执行）：

```bash
export PATH="$HOME/.bun/bin:$PATH"
export GBRAIN_HOME="$PWD/sandbox/gbrain-pg"

gbrain auth register-client alien-codex \
  --grant-types client_credentials \
  --scopes "read write" \
  --source alien-codex \
  --federated-read ALL
```

凭证（client_id / client_secret）写入 `infra/gbrain/clients.env`（格式同现有条目）。
把凭证安全传给 Alienware（不进 git，用 `scp` 或 1Password）。

## `--public-url` 注意

如果 OAuth metadata（`/.well-known/openid-configuration`）的 token_endpoint
对远端不可达，需在 serve 启动时传入 public URL：

```bash
sudo tailscale serve --bg --https=443 \
  --public-url "https://<hostname>.tail567e5a.ts.net" \
  --set-path /token http://127.0.0.1:7777/token
```

让 token_endpoint 在 metadata 里输出为 `https://...ts.net/token` 而不是 `http://127.0.0.1:7777/token`。

## 远端负例验证（无凭证访问被拒）

```bash
# 远端无 token 访问 /mcp → 401
curl -s -o /dev/null -w "%{http_code}" https://<hostname>.tail567e5a.ts.net/mcp

# 远端无凭证访问 /ingest → 404 或连接拒绝（未暴露）
curl -s -o /dev/null -w "%{http_code}" https://<hostname>.tail567e5a.ts.net/ingest
```

## 扩展接线（codex / openclaw）

待各端 connect 脚本完成后，在 `gbrain-token-refresh.service` 追加同形 `ExecStart`：

```ini
# codex
ExecStart=%h/projects/sharedmemory/infra/gbrain/mcp/connect-codex.sh
# openclaw
ExecStart=%h/projects/sharedmemory/infra/gbrain/mcp/connect-openclaw.sh
```

## M2 收尾：Alienware 跨写测

1. Alienware Codex 用 alien-codex 凭证换 token（打 `https://...ts.net/token`）
2. 写一个 page（`source=alien-codex`）→ 打 `https://...ts.net/mcp` put_page
3. Hub CC 读联邦可见（`source_id=alien-codex`，federated-read ALL）→ get_page 验通

本 Task 只做 hub 单边 ready + 文档 + 负例；真机 Alienware 跨写测 = M2 收尾小步。

## 停用

```bash
# 清除所有 serve 规则
sudo tailscale serve reset
```
