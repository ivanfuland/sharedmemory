# Infinity 推理服务（CASS 语义栈）

bge-m3（embedding）+ bge-reranker-v2-m3（rerank），GPU，systemd user service 常驻。

## 服务

- Unit 模板：`infra/systemd/cc-infinity.service`（install 到 `~/.config/systemd/user/`）
- 容器：`cc-infinity`，`docker run --rm`（前台，systemd 追 docker client 进程）
- 端口：**127.0.0.1:7997**（仅本机环回；内部服务不暴露 LAN）
- 镜像 pin：`michaelf34/infinity@sha256:11e8b3921b9f1a58965afaad4a844c435c9807cbc82c51e47cb147b7d977fc88`
  （避免 `latest` 漂移；升级流程 = 重 pull → 取新 RepoDigests → 改 unit digest → restart → 跑召回门）
- 模型卷：docker named volume `cc-infinity-hf` → `/app/.cache/huggingface`（HF 缓存，免重下模型）

## 安装

```bash
docker rm -f cc-infinity 2>/dev/null || true
mkdir -p ~/.config/systemd/user
cp ~/projects/sharedmemory/infra/systemd/cc-infinity.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now cc-infinity.service
loginctl enable-linger "$USER"   # 无登录会话也常驻（开机自启）
```

## 运维

```bash
systemctl --user status cc-infinity.service
journalctl --user -u cc-infinity.service -f      # 跟日志
curl -s http://127.0.0.1:7997/health             # 健康
curl -s http://127.0.0.1:7997/models | python3 -m json.tool   # 已载模型
systemctl --user restart cc-infinity.service     # 重启（容器随之重建）
```

## 设计要点（codex R1 审）

- **不依赖 docker.service**：user systemd manager 看不到 system-level unit，`After=docker.service` 无效 →
  改 `ExecStartPre=/usr/bin/docker info` 探 daemon 可达。
- **`--rm` 前台模式**：systemd Type=simple 直接监督 docker client 进程，client 随容器存活；
  `ExecStop=docker stop` 优雅停；`ExecStartPre=-docker rm -f` 清理残留（`-` 忽略失败）。
- **digest pin**：codex R1 P1，防 `latest` 静默漂移破坏召回基线。
