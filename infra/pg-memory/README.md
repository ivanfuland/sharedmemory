# pg-memory（GBrain Postgres 后端，M1 / spec §12.8）

- 镜像：**stock `pgvector/pgvector:pg17`**（无自建 Dockerfile，无 zhparser）
  - RepoDigest（锁版本）：`pgvector/pgvector@sha256:be400b50812ab2cc908ed78593fda2e51e3b45fe774fa637f1c7b16e68531d95`
- 端口：`127.0.0.1:5433`（仅 localhost）；库 `gbrain`，用户 `gbrain`（superuser）
- GBrain 只需扩展 `vector` + `pg_trgm` + `pgcrypto`（gbrain init 自动 CREATE EXTENSION）
- 密码在 `.env`（600，gitignore）

## 起停 / 回滚
```bash
docker compose up -d        # 起
docker compose down         # 停（留卷）
docker compose down -v      # 回滚（删容器 + 卷；现有 postgres:16 业务库不受影响）
```

> 注：本机 docker 若配了挂掉的 registry-mirror（如 docker.1ms.run 报 unknown blob），拉镜像会失败；
> 去掉 mirror（`/etc/docker/daemon.json` 置 `{}` + `systemctl restart docker`）走直连即可。
