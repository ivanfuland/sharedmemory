# gbrain-mcp systemd user service (Task 2)

GBrain HTTP MCP server 常驻服务，绑 `127.0.0.1:7777`，systemd --user 自启自愈。

> **tailnet 暴露在 Task 7**：当前仅绑 127.0.0.1，不对外。Task 7 用 `tailscale serve` 将 `:7777` 转发到 tailnet（见 MEMORY.md tailscale serve 硬坑：服务必须绑 127.0.0.1 而非 0.0.0.0，否则 tailscale 会抢端口报 SSL wrong version）。

## 初次安装

```bash
chmod +x infra/gbrain/serve/install-service.sh
infra/gbrain/serve/install-service.sh
```

脚本做：
1. bash-source `config.env` + `pg-memory/.env` → 生成 `infra/gbrain/serve/env.generated`（600，gitignored）
2. 复制 unit 到 `~/.config/systemd/user/gbrain-mcp.service` + daemon-reload + enable + start
3. 等 `/health` 就绪
4. 经服务进程 /mcp 验嵌入路径（query 工具，token 来自 Task1 注册的 hub-cc client）

> **env.generated 为何必要**：systemd `EnvironmentFile` 不做 shell source 语义，行内 `# comment` 会被吃进值（`OPENROUTER_BASE_URL=https://...  # 注释` → 值含注释文本）。脚本先 bash-source 再 re-emit 干净 `KEY=value`，规避此坑。

## 日常运维

```bash
# 状态
systemctl --user status gbrain-mcp

# 启/停/重启
systemctl --user start gbrain-mcp
systemctl --user stop gbrain-mcp
systemctl --user restart gbrain-mcp

# 实时日志
journalctl --user -u gbrain-mcp -f

# 最近 50 行日志（故障排查）
journalctl --user -u gbrain-mcp -n 50
```

## 验证

```bash
# /health
curl http://127.0.0.1:7777/health

# 确认绑 127.0.0.1 非 0.0.0.0
ss -tlnp | grep :7777

# 跑存活测试
cd /home/ivan/projects/sharedmemory
uv run pytest tests/test_m2_service.py -v
```

## 回滚

```bash
systemctl --user disable --now gbrain-mcp
rm ~/.config/systemd/user/gbrain-mcp.service
systemctl --user daemon-reload
# 可选：删 env.generated
rm infra/gbrain/serve/env.generated
```

## 重新生成 env.generated

config.env / pg-memory/.env 变更后重跑安装脚本即可（幂等）：

```bash
infra/gbrain/serve/install-service.sh
```

## 端口约定

| 端口 | 绑定 | 用途 |
|------|------|------|
| 7777 | 127.0.0.1 | gbrain MCP HTTP (本机) |
| 7777 | tailnet（Task 7） | tailscale serve 暴露给 tailnet |
