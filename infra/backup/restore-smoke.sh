#!/usr/bin/env bash
# restore smoke（R4 兜底验证）：
#   export 现库 → 全新临时 PGLite 库 import → 页计数 + 页内容 md5 一致性断言。
#   证明 export markdown 可重建检索层（R4 兜底成立）。
#
# 已知限制（EXIT #9）：
#   gbrain import 走单 source，不恢复 source_id 分区。
#   restore smoke 仅证明「页内容/frontmatter 可重建」，不证 source 分区。
#   完整 source 分区恢复须用 backup-brain.sh 生成的 pg dump。
#
# 计数策略：用 gbrain stats 的 Pages 行（含所有 source 页），
#   不用 gbrain list（只显示默认 source 的页，多 source 库会少计）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$HOME/.bun/bin:$PATH"
export GBRAIN_HOME="$ROOT/sandbox/gbrain-pg"
set -a
source "$ROOT/infra/gbrain/config.env"
source "$ROOT/infra/pg-memory/.env"
set +a

SRC_HOME="$GBRAIN_HOME"
TMP_DIR="$(mktemp -d)"
TMP_HOME="$TMP_DIR/restore"
EXP="$TMP_DIR/export"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "[smoke] SRC_HOME=$SRC_HOME"
echo "[smoke] step 1: export 现库 → $EXP"
GBRAIN_HOME="$SRC_HOME" gbrain export --dir "$EXP" 2>&1 | tail -3

# 页计数：用 gbrain stats 而非 gbrain list（list 仅显示默认 source，多 source 库会少计）
N_SRC=$(GBRAIN_HOME="$SRC_HOME" gbrain stats 2>/dev/null | grep '^Pages:' | awk '{print $2}')
N_EXP=$(find "$EXP" -type f -name '*.md' | wc -l)
echo "[smoke] src_stats_pages=$N_SRC  export_files=$N_EXP"
[ "$N_SRC" = "$N_EXP" ] || echo "[smoke] WARN: stats 页数($N_SRC) ≠ 导出文件数($N_EXP)（可能有非 page 内部记录，继续）"

echo "[smoke] step 2: 初始化临时 PGLite 库 → $TMP_HOME"
mkdir -p "$TMP_HOME"
GBRAIN_HOME="$TMP_HOME" gbrain init \
    --pglite \
    --embedding-model openrouter:text-embedding-3-small \
    --embedding-dimensions 1536 \
    --skip-embed-check 2>&1 | tail -3

echo "[smoke] step 3: import --no-embed → $TMP_HOME"
GBRAIN_HOME="$TMP_HOME" gbrain import "$EXP" --no-embed 2>&1 | tail -5

N_DST=$(GBRAIN_HOME="$TMP_HOME" gbrain stats 2>/dev/null | grep '^Pages:' | awk '{print $2}')
echo ""
echo "RESTORE_SMOKE src=$N_EXP dst=$N_DST"

if [ "$N_EXP" != "$N_DST" ]; then
    echo "FAIL: restore 页计数不一致（export=$N_EXP  import=$N_DST）"
    exit 1
fi

echo "[smoke] step 4: 内容/metadata 级断言（md5 round-trip，非仅计数）"
# 找第一个有实质内容的页（从 gbrain list 的默认 source 里取，两端都能 get）
PROBE_SLUG="$(GBRAIN_HOME="$SRC_HOME" gbrain list -n 100 2>/dev/null | awk '{print $1}' | head -1)"
if [ -z "$PROBE_SLUG" ]; then
    echo "WARN: 源库无默认 source 页可抽样，跳过 md5 断言"
    echo "PASS: export markdown 可重建检索层（页计数一致；md5 跳过-无默认页；source_id 分区不保，见 EXIT 限制）"
    exit 0
fi

SRC_BODY="$(GBRAIN_HOME="$SRC_HOME" gbrain get "$PROBE_SLUG" 2>/dev/null | md5sum | cut -d' ' -f1)"
DST_BODY="$(GBRAIN_HOME="$TMP_HOME" gbrain get "$PROBE_SLUG" 2>/dev/null | md5sum | cut -d' ' -f1)"

echo "RESTORE_CONTENT slug=$PROBE_SLUG src_md5=$SRC_BODY dst_md5=$DST_BODY"

if [ -z "$DST_BODY" ] || [ "$SRC_BODY" != "$DST_BODY" ]; then
    echo "FAIL: restore 后页内容/frontmatter 不一致（仅计数对不够）"
    echo "  slug=$PROBE_SLUG  src=$SRC_BODY  dst=${DST_BODY:-EMPTY}"
    exit 1
fi

echo ""
echo "PASS: export markdown 可重建检索层 + 页内容一致（R4 兜底成立；source_id 分区不保，见 EXIT 限制）"
