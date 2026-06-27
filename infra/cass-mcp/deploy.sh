#!/usr/bin/env bash
# cass-mcp 部署（幂等）。合并后从 canonical checkout 跑：bash infra/cass-mcp/deploy.sh
# 装 systemd user service（127.0.0.1:7788 常驻 + 自启 + 自愈），smoke 验 401/启动。
# 外网暴露（tailscale serve）需真终端 sudo，见 README，本脚本不做。
set -euo pipefail

REPO="$HOME/projects/sharedmemory"
ENVF="$REPO/infra/cass-mcp/cass-mcp.env"
EXAMPLE="$REPO/infra/cass-mcp/cass-mcp.env.example"
UNIT_SRC="$REPO/infra/cass-mcp/cass-mcp.service"
UNIT_DST="$HOME/.config/systemd/user/cass-mcp.service"

# 0. 前置：cass_mcp 必须在 canonical checkout（合并后才有）
[ -f "$REPO/cass_mcp/server.py" ] || { echo "✗ $REPO/cass_mcp/server.py 不存在——先合并 PR #21 再部署"; exit 1; }

# 1. env：缺则从模板生成 + 灌随机 bearer（幂等：已存在不覆盖，保留现有 bearer）
if [ ! -f "$ENVF" ]; then
  sed "s|__REPLACE_WITH_SECRET__|$(openssl rand -hex 32)|" "$EXAMPLE" > "$ENVF"
  chmod 600 "$ENVF"
  echo "✓ 生成 $ENVF（随机 bearer）"
else
  echo "• $ENVF 已存在，保留现有 bearer"
fi

# 1b. bearer 校验（新建/已存在两分支都做：placeholder/空/弱 → 报错退出，保护唯一鉴权闸）
BEARER=$(grep '^CASS_MCP_BEARER=' "$ENVF" | cut -d= -f2-)
if ! [[ "$BEARER" =~ ^[0-9a-f]{64}$ ]]; then
  echo "✗ $ENVF 的 CASS_MCP_BEARER 非法（应为 openssl rand -hex 32 的 64 位 hex；当前疑似 placeholder/空/弱）。"
  echo "  修复：在 $ENVF 填 CASS_MCP_BEARER=\$(openssl rand -hex 32) 后重跑。"
  exit 1
fi
chmod 600 "$ENVF"   # 已存在时也确保权限收紧（防权限松）

# 2. 装 + 起 user service
mkdir -p "$(dirname "$UNIT_DST")"
cp "$UNIT_SRC" "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable --now cass-mcp.service
loginctl enable-linger "$USER" >/dev/null 2>&1 || true   # 重启/无登录也自启

# 3. smoke：等监听 → 无 token 应 401
for i in $(seq 1 30); do ss -tlnp 2>/dev/null | grep -q ':7788' && break; sleep 0.5; done
CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7788/mcp -X POST -H 'Content-Type: application/json' -d '{}' || echo 000)
echo "no-token HTTP $CODE（期望 401）"
[[ "$CODE" == "401" ]] || { echo "✗ smoke 失败：期望 401 得到 $CODE。查 journalctl --user -u cass-mcp.service"; exit 1; }
systemctl --user --no-pager status cass-mcp.service | sed -n '1,4p'
echo ""
echo "✓ cass-mcp 部署完成（127.0.0.1:7788）"
echo "  注册三端 MCP 用的 bearer： $BEARER"
echo "  下一步：① 外网 tailscale serve（见 README，需 sudo）② 三端注册（见 README）"
