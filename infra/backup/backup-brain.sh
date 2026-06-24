#!/usr/bin/env bash
# §11.3 备份脚本：brain repo（export mirror + R4兜底）+ GBRAIN_HOME state + canonical sqlite + Postgres dump。
# 定位：M2 阶段 brain repo = Postgres 的 export mirror（非 write-through 真理源；write-through 随 P4 成熟）。
# 不删源（rsync --archive，无 --delete）；目标默认 NAS，可通过 BRAIN_BACKUP_DEST 环境变量覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${BRAIN_BACKUP_DEST:-$HOME/nas/openclaw/brain-backup}"
mkdir -p "$DEST"

export PATH="$HOME/.bun/bin:$PATH"
export GBRAIN_HOME="$ROOT/sandbox/gbrain-pg"
set -a
source "$ROOT/infra/gbrain/config.env"
source "$ROOT/infra/pg-memory/.env"
set +a

STAMP="$(date +%Y%m%d)"

echo "[backup] dest=$DEST stamp=$STAMP"

# 1) brain repo markdown（M2=export mirror + R4 兜底；P4 起渐成 write-through 真理源）
if [ -d "$HOME/projects/brain" ]; then
    # 先 export 最新快照到 brain repo
    gbrain export --dir "$HOME/projects/brain" 2>&1 | tail -3
    # 再 rsync 到备份目标
    rsync -a "$HOME/projects/brain/" "$DEST/brain-repo/"
    echo "[backup] brain-repo → $DEST/brain-repo/"
else
    echo "[backup] WARN: ~/projects/brain 不存在，跳过 brain repo 备份"
fi

# 2) GBRAIN_HOME state（config.json、auth 等配置）
rsync -a "$GBRAIN_HOME/.gbrain/" "$DEST/gbrain-home/" 2>/dev/null || \
    echo "[backup] WARN: gbrain-home state rsync 失败（非致命）"
echo "[backup] gbrain-home → $DEST/gbrain-home/"

# 3) canonical sqlite（CASS 读端）+ raw mirror——路径以实际 $CASS_CANON_DB 为准
CANON="${CASS_CANON_DB:-$HOME/.local/share/coding-agent-search/agent_search.db}"
if [ -f "$CANON" ]; then
    cp "$CANON" "$DEST/canonical-$STAMP.db"
    echo "[backup] canonical db → $DEST/canonical-$STAMP.db"
else
    echo "[backup] WARN: canonical db 不存在（路径=$CANON）"
fi

# 4) Postgres 派生库 dump（可重建，但备一份省 re-embed；restore 整库靠此 dump，非 gbrain import）
# 注：gbrain import 不保 source_id 分区（EXIT 限制），完整 source 分区恢复必须用此 pg dump。
if docker exec pg-memory pg_dump -U gbrain gbrain > "$DEST/gbrain-pg-$STAMP.sql" 2>/dev/null; then
    echo "[backup] pg dump → $DEST/gbrain-pg-$STAMP.sql"
else
    echo "[backup] WARN: pg dump 失败（pg-memory 未运行？）"
fi

# §11.3：蒸馏桥状态库（cursor/raw_work_item/journal）——hub 唯一副本，必备份
BRIDGE_STATE="${BRIDGE_STATE_DB:-$ROOT/infra/distill/bridge-state.db}"
BACKUP_DIR="$DEST"
if [ -f "$BRIDGE_STATE" ]; then
    sqlite3 "$BRIDGE_STATE" ".backup '$BACKUP_DIR/bridge-state.db'"
    echo "[backup] bridge state → $BACKUP_DIR/bridge-state.db"
    # restore smoke 验证 + 记录最近成功时间（§11.3 要求留档）
    if bash "$ROOT/infra/backup/restore-bridge-smoke.sh" "$BRIDGE_STATE" "$BACKUP_DIR/bridge-state.verify.db" 2>&1; then
        date -u +%FT%TZ > "$BACKUP_DIR/bridge-state.last-restore-ok"
        echo "[backup] bridge state restore smoke OK → last-restore-ok 已更新"
    else
        echo "[backup] WARN: bridge state restore smoke 失败（非致命，继续备份其余内容）"
    fi
else
    echo "[backup] INFO: bridge state db 不存在（路径=$BRIDGE_STATE），桥尚未首次运行，跳过"
fi

echo "[backup] 完成 → $DEST（brain-repo / gbrain-home / canonical / pg dump / bridge-state）"
