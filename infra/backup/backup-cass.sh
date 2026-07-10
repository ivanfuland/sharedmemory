#!/usr/bin/env bash
# CASS data_dir 每日备份 —— 设计契约见
# ~/projects/cc-workspace/docs/projects/shared-memory/specs/2026-07-09-cass-data-dir-backup-design.md
# （spec §6 数据流 step 0-18；本文件当前实现到 step 9，step 10+ 见文件末尾标记）。
#
# PUBLIC 仓纪律：本文件禁止出现任何真实路径 / 密钥 / 基建拓扑，只放命令与判据。
#
# 两把锁语义不同，不可混淆（spec §6.2）：
#   - 自身锁（fd 8）抢不到 = 另一个 backup-cass.sh 实例在跑，它会完成备份 —— 静默 skip，exit 0。
#   - .cass-write.lock（fd 9）抢不到 = 有 CASS 写者在跑，没有任何人会替你做备份 —— 必须
#     exit 非零告警，cron 包装层据此发 TG。
#
# 数据流 step 0-9（本骨架实现范围）：
#   0 自身锁 → 1 参数校验 + blake3 preflight → 2 staging guard（非 tmpfs + 3x 余量）
#   → 3 NAS guard（挂载 + 可写探针）→ 4 陈旧 .incomplete-* 清理
#   → 5-8 写锁段（doctor Tier 0 门 + .backup + manifests 快照，锁内三件事一件不少）
#   → 9 五腿门（锁外，staging 上；失败落 SUSPECT-<stamp>/ 取证）
set -euo pipefail
shopt -s nullglob

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# CASS_BACKUP_VENV_PY 是 Task 9 新增的测试口子（同 DEV-4 系列约定：可测性优先，cron 白名单
# 传不进，无生产风险）：默认走仓内单解释器 venv，测试可覆盖成裸 python3 验证 blake3 preflight
# 真的会在 doctor 被调用之前拦下来。
VENV_PY="${CASS_BACKUP_VENV_PY:-$ROOT/.venv/bin/python}"
LIB="$ROOT/infra/backup/cass"

# --- env 契约（plan「关键接口」全集，默认值逐一落此；本 task 只用到 staging/两把锁的
#     超时/stamp，其余留给后续 task 消费——先落齐默认值，避免下个 task 还要回来补） ---
CASS_DATA_DIR="${CASS_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
DEST="${CASS_BACKUP_DEST:-$HOME/nas/openclaw/backups/cass}"
KEEP="${CASS_BACKUP_KEEP:-7}"
VERIFY_DOW="${CASS_BACKUP_VERIFY_DOW:-7}"
STAGING="${CASS_BACKUP_STAGING:-${TMPDIR:-/tmp}}"
LOCK_WAIT="${CASS_BACKUP_LOCK_WAIT:-900}"
DB_TIMEOUT="${CASS_BACKUP_DB_TIMEOUT:-600}"
DOCTOR_TIMEOUT="${CASS_BACKUP_DOCTOR_TIMEOUT:-900}"
STAMP="${CASS_BACKUP_STAMP:-$(date +%Y%m%d-%H%M%S)-$$}"
SESSION_ROOTS="${CASS_BACKUP_SESSION_ROOTS:-claude-projects=$HOME/.claude/projects:codex-sessions=$HOME/.codex/sessions:openclaw-agents=$HOME/.openclaw/agents}"
TG_ENV="${CASS_BACKUP_TG_ENV:-$HOME/.claude/channels/telegram/.env}"
# 测试故障注入枚举（DEV-7）——本 task 不实现任何注入点，只读取占位，供 Task 10+ 消费。
FAULT="${CASS_BACKUP_FAULT:-}"

# 人工通道，四组各自成对（缺一即拒绝运行——cron 只传 {PATH,HOME} 白名单，传不进这些变量，
# 故这些通道只有人在 shell 里能触发，见 spec §5.7）。本 task 只做成对性校验 + （rebaseline）
# 接入五腿门 CLI；adopt/quarantine/retention_reset 的消费点在后续 task。
REBASELINE="${CASS_BACKUP_REBASELINE:-}"
REBASELINE_REASON="${CASS_BACKUP_REBASELINE_REASON:-}"
ADOPT_SESSIONS="${CASS_BACKUP_ADOPT_SESSIONS:-}"
ADOPT_REASON="${CASS_BACKUP_ADOPT_REASON:-}"
QUARANTINE_SESSIONS="${CASS_BACKUP_QUARANTINE_SESSIONS:-}"
QUARANTINE_REASON="${CASS_BACKUP_QUARANTINE_REASON:-}"
RETENTION_RESET="${CASS_BACKUP_RETENTION_RESET:-}"
RETENTION_RESET_REASON="${CASS_BACKUP_RETENTION_RESET_REASON:-}"

ALERT_FLAG=0        # step 4 的 RECOVERABLE 救援会置 1；即使备份本身成功也要 exit 非零（DEV-6）
TRAP_INCOMPLETE=""   # 占位，Task 10 起指向 .incomplete-$STAMP，供 trap 在异常路径改名/清理
STG=""               # step 2 通过后赋值为本轮 staging 工作目录

cleanup() {
  # EXIT trap：bash 保证 trap 处理函数不会覆盖触发退出时的 $?（除非这里显式 exit），
  # 故只管清理，不需要手动保存/恢复退出码。
  if [ -n "$STG" ]; then
    rm -rf "$STG" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'echo "[backup] FATAL: terminated (SIGTERM)"; exit 143' TERM
trap 'echo "[backup] FATAL: interrupted (SIGINT)"; exit 130' INT

# ---------------------------------------------------------------------------
# step 0 — 自身锁（抢不到 = 另一实例在跑，它会完成备份，静默 skip，spec §6.2）
# ---------------------------------------------------------------------------
mkdir -p "$STAGING"
exec 8>"$STAGING/.backup-cass.self.lock"
flock -n 8 || { echo "skip: another backup-cass.sh instance holds the self lock"; exit 0; }

# ---------------------------------------------------------------------------
# step 1 — 参数校验 + blake3 preflight（DEV-5：早期探测，别等 doctor 5.4 min 后才炸）
# ---------------------------------------------------------------------------
if ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
  echo "[backup] FATAL: CASS_BACKUP_KEEP must be a positive integer, got '$KEEP'"; exit 1
fi
if ! [[ "$VERIFY_DOW" =~ ^[1-7]$ ]]; then
  echo "[backup] FATAL: CASS_BACKUP_VERIFY_DOW must be an integer 1-7 (date +%u), got '$VERIFY_DOW'"; exit 1
fi

_pair_or_die() {  # $1=人读名字 $2=A 值 $3=B(reason) 值 —— 缺一即拒绝运行（fail-closed）
  local name="$1" a="$2" b="$3"
  if { [ -n "$a" ] && [ -z "$b" ]; } || { [ -z "$a" ] && [ -n "$b" ]; }; then
    echo "[backup] FATAL: $name must be provided in pairs (value + reason), missing one"; exit 1
  fi
}
_pair_or_die "CASS_BACKUP_REBASELINE / CASS_BACKUP_REBASELINE_REASON" "$REBASELINE" "$REBASELINE_REASON"
_pair_or_die "CASS_BACKUP_ADOPT_SESSIONS / CASS_BACKUP_ADOPT_REASON" "$ADOPT_SESSIONS" "$ADOPT_REASON"
_pair_or_die "CASS_BACKUP_QUARANTINE_SESSIONS / CASS_BACKUP_QUARANTINE_REASON" "$QUARANTINE_SESSIONS" "$QUARANTINE_REASON"
_pair_or_die "CASS_BACKUP_RETENTION_RESET / CASS_BACKUP_RETENTION_RESET_REASON" "$RETENTION_RESET" "$RETENTION_RESET_REASON"

if ! "$VENV_PY" -c 'import blake3' 9>&- 8>&- 2>/dev/null; then
  echo "[backup] FATAL: $VENV_PY missing blake3 (sessions/blob 校验硬依赖，Task 12+ 消费) — refusing to proceed"
  exit 1
fi

# ---------------------------------------------------------------------------
# step 2 — staging guard：非 tmpfs 且余量 >= 3x db 大小；通过后建本轮 staging 工作目录
# ---------------------------------------------------------------------------
STAGING_FSTYPE="$(stat -f -c %T "$STAGING")"
if [ "$STAGING_FSTYPE" = "tmpfs" ]; then
  echo "[backup] FATAL: staging dir $STAGING is tmpfs — refusing (backup must not round-trip through RAM-only fs)"
  exit 1
fi
if [ ! -f "$CASS_DATA_DIR/agent_search.db" ]; then
  echo "[backup] FATAL: CASS_DATA_DIR db missing: $CASS_DATA_DIR/agent_search.db"
  exit 1
fi
DB_SIZE="$(stat -c %s "$CASS_DATA_DIR/agent_search.db")"
STAGING_AVAIL="$(df --output=avail -B1 "$STAGING" | tail -n1 | tr -d ' ')"
STAGING_REQUIRED=$((DB_SIZE * 3))
if [ "$STAGING_AVAIL" -lt "$STAGING_REQUIRED" ]; then
  echo "[backup] FATAL: staging $STAGING has ${STAGING_AVAIL}B free, need >= ${STAGING_REQUIRED}B (3x db size ${DB_SIZE}B)"
  exit 1
fi

STG="$(mktemp -d "$STAGING/cass-backup-$STAMP.XXXX")"

# ---------------------------------------------------------------------------
# step 3 — NAS guard：mountpoint 且可写探针（backup-gbrain.sh L41-48 构型逐字照抄——
# DEST 自身不是 mountpoint，直接 mountpoint -q "$DEST" 会让生产备份永久拒绝）
# ---------------------------------------------------------------------------
NAS_PREFIX="$HOME/nas/"
if [[ "$DEST" == "$NAS_PREFIX"* ]]; then
  rest="${DEST#"$NAS_PREFIX"}"; share="${rest%%/*}"; SHARE_ROOT="$NAS_PREFIX$share"
  # stat 先触发任何 autofs 挂载，再要求它是真挂载点。
  ls "$SHARE_ROOT" >/dev/null 2>&1 || true
  if ! mountpoint -q "$SHARE_ROOT" 2>/dev/null; then
    echo "[backup] FATAL: NAS share not mounted at $SHARE_ROOT — refusing to back up to local disk"
    exit 1
  fi
fi
mkdir -p "$DEST"
# 无条件可写探针（挂载点只证明挂载、不证明可写——soft 挂载 I/O 出错会返回错误而非挂起）：
PROBE="$DEST/.cass-write-probe.$$"
if ! touch "$PROBE" 2>/dev/null; then
  echo "[backup] FATAL: DEST not writable: $DEST"
  exit 1
fi
rm -f "$PROBE"

# ---------------------------------------------------------------------------
# step 4 — 清理 NAS 上陈旧的 .incomplete-*（自身锁保证无并发）：mtime > 1 天。
# 含顶层 COMPLETE 的绝不当垃圾清掉——那可能是「touch COMPLETE 已完成、mv -T 尚未执行」时
# 断电/SIGKILL 留下的完整且已校验通过的备份载荷。mv -T 成 RECOVERABLE-<stamp> + 告警继续
# 当晚备份（DEV-6）；不含则 rm -rf。
# ---------------------------------------------------------------------------
NOW_EPOCH="$(date +%s)"
STALE_CUTOFF=$((NOW_EPOCH - 86400))
for d in "$DEST"/.incomplete-*; do
  [ -d "$d" ] || continue
  d_mtime="$(stat -c %Y "$d")"
  [ "$d_mtime" -lt "$STALE_CUTOFF" ] || continue
  suffix="${d#"$DEST"/.incomplete-}"
  if [ -f "$d/COMPLETE" ]; then
    mv -T "$d" "$DEST/RECOVERABLE-$suffix"
    echo "[backup] ALERT: stale .incomplete-* had COMPLETE — recovered to RECOVERABLE-$suffix (needs human review)"
    ALERT_FLAG=1
  else
    rm -rf "$d"
    echo "[backup] cleaned stale incomplete dir: $d"
  fi
done

# ---------------------------------------------------------------------------
# step 5-8 — 写锁段：doctor（Tier 0 门）+ .backup + manifests 快照，全部锁内一次做完。
# 持锁期间每个子进程调用带 9>&- 8>&-（exec 9> 的 fd 会被子进程继承，bash 不设
# O_CLOEXEC——见 spec §4.3 restore-cass.sh 的教训，backup-cass.sh 同理）。
# ---------------------------------------------------------------------------
mkdir -p "$HOME/.local/share"
exec 9>"$HOME/.local/share/.cass-write.lock"
flock -w "$LOCK_WAIT" 9 || { echo "[backup] FATAL: cass write lock busy"; exit 1; }

timeout "$DOCTOR_TIMEOUT" cass doctor --json --data-dir "$CASS_DATA_DIR" > "$STG/doctor.json" 9>&- 8>&- || true
#   （doctor exit code 不可信；status/summary 的锁内快速检查：status 非 verified/warn 结构
#   → exit 1 —— 这一判据由紧随其后的 cass_manifest_census.py 一并做出，见下方调用）
timeout "$DB_TIMEOUT" sqlite3 "$CASS_DATA_DIR/agent_search.db" ".backup '$STG/db'" 9>&- 8>&- \
  || { echo "[backup] FATAL: .backup failed/timeout"; exit 1; }
cp -a "$CASS_DATA_DIR/raw-mirror/v1/manifests" "$STG/manifests" 9>&- 8>&-
# Tier 0 门收尾【仍在锁内，codex R2-P1】：独立普查与 doctor 必须看同一个锁内状态（spec
# §5.6），且普查的 blob stat 打的是源端 —— 锁内跑（~1-2 s，doctor 5.4 min 面前可忽略）：
"$VENV_PY" "$LIB/cass_manifest_census.py" --manifests-dir "$STG/manifests" \
    --doctor-json "$STG/doctor.json" --blobs-root "$CASS_DATA_DIR/raw-mirror/v1/blobs" 9>&- 8>&- \
  || { echo "[backup] FATAL: Tier 0 gate failed"; exit 1; }
#   Tier 0 不过 → exit 1，零 NAS 产物（不落 SUSPECT——SUSPECT 是五腿门专属的取证路径）
exec 9>&-      # 释放写锁

# ---------------------------------------------------------------------------
# step 9 — DB 五腿门（锁外，staging 上；与 Tier 0 门的失败语义不同——spec §6 step 6 vs 9）
# ---------------------------------------------------------------------------
GATE_ARGS=(
  --db "$STG/db" --dest "$DEST"
  --out-census "$STG/census.tsv" --out-gate-json "$STG/gate.json"
)
if [ -n "$REBASELINE" ]; then
  GATE_ARGS+=(--rebaseline "$REBASELINE" --rebaseline-reason "$REBASELINE_REASON")
fi
DB_GATE_FAIL=0
"$VENV_PY" "$LIB/cass_backup_gate.py" "${GATE_ARGS[@]}" 8>&- || DB_GATE_FAIL=1

if [ "$DB_GATE_FAIL" = 1 ]; then
  SUSPECT_DIR="$DEST/SUSPECT-$STAMP"
  mkdir "$SUSPECT_DIR"
  cp -a "$STG/db" "$SUSPECT_DIR/db"
  cp -a "$STG/census.tsv" "$SUSPECT_DIR/census.tsv"
  cp -a "$STG/gate.json" "$SUSPECT_DIR/gate.json"
  # digest.json 版取证（完整 sidecar：backup_name/generation/prev_* 等，供人 diff 新旧
  # sidecar 判断这是迁移还是事故，spec §5.7 原文）是 Task 13 的升级点；本 task 先落
  # db + census.tsv + gate.json（进度 ledger 已记「Task 13 待办注入」）。无 COMPLETE 不入链。
  echo "[backup] FATAL: five-leg gate failed — forensics landed at $SUSPECT_DIR"
  exit 1
fi

# === STEP 10+ (Task 10/11/12/13) ===
# 后续 task 在此扩展 step 10 起的 .incomplete 落盘 / O_DIRECT 读回 / blobs+manifests 双门 /
# sessions 通道 / digest.json / COMPLETE 发布 / keep-N 轮转 / 周校验。
if [ "$ALERT_FLAG" = 1 ]; then
  echo "[backup] gate passed but a stale RECOVERABLE-* alert was raised above — exiting non-zero (DEV-6)"
  exit 1
fi
echo "[backup] gate passed (steps 10+ not yet implemented)"
exit 0
