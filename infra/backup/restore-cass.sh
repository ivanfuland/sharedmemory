#!/usr/bin/env bash
# restore-cass.sh — CASS data_dir 灾难恢复（持锁 wrapper，设计契约见 spec §4.3）
# ~/projects/cc-workspace/docs/projects/shared-memory/specs/2026-07-09-cass-data-dir-backup-design.md
#
# 恢复一份 backup-cass.sh 产出的备份到一个**全新的** data-dir：校验产物 → 落 DB →
# 落 raw-mirror（blob 取共享池 / manifest 取本备份快照）→ 可选恢复会话源 → 重建 Tier 2
# （lexical 分钟级 + semantic ≈2h）→ doctor + search 验证。
#
# 为什么必须是持锁 wrapper（不是一串文档命令，spec §4.3）：
#   `flock -n 9 9>lock || exit 1` 这种写法在复合命令结束时立即释放锁，之后整个 restore
#   无锁保护。正解：`exec 9>lock` 在**同一 shell** 内持锁全程；且 `exec 9>` 的 fd 会被
#   子进程继承、bash 不设 O_CLOEXEC——**每个子进程调用都要 `9>&-`**，否则末尾
#   `systemctl --user start cass-mcp` 拉起的常驻服务会永久持锁，静默饿死每小时 index-pull。
#
# PUBLIC 仓纪律：本文件禁止出现真实密钥 / 基建拓扑，只放命令与判据。
#
# 用法：
#   infra/backup/restore-cass.sh --data-dir <全新目标目录> [--backup <cass-<stamp>|latest>]
#                                [--sessions-into <目录>|--sessions-into-source]
#                                [--rescan-history] [--skip-semantic] [--yes]
#   环境：CASS_BACKUP_DEST（默认 ~/nas/openclaw/backups/cass）· CASS_BIN · CASS_INFINITY_URL
#
# ⚠ 演练（V25 零生产改动）：--data-dir 指向临时目录、**不要** --sessions-into-source
#    （那会写回 ~/.claude/projects 等生产源）。真灾难恢复才指向 canonical + 源目录。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${CASS_BACKUP_DEST:-$HOME/nas/openclaw/backups/cass}"
CASS_BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
LOCK="$HOME/.local/share/.cass-write.lock"
# uv 在仓根跑（restore-from-mirror.py / blake3 preflight 依赖仓内 .venv，裸 python3 无 blake3）
UV_PY=(uv run python)

# 会话源根（同 backup-cass.sh 的 SESSION_ROOTS 三别名）
declare -A SESSION_ROOTS=(
  [claude-projects]="$HOME/.claude/projects"
  [codex-sessions]="$HOME/.codex/sessions"
  [openclaw-agents]="$HOME/.openclaw/agents"
)

# --- 参数 ---
TARGET=""            # 全新 data-dir（必填）
BACKUP="latest"      # cass-<stamp> 目录名，或 latest
SESSIONS_INTO=""     # 会话恢复目标前缀（空=跳过；--sessions-into-source 置为源根）
SESSIONS_TO_SOURCE=0
RESCAN=0             # 置 meta.last_scan_ts:* = 0，强制重扫历史
SKIP_SEMANTIC=0     # 只重建 lexical（跳过 ≈2h semantic，供快速冒烟）
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) TARGET="${2:?}"; shift 2 ;;
    --backup) BACKUP="${2:?}"; shift 2 ;;
    --sessions-into) SESSIONS_INTO="${2:?}"; shift 2 ;;
    --sessions-into-source) SESSIONS_TO_SOURCE=1; shift ;;
    --rescan-history) RESCAN=1; shift ;;
    --skip-semantic) SKIP_SEMANTIC=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    *) echo "[restore] FATAL: 未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$TARGET" ] || { echo "[restore] FATAL: 必须 --data-dir <全新目标目录>" >&2; exit 2; }

# --- canonical 目标 fail-closed guard（codex R10-[critical]）：拒绝 --data-dir 解析后 == live canonical
#     （cass-mcp 生产库）。否则 step3 后任何 FATAL 走 cleanup **重启 cass-mcp**、服务 env 仍指 canonical
#     → 读到**半恢复**生产库。设计强制 staging + swap（restore 到 <canonical>.new，验证全过后人工 swap，
#     spec V25 零生产改动）。**必须在停任何服务 / 抢锁之前**——纯 TARGET 校验，fail-fast 最安全。
#     用 realpath -m 归一化（TARGET 可能尚不存在），env 可覆盖 canonical 默认（同 backup-cass.sh）。 ---
CANONICAL="${CASS_CANONICAL_DIR:-$HOME/.local/share/coding-agent-search}"
if [ "$(realpath -m "$TARGET")" = "$(realpath -m "$CANONICAL")" ]; then
  echo "[restore] FATAL: --data-dir 不能等于 live canonical（$CANONICAL）——restore 必须落到 staging" >&2
  echo "         （如 <canonical>.new），验证全过后人工 swap；直接落 canonical 会让失败后 cleanup 重启的" >&2
  echo "         cass-mcp 读到半恢复库（spec V25 零生产改动）。" >&2
  exit 2
fi

# --- restore 自锁（fd 7，**独立于** .cass-write.lock）：防两个 restore 并发（codex R7）。
#     必须在**触碰任何 systemctl / .cass-write.lock 之前**抢——否则失败方会在 flock(.cass-write.lock)
#     失败退出时，由 cleanup 把 cass-mcp（CASS 写者）拉回来，破坏胜出方仍持锁恢复的隔离。
#     只有持自锁的这个进程才会往下走去 stop/start cass-mcp。 ---
exec 7>"$HOME/.local/share/.cass-restore.lock"
flock -n 7 || { echo "[restore] FATAL: 另一个 restore-cass.sh 正在运行（restore 自锁被持），拒绝并发" >&2; exit 1; }

# --- 解析备份目录 ---
if [ "$BACKUP" = "latest" ]; then
  # 只认含 COMPLETE 的 cass-*/（未完成/半成品不参与）；按名字排序取最新（stamp 单调）
  BK=""
  for d in "$DEST"/cass-*/; do
    [ -e "$d/COMPLETE" ] && BK="$d"
  done
  [ -n "$BK" ] || { echo "[restore] FATAL: $DEST 下无含 COMPLETE 的 cass-*/ 备份" >&2; exit 1; }
else
  BK="$DEST/$BACKUP"
fi
BK="${BK%/}"
echo "[restore] 备份源: $BK"
echo "[restore] 目标 data-dir: $TARGET"

# ---------------------------------------------------------------------------
# step 0a — 纯输入校验（**在停任何服务之前**！传错 --backup/--data-dir 是操作者错误，
#           不该把生产 cass-mcp 停下——codex 2026-07-12 抓出的服务留停 P0 的第一道防线）
# ---------------------------------------------------------------------------
# 参数级互斥 / 危险确认——**必须在停任何服务 / 复制任何东西之前**（codex R5：放 step4 会等到
# 停服务+抢锁+复制完 db 才失败，TARGET 已被污染，正确重跑会被下方非空门拒）。
if [ "$SESSIONS_TO_SOURCE" = "1" ] && [ -n "$SESSIONS_INTO" ]; then
  echo "[restore] FATAL: --sessions-into 与 --sessions-into-source 互斥，二选一" >&2; exit 1
fi
if [ "$SESSIONS_TO_SOURCE" = "1" ] && [ "$ASSUME_YES" != "1" ]; then
  echo "[restore] FATAL: --sessions-into-source 会写回**生产**会话源（~/.claude/projects 等 live 数据）。" >&2
  echo "         确认要写（仅在源 jsonl 也丢失时需要）→ 加 --yes；演练/常规请改 --sessions-into <临时目录>。" >&2
  exit 1
fi
# --sessions-into-source **只用于源全丢的空目录场景**（codex R8 fail-closed）：若目标源根已有 .jsonl，
# 中止——绝不对残留会话用 rsync（--append 会从错误 offset 拼坏；--ignore-existing 会静默跳过截断文件
# 并报成功、永久冻结坏会话）。有残留请用 --sessions-into <staging> 恢复到暂存区、人工核对后合入。
if [ "$SESSIONS_TO_SOURCE" = "1" ]; then
  for _alias in "${!SESSION_ROOTS[@]}"; do
    _root="${SESSION_ROOTS[$_alias]}"
    if [ -d "$_root" ] && [ -n "$(find "$_root" -type f -name '*.jsonl' -print -quit 2>/dev/null)" ]; then
      echo "[restore] FATAL: --sessions-into-source 只用于**源全丢的空目录**场景，但 $_root 已有 .jsonl。" >&2
      echo "         有残留会话：改用 --sessions-into <staging> 恢复到暂存区、人工核对后再合入（避免静默跳过/拼坏）。" >&2
      exit 1
    fi
  done
fi

[ -e "$BK/COMPLETE" ] || { echo "[restore] FATAL: 备份缺 COMPLETE marker: $BK" >&2; exit 1; }
{ [ -f "$BK/db" ] && [ -f "$BK/db.sha256" ] && [ -f "$BK/manifests.sha256sum" ] \
  && [ -d "$BK/manifests" ] && [ -f "$BK/digest.json" ]; } \
  || { echo "[restore] FATAL: 备份产物不全（需 db/db.sha256/manifests.sha256sum/manifests/digest.json）: $BK" >&2; exit 1; }
if [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "[restore] FATAL: 目标 $TARGET 非空——restore 必须落到全新目录（防误覆盖生产）" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# step 0b — preflight（缺一即停）：blake3 · 停 cass-mcp（装 cleanup trap 兜底重启）· 无 cass-infinity · 持写锁
# ---------------------------------------------------------------------------
echo "[restore] preflight …"

# blake3（在停服务前——纯依赖检查，失败不该留停服务）
( cd "$ROOT" && "${UV_PY[@]}" -c 'import blake3' ) 9>&- 8>&- 7>&- \
  || { echo "[restore] FATAL: uv run python 无 blake3（restore 硬依赖）" >&2; exit 1; }

# 记录进入脚本前 cass-mcp 是否 active（cleanup 据此决定要不要重启）
_MCP_WAS_ACTIVE=0
systemctl --user is-active --quiet cass-mcp && _MCP_WAS_ACTIVE=1
_MCP_SHOULD_RESTART=0   # 在 stop **之前**置位（见下）；cleanup 据此决定要不要拉起
_SHA_TMP=""             # step 2 的临时 hash 文件；cleanup 一并清

# ⚠ cleanup trap（codex 2026-07-12 抓出的 [critical]，R3+R4 两轮加固）：一旦准备停 cass-mcp，
#   **任何**退出路径（目标非空/checksum 不符/doctor 门未过/传错参数/另一写者抢锁/set -e 击杀/
#   停到一半被 SIGTERM…）都必须先释放写锁、再把 cass-mcp 拉回来——否则失败的 restore 把生产服务
#   留在 stopped、永不自动恢复。且 start 失败要**报非 0**（成功 restore 但服务没回来 ≠ 成功）。
cleanup() {
  local rc=$?
  [ -n "$_SHA_TMP" ] && rm -f "$_SHA_TMP" 2>/dev/null || true
  # **顺序关键**（codex R8）：重启 cass-mcp 期间**必须仍持有自锁 fd7**——否则第二个 restore 能在
  # 本 start/is-active 完成前抢到 fd7 进 preflight，坏交错下把刚拉起的服务又停掉、且它自己
  # _MCP_SHOULD_RESTART=0 不重启，服务被并发实例留停。故先重启、**最后**才释放 fd9/fd7。
  if [ "$_MCP_SHOULD_RESTART" = "1" ]; then
    # start 幂等。**`|| true` 不可省**——全局 set -e 下 start 自身非零会在 is-active/WARN 前打断
    # trap（服务留停+无提示+rc 被覆盖）。真正判据是随后的 is-active。systemctl 子命令带
    # `9>&- 8>&- 7>&-` 防 daemon 继承任何锁 fd。
    systemctl --user start cass-mcp 9>&- 8>&- 7>&- 2>/dev/null || true
    if ! systemctl --user is-active --quiet cass-mcp 9>&- 8>&- 7>&-; then
      echo "[restore] WARN: cleanup 未能确认 cass-mcp 已拉起，请手动 systemctl --user start cass-mcp" >&2
      [ "$rc" = "0" ] && rc=1   # 别把「服务没回来」谎报成 restore 成功
    fi
  fi
  # 服务处理完，现在才释放锁（**不加 `2>/dev/null`**——加在 exec 上会持久吞 stderr，codex harness 抓出）
  exec 9>&-
  exec 7>&-
  exit "$rc"
}
trap cleanup EXIT

# 停 cass-mcp（它是 CASS 写者）。**先**定重启意图再 stop——这样即便 stop 中途被 SIGTERM、
# 或 stop 非零退出但服务已实际停，cleanup 也会把它拉回来（start 幂等）。stop 非零仍 FATAL 退出，
# 但退出会走 cleanup、由它兜底重启。
[ "$_MCP_WAS_ACTIVE" = "1" ] && _MCP_SHOULD_RESTART=1
systemctl --user stop cass-mcp 9>&- 8>&- 7>&- || { echo "[restore] FATAL: 无法 stop cass-mcp" >&2; exit 1; }

# 无 cass-infinity 常驻（daemon 也是写者）——用 -x 精确匹配，避免自匹配假阳性
if pgrep -x cass-infinity >/dev/null 2>&1; then
  echo "[restore] FATAL: cass-infinity 仍在跑（daemon 是 CASS 写者），先停它" >&2; exit 1
fi

# 持写锁（全程同一 shell，fd 9 一直被持有）——抢不到 = 有别的 CASS 写者
exec 9>"$LOCK"
flock -n 9 || { echo "[restore] FATAL: 有 CASS 写者持 .cass-write.lock，restore 拒绝并发" >&2; exit 1; }
echo "[restore] preflight OK（持有写锁 fd 9）"

# ---------------------------------------------------------------------------
# step 1 — 空目录（非空即拒已在 0a 校验；此处建目录）
# ---------------------------------------------------------------------------
mkdir -p "$TARGET" 9>&- 8>&- 7>&-

# ---------------------------------------------------------------------------
# step 2 — 校验备份产物（双 sha256，dd iflag=direct 绕页缓存，**真查 PIPESTATUS**）
# ---------------------------------------------------------------------------
# 绕页缓存读回校验（spec §6.4）：dd iflag=direct 读真磁盘字节。**PIPESTATUS 必须在管道直接执行处读**——
# 绝不能在 `var=$(dd|sha256sum)` 里读（那是赋值命令的状态、非内部管道的，dd 读失败会漏过；
# codex 2026-07-12 抓出）。照 backup-cass.sh 模式：临时关 errexit+pipefail、管道输出到临时文件、
# 紧跟捕获 PIPESTATUS、恢复、逐段查 rc、再读 hash。
# fd 卫生（codex R6 硬约束，spec §4.3）：`exec 9>` 之后**每个**子进程都要 `9>&- 8>&- 7>&-`——包括
# **管道两侧**与命令替换里的子进程（不只左侧 dd）；否则父被 kill 时残留子进程仍持写锁，饿死 index-pull。
_SHA_TMP="$(mktemp)"
_verify_sha_direct() {  # $1=待校验文件 $2=期望 sha256（来自 sidecar）
  local f="$1" want="$2" got
  set +e +o pipefail
  dd if="$f" iflag=direct bs=4M 2>/dev/null 9>&- 8>&- 7>&- | sha256sum 9>&- 8>&- 7>&- > "$_SHA_TMP"
  local pipe=("${PIPESTATUS[@]}")
  set -e -o pipefail
  [ "${pipe[0]}" = "0" ] || { echo "[restore] FATAL: dd iflag=direct 读 $f 失败（rc=${pipe[0]}）" >&2; exit 1; }
  [ "${pipe[1]}" = "0" ] || { echo "[restore] FATAL: sha256sum $f 失败（rc=${pipe[1]}）" >&2; exit 1; }
  got="$(awk '{print $1}' "$_SHA_TMP" 9>&- 8>&- 7>&-)"
  [ "$got" = "$want" ] || { echo "[restore] FATAL: sha256 不符 $f（期望 $want 实得 $got）" >&2; exit 1; }
}

echo "[restore] step 2: 校验 db.sha256（dd iflag=direct）…"
DB_WANT="$(awk '{print $1}' "$BK/db.sha256" 9>&- 8>&- 7>&-)"
_verify_sha_direct "$BK/db" "$DB_WANT"

echo "[restore] step 2: 校验 manifests.sha256sum（逐文件 dd iflag=direct）…"
# manifests.sha256sum 由 backup 在备份根跑 `sha256sum manifests/*.json` 生成，故每行路径是
# `manifests/<file>`（相对 $BK/）。`read -r want rel` 按空白拆，rel=完整路径。
while read -r want rel; do
  [ -n "$want" ] || continue
  rel="${rel#\*}"   # 去掉二进制模式前缀 '*'（若有）
  _verify_sha_direct "$BK/$rel" "$want"
done < "$BK/manifests.sha256sum"

# manifests **精确快照门**（codex R6-[critical]：只比数量不够——symlink 逃过 find -type f、重复
# sidecar 行+漏列都是"数量相等集合不等"）。走可测 python 模块做严格集合比较 + 拒 symlink + 去重。
( cd "$ROOT" && "${UV_PY[@]}" infra/backup/cass/restore_manifest_check.py \
    "$BK/manifests.sha256sum" "$BK/manifests" ) 9>&- 8>&- 7>&- \
  || { echo "[restore] FATAL: manifest 精确快照门未过（见上）" >&2; exit 1; }

# digest.json 锚点：把 db 与 manifest 清单绑到本备份的**权威 digest**（让 restore 端与发布门同强度）。
# python3 读失败/字段缺 → 变量空 → 比对必不符 → FATAL（fail-closed）。
_DB_SHA_DIGEST="$(python3 -c "import json;print(json.load(open('$BK/digest.json')).get('db_sha256',''))" 9>&- 8>&- 7>&- 2>/dev/null || true)"
[ -n "$_DB_SHA_DIGEST" ] && [ "$_DB_SHA_DIGEST" = "$DB_WANT" ] \
  || { echo "[restore] FATAL: digest.db_sha256 与 db.sha256 sidecar 不符（备份自身不自洽）" >&2; exit 1; }
_MAN_SIDECAR_SHA="$(sha256sum "$BK/manifests.sha256sum" 9>&- 8>&- 7>&- | awk '{print $1}' 9>&- 8>&- 7>&-)"
_MAN_SHA_DIGEST="$(python3 -c "import json;print(json.load(open('$BK/digest.json')).get('manifests_sha256sum_sha256',''))" 9>&- 8>&- 7>&- 2>/dev/null || true)"
[ -n "$_MAN_SHA_DIGEST" ] && [ "$_MAN_SHA_DIGEST" = "$_MAN_SIDECAR_SHA" ] \
  || { echo "[restore] FATAL: digest.manifests_sha256sum_sha256 与实算不符（manifest 清单不自洽）" >&2; exit 1; }

echo "[restore] step 2 OK：产物校验通过（COMPLETE + db + manifest 集合恒等 + digest 锚点，均绕页缓存）"

# ---------------------------------------------------------------------------
# step 3 — 落 DB：db → <target>/agent_search.db（绝不拷贝任何 -wal / -shm）
# ---------------------------------------------------------------------------
echo "[restore] step 3: 落 DB → $TARGET/agent_search.db"
cp "$BK/db" "$TARGET/agent_search.db" 9>&- 8>&- 7>&-
# 显式防呆：备份的 .backup 产物本就无 -wal/-shm；若不慎带入会读到陈旧半事务（§9.4 V26）
rm -f "$TARGET/agent_search.db-wal" "$TARGET/agent_search.db-shm" 9>&- 8>&- 7>&-

# ---------------------------------------------------------------------------
# step 4 — 落 raw-mirror：blob 取共享池，manifest 取本备份快照；chmod 700
# ---------------------------------------------------------------------------
echo "[restore] step 4: 落 raw-mirror（blob=共享池 / manifest=本备份快照）"
mkdir -p "$TARGET/raw-mirror/v1" 9>&- 8>&- 7>&-
# blob 取共享池 $DEST/raw-mirror/v1/blobs（内容寻址 <blobs>/blake3/<2hex>/<64hex>.raw，只增不改）
[ -d "$DEST/raw-mirror/v1/blobs" ] || { echo "[restore] FATAL: 共享 blob 池不存在: $DEST/raw-mirror/v1/blobs" >&2; exit 1; }
cp -a "$DEST/raw-mirror/v1/blobs" "$TARGET/raw-mirror/v1/blobs" 9>&- 8>&- 7>&-
# manifest **必须**取本备份目录内的快照（共享目录没有 manifest；manifest 可变，用别时刻的不自洽）
[ -d "$BK/manifests" ] || { echo "[restore] FATAL: 备份缺 manifests 快照: $BK/manifests" >&2; exit 1; }
cp -a "$BK/manifests" "$TARGET/raw-mirror/v1/manifests" 9>&- 8>&- 7>&-
mkdir -p "$TARGET/raw-mirror/v1/tmp" 9>&- 8>&- 7>&-
chmod 700 "$TARGET/raw-mirror"

# 会话源恢复（可选）：$DEST/sessions/<alias>/ → 源根（或 --sessions-into 前缀）。
# 演练默认跳过（写回生产源 = 违 V25 零改动）；真灾难或显式 --sessions-into 才做。
#
# ⚠ **绝不用 `rsync --append`**（codex 2026-07-12 抓出）：--append 假设目标既有前缀与源完全一致、
#    只补尾部——若生产源残留半截/错前缀文件，它会从错误 offset 追加并 return 0，**静默拼坏生产会话**。
#    改用 **`--ignore-existing`**：只补目标缺失的文件，**绝不碰任何已存在文件**（不可能拼坏，最坏是
#    某已存在的截断文件不被修复——可接受，用户可手工处理）。写回**生产源**（--sessions-into-source）
#    额外要 `--yes` 显式确认（改的是 live 数据）——该门 + 与 --sessions-into 互斥已在 step0a 前置，
#    确保参数错误在停服务/复制之前就失败（codex R5）。
if [ "$SESSIONS_TO_SOURCE" = "1" ] || [ -n "$SESSIONS_INTO" ]; then
  # 会话恢复 fail-closed 门（codex R10-[critical]）：复制**之前**校验所选备份的 sessions 清单与共享池
  #   自洽——sha256(sessions.tsv)==digest.sessions_tsv_sha256 且清单每条会话在池内 存在+size+blake3 相符。
  #   否则池缺失/少文件/腐烂时 rsync 会拷残缺集合还报成功；`--sessions-into-source` 源全丢场景下生产
  #   会话源会保持空缺/不完整而脚本仍返回 0（谎报成功）。走可测 python 模块，缺一即 fail-closed。
  [ -f "$BK/sessions.tsv" ] || { echo "[restore] FATAL: 会话恢复被请求，但备份缺 sessions.tsv: $BK" >&2; exit 1; }
  _SESS_SHA_DIGEST="$(python3 -c "import json;print(json.load(open('$BK/digest.json')).get('sessions_tsv_sha256',''))" 9>&- 8>&- 7>&- 2>/dev/null || true)"
  ( cd "$ROOT" && "${UV_PY[@]}" infra/backup/cass/restore_sessions_check.py \
      "$BK/sessions.tsv" "$_SESS_SHA_DIGEST" "$DEST/sessions" ) 9>&- 8>&- 7>&- \
    || { echo "[restore] FATAL: 会话恢复 fail-closed 门未过（见上）" >&2; exit 1; }
  echo "[restore] step 4: 恢复会话源（jsonl，--ignore-existing 只补缺失、绝不动已存在）"
  for alias in "${!SESSION_ROOTS[@]}"; do
    src="$DEST/sessions/$alias"
    [ -d "$src" ] || continue
    if [ "$SESSIONS_TO_SOURCE" = "1" ]; then
      dst="${SESSION_ROOTS[$alias]}"
      echo "[restore]   ⚠ 写回生产源: $alias → $dst（--ignore-existing）"
    else
      dst="$SESSIONS_INTO/$alias"
    fi
    mkdir -p "$dst" 9>&- 8>&- 7>&-
    rsync -a --ignore-existing --prune-empty-dirs --include='*/' --include='*.jsonl' --exclude='*' \
      "$src/" "$dst/" 9>&- 8>&- 7>&-
  done
else
  echo "[restore] step 4: 跳过会话源恢复（未给 --sessions-into[-source]；演练默认，避免写回生产源）"
fi

# ---------------------------------------------------------------------------
# step 5 —（可选）restore-from-mirror.py：仅当需要重建源清单 sources.toml。
#   注意：它把 mirror winner 恢复到一个 **staging fake-HOME**（不是本 data-dir），
#   只重建 mirror + sources.toml，不落 DB / 不重建 Tier 2。本脚本默认**不跑**它
#   （db 已从备份直接落、blob 已从共享池落）。需要时手动：
#     ( cd "$ROOT" && uv run python infra/cass-semantic/restore-from-mirror.py --help )
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# step 7 —（先于重建）meta 水位：别删 last_scan_ts:*（>1GiB 库缺水位会 bootstrap 成"当前时刻"、
#   跳过全部历史）。要重扫历史用 --rescan-history 把它们置 0。
# ---------------------------------------------------------------------------
if [ "$RESCAN" = "1" ]; then
  echo "[restore] step 7: --rescan-history → 置 meta.last_scan_ts:* = 0（强制重扫历史）"
  sqlite3 "$TARGET/agent_search.db" \
    "UPDATE meta SET value='0' WHERE key='last_scan_ts' OR key LIKE 'last_scan_ts:%';" 9>&- 8>&- 7>&-
fi

# ---------------------------------------------------------------------------
# step 6 — 重建 Tier 2：删 index/ vector_index/，lexical（--force-rebuild，分钟级）
#   + semantic（models backfill 循环至 published，≈2h）。绝不用 index --full / index --semantic
#   全量（上游 #244/#258 死锁/stall）。重建期 CASS_INDEX_STALL_ABORT_SECS=0。
# ---------------------------------------------------------------------------
rm -rf "$TARGET/index" "$TARGET/vector_index" 9>&- 8>&- 7>&-
export CASS_INDEX_STALL_ABORT_SECS=0

echo "[restore] step 6a: lexical 重建（index --force-rebuild）…"
CASS_DATA_DIR="$TARGET" CASS_INFINITY_URL="$URL" "$CASS_BIN" index --force-rebuild 9>&- 8>&- 7>&- \
  || { echo "[restore] FATAL: lexical 重建失败" >&2; exit 1; }
LEX_KB="$(du -sk "$TARGET/index" 2>/dev/null 9>&- 8>&- 7>&- | awk '{print $1}' 9>&- 8>&- 7>&-)"
[ "${LEX_KB:-0}" -gt 500 ] 2>/dev/null \
  || { echo "[restore] FATAL: lexical index 过小（${LEX_KB}KB <= 500KB），疑重建未成" >&2; exit 1; }
echo "[restore] step 6a OK（index ${LEX_KB}KB）"

if [ "$SKIP_SEMANTIC" = "1" ]; then
  echo "[restore] step 6b: --skip-semantic → 跳过 ≈2h semantic 重建（仅冒烟）"
else
  echo "[restore] step 6b: semantic 重建（models backfill 循环，≈2h）…"
  prev=-1; pub=""
  for i in $(seq 1 200); do
    out="$(CASS_DATA_DIR="$TARGET" CASS_INFINITY_URL="$URL" "$CASS_BIN" \
      models backfill --tier quality --embedder infinity --scheduled \
      --batch-conversations 999999 --json 9>&- 8>&- 7>&- 2>>/tmp/cc-restore-backfill.err)" \
      || { echo "[restore] FATAL: semantic backfill 报错（见 /tmp/cc-restore-backfill.err）" >&2; exit 1; }
    read -r pub off < <(printf '%s' "$out" | python3 -c \
      "import sys,json;d=json.load(sys.stdin);print(d.get('published'),d.get('last_offset'))" 9>&- 8>&- 7>&-) \
      || { echo "[restore] FATAL: backfill json 解析失败" >&2; exit 1; }
    echo "[restore]   semantic iter $i: published=$pub offset=$off"
    [ "$pub" = "True" ] && break
    [ "$off" = "$prev" ] && { echo "[restore] FATAL: semantic STALLED at offset $off" >&2; exit 1; }
    prev="$off"
  done
  [ "$pub" = "True" ] || { echo "[restore] FATAL: bge-m3 semantic 未 published（200 轮未收敛）" >&2; exit 1; }
  # serving-dir 完整性
  [ -f "$TARGET/vector_index/index-bge-m3.fsvi" ] \
    || { echo "[restore] FATAL: 缺 semantic 产物 index-bge-m3.fsvi" >&2; exit 1; }
  python3 -c "import json,sys;m=json.load(open('$TARGET/vector_index/semantic_manifest.json'))['quality_tier'];sys.exit(0 if m.get('ready') and m.get('embedder_id')=='bge-m3' else 1)" 9>&- 8>&- 7>&- \
    || { echo "[restore] FATAL: semantic_manifest 未 ready 或 embedder_id != bge-m3" >&2; exit 1; }
  echo "[restore] step 6b OK（bge-m3 published）"
fi

# ---------------------------------------------------------------------------
# step 8 — 验证：doctor 的 raw_mirror.summary.* 全 0 且 verified_blob_count>0；search 命中已知会话
#   （零错误与没检查在计数器上长得一样 → 必须额外断言 verified_blob_count>0，spec §2.10/§4.3）
# ---------------------------------------------------------------------------
echo "[restore] step 8: doctor 验证（raw_mirror.summary 全 0 且 verified_blob_count>0）…"
DOCTOR_JSON="$(CASS_DATA_DIR="$TARGET" "$CASS_BIN" doctor --json --data-dir "$TARGET" 9>&- 8>&- 7>&- || true)"
# validator 从 **stdin** 读（纯 pipe）。**绝不用 `python3 - <<'PY'`**——here-doc 会占 python 的
# stdin 当脚本源，`sys.stdin.read()` 读到 EOF、DOCTOR_JSON 被吞（codex 2026-07-12 抓出的 P0：
# 有效恢复也会在此崩，且 cass-mcp 已在 preflight 停、末尾 start 不执行 → 服务留停）。
printf '%s' "$DOCTOR_JSON" \
  | ( cd "$ROOT" && "${UV_PY[@]}" infra/backup/cass/restore_verify.py ) 9>&- 8>&- 7>&- \
  || { echo "[restore] FATAL: doctor 验证门未过（见上）" >&2; exit 1; }

# search 可用性验证：semantic 重建了就验 semantic；--skip-semantic 时验 **lexical**（不整个跳过——
# 跳过会让检索层不可用/零命中却仍报完成，codex 2026-07-12 抓出）。§2.5：fts_messages_config
# 引起的 abort 是良性、不算失败，故只判「有命中」。
#
# ⚠ semantic 门必须用**生产 cass-mcp 真依赖的**路由（cass_mcp/config.py 的 SEMANTIC_FLAGS =
#   --mode semantic --daemon --model bge-m3 --rerank，--rerank 恒开），否则验的是另一条路：
#   native/default semantic 可用 ≠ 用户依赖的 daemon+bge-m3+rerank 路由可用 → 好恢复误判失败，
#   或没证明 cass-mcp semantic 可用就报成功（codex R9-[critical]）。lexical 路由不接受
#   --model/--rerank（contract.py），故按 mode 分支构造 flags；与 config.py 一致由集成测试守。
if [ "$SKIP_SEMANTIC" = "1" ]; then
  SEARCH_MODE=lexical
  SEARCH_FLAGS=(--mode lexical)
else
  SEARCH_MODE=semantic
  SEARCH_FLAGS=(--mode semantic --daemon --model bge-m3 --rerank)   # == cass_mcp.config.SEMANTIC_FLAGS
fi
echo "[restore] step 8: cass search（$SEARCH_MODE，flags: ${SEARCH_FLAGS[*]}）命中已知会话验证…"
# **先捕获 search 的 stdout + rc，再单独解析**——绝不把 search 和解析放同一 pipefail 管道里：
# §2.5 的 fts_messages_config 良性 abort 会让 search **非零退出但已吐有效 hits**；若同管道，
# pipefail 会触发 `|| echo 0` 往 stdout 追加一行 → HITS 成多行 `3\n0` → `[ -gt 0 ]` 崩、
# 误杀有效恢复且跳过收尾 systemctl start（codex 2026-07-12 抓出）。非零 rc 只当 warning。
set +e
SEARCH_JSON="$(CASS_DATA_DIR="$TARGET" CASS_INFINITY_URL="$URL" "$CASS_BIN" \
  search "error" "${SEARCH_FLAGS[@]}" --json --limit 3 9>&- 8>&- 7>&- 2>/dev/null)"
SEARCH_RC=$?
set -e
HITS="$(printf '%s' "$SEARCH_JSON" | python3 -c "import sys,json
try: print(len(json.load(sys.stdin).get('hits',[])))
except Exception: print(0)" 9>&- 8>&- 7>&-)"
if [ "${HITS:-0}" -gt 0 ] 2>/dev/null; then
  echo "[restore] step 8 OK：search（$SEARCH_MODE）命中 $HITS 条（search rc=$SEARCH_RC；非零多为 §2.5 良性 abort，有 hits 即算可用）"
else
  echo "[restore] FATAL: cass search（$SEARCH_MODE）零命中/无有效 JSON（restore 后检索层不可用，rc=$SEARCH_RC）" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 收尾 — cass-mcp 的重启 + 写锁释放由 EXIT 上的 cleanup trap **统一**处理（成功/失败/被杀都走它，
#   带 9>&- 释放锁继承——否则常驻服务继承 fd 9 永久持锁、饿死 hourly index-pull）。这里不重复 start。
# ---------------------------------------------------------------------------
echo "[restore] 全部步骤完成，cass-mcp 将由 cleanup 拉起…"

cat <<EOF

[restore] ✅ 完成。恢复到: $TARGET
  提示：
   - 若刚 restore 的是**替换生产**，需把 $TARGET 切成 canonical（移开损坏库后 mv/symlink），并核对
     cass search / cass-mcp 指向新库。
   - step 10：cass mirror prune 后缺失的 blob 只能从更早备份找回（本脚本不 prune）。
   - 陈旧 -wal/-shm 已在 step 3 显式删除；绝不从别处拷入。
EOF
