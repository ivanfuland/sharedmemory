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
TRAP_INCOMPLETE=""   # step 10 起指向 $DEST/.incomplete-$STAMP；非空时 EXIT trap 会 rm -rf
                      # 它——覆盖「没走 fail_incomplete 就意外退出」的路径（SIGTERM/未预料
                      # 的 set -e 击杀）。fail_incomplete 改名前、以及临时成功出口前都必须
                      # 先清空它，否则刚保下来的东西会被这里删掉（spec §6.6 实现注意）。
STG=""               # step 2 通过后赋值为本轮 staging 工作目录

cleanup() {
  # EXIT trap：bash 保证 trap 处理函数不会覆盖触发退出时的 $?（除非这里显式 exit），
  # 故只管清理，不需要手动保存/恢复退出码。
  if [ -n "$STG" ]; then
    rm -rf "$STG" 2>/dev/null || true
  fi
  if [ -n "$TRAP_INCOMPLETE" ]; then
    rm -rf "$TRAP_INCOMPLETE" 2>/dev/null || true
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
# DEV-7 故障注入：`.backup` 刚完成、`.incomplete-$STAMP` 尚未创建——此刻 NAS 上
# 还没有任何本轮产物，验证「崩在这里 = NAS 零产物」（V7 两个注入点之一）。
if [ "$FAULT" = "kill-after-db-backup" ]; then
  kill -9 $$
fi
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
# gate 的 exit 语义三分，必须按精确 rc 分流（不能 `|| FAIL=1` 一锅端）：
#   0 = PASS → 继续
#   1 = 数据 FAIL → census/gate.json 已落盘（CLI 契约：产物无论 PASS/FAIL 都写），走 SUSPECT 取证
#   2 = 用法/环境错（如 rebaseline 目标非法）→ 此时产物根本没写；若误并进 SUSPECT 路径，
#       cp 不存在的文件会被 set -e 中途击杀 → NAS 留半拉 SUSPECT-*/db 孤儿 + FATAL 消息
#       没打出来 + 真实 stderr 被埋。环境错没有取证价值——不落 SUSPECT，直接 FATAL。
GATE_RC=0
"$VENV_PY" "$LIB/cass_backup_gate.py" "${GATE_ARGS[@]}" 8>&- || GATE_RC=$?

if [ "$GATE_RC" -eq 1 ]; then
  SUSPECT_DIR="$DEST/SUSPECT-$STAMP"
  mkdir "$SUSPECT_DIR"
  cp -a "$STG/db" "$SUSPECT_DIR/db"
  cp -a "$STG/census.tsv" "$SUSPECT_DIR/census.tsv"
  cp -a "$STG/gate.json" "$SUSPECT_DIR/gate.json"
  # digest.json 版取证（Task 13 升级点，spec §5.7「人可以直接 diff 新旧 sidecar
  # 判断这是迁移还是事故」）：五腿门失败发生在 step 10+ 之前，sessions.tsv 与
  # manifests.sha256sum 这两个字段的源文件此刻根本不存在——用「当晚可得字段」
  # 组装，缺的两个 sha 字段留空串，其余字段（含 rebaseline 留痕）照常算。
  # 最佳努力：这份 digest.json 是取证辅助，不是发布契约的一部分（无 COMPLETE
  # 不入链），写失败不应该掩盖下面真正的 FATAL 消息与 exit 1。
  LIB="$LIB" DEST="$DEST" STG="$STG" SUSPECT_DIR="$SUSPECT_DIR" STAMP="$STAMP" \
    "$VENV_PY" 8>&- - <<'PYEOF' || true
import json
import os
import pathlib
import sys

sys.path.insert(0, os.environ["LIB"])
import cass_common  # noqa: E402

dest = pathlib.Path(os.environ["DEST"])
stg = pathlib.Path(os.environ["STG"])
suspect_dir = pathlib.Path(os.environ["SUSPECT_DIR"])
stamp = os.environ["STAMP"]

gate = json.loads((stg / "gate.json").read_bytes())

prev = cass_common.latest_published(dest)
if prev is None:
    generation = 1
    prev_backup_name = ""
    prev_sidecar_sha256 = ""
else:
    prev_name, prev_digest = prev
    generation = prev_digest["generation"] + 1
    prev_backup_name = prev_name
    prev_sidecar_sha256 = cass_common.sha256_file(dest / prev_name / "digest.json")

digest: dict = {
    "backup_name": f"SUSPECT-{stamp}",
    "generation": generation,
    "prev_backup_name": prev_backup_name,
    "prev_sidecar_sha256": prev_sidecar_sha256,
    "db_sha256": cass_common.sha256_file(stg / "db"),
    "census_sha256": gate["census_sha256"],
    "sessions_tsv_sha256": "",
    "manifests_sha256sum_sha256": "",
    "schema_fingerprint": gate["schema_fingerprint"],
    "tables": gate["tables"],
    "meta_watermarks": gate["meta_watermarks"],
}
if "rebaselined_from" in gate:
    digest["rebaselined_from"] = gate["rebaselined_from"]
    digest["reason"] = gate["reason"]

(suspect_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))
PYEOF
  echo "[backup] FATAL: five-leg gate failed — forensics landed at $SUSPECT_DIR"
  exit 1
elif [ "$GATE_RC" -ne 0 ]; then
  echo "[backup] FATAL: gate usage/env error (rc=$GATE_RC) — see stderr above; no SUSPECT written"
  exit 1
fi

# === STEP 10+ (Task 10 实现到 14b；Task 11 补 sessions 通道 A 的 13b-13d；
#     13a 完整语义/ADOPT/13e-13g、digest.json、COMPLETE 发布、keep-N 轮转、
#     周校验是 Task 12-13 待办) ===

# ---------------------------------------------------------------------------
# step 10 — .incomplete-$STAMP 落盘：db / db.sha256 / census.tsv / manifests/ /
# manifests.sha256sum（spec §6 step 10）。digest.json 此时还不能写——它要含
# sessions_tsv_sha256，而 sessions.tsv 要到 step 13g 才生成（Task 12）。
#
# 从此刻起半成品对 EXIT trap 可见（TRAP_INCOMPLETE，见上方 cleanup()）：任何没
# 走 fail_incomplete 就退出的路径（SIGTERM/未预料的 set -e 击杀）都会被自动
# rm -rf，不留一个既未校验、也未改名求助的半成品在 NAS 上。
# ---------------------------------------------------------------------------
INCOMPLETE_DIR="$DEST/.incomplete-$STAMP"

fail_incomplete() {
  # spec §6.6 实现注意：改名成 INCOMPLETE-* 之后必须先清空 trap 记录的路径，否则
  # EXIT trap 会把刚保下来的东西删掉。**先清空**（mv -T 之前）而不是之后——这样
  # 即使 mv -T 本身失败（源目录未被移动），trap 也不会趁乱把它删掉，两种结局
  # （改名成功 / mv 失败原地留守）都保住取证现场。
  local reason="$1"
  TRAP_INCOMPLETE=""
  mv -T "$INCOMPLETE_DIR" "$DEST/INCOMPLETE-$STAMP"
  echo "[backup] FATAL: $reason"
  exit 1
}

mkdir "$INCOMPLETE_DIR"
TRAP_INCOMPLETE="$INCOMPLETE_DIR"

cp -a "$STG/db" "$INCOMPLETE_DIR/db"
# DEV-7 故障注入：db 刚拷进 `.incomplete-$STAMP`、其余产物（db.sha256/census.tsv/
# manifests/manifests.sha256sum）还没落地——下一轮跑时该目录 mtime 未满 1 天不清、
# `touch -d '2 days ago'` 老化后按「无 COMPLETE 半成品」被清掉（V7 两个注入点之一）。
if [ "$FAULT" = "kill-after-incomplete-db-copy" ]; then
  kill -9 $$
fi
(cd "$STG" && sha256sum db > db.sha256) 8>&-
LOCAL_SHA="$(cut -d' ' -f1 <"$STG/db.sha256")"
cp -a "$STG/db.sha256" "$INCOMPLETE_DIR/db.sha256"
cp -a "$STG/census.tsv" "$INCOMPLETE_DIR/census.tsv"
cp -a "$STG/manifests" "$INCOMPLETE_DIR/manifests"
# 相对路径形态（cd 进 STG 再算）——落 NAS 后必须能在 .incomplete-$STAMP/ 内直接
# `sha256sum -c manifests.sha256sum` 校验（R3-P2：manifests 走 cp -a 而非 rsync -a，
# 因为 quick-check 只看 size+mtime，同 size 同 mtime 不同内容会被跳过）。
(cd "$STG" && sha256sum manifests/*.json > manifests.sha256sum) 8>&-
cp -a "$STG/manifests.sha256sum" "$INCOMPLETE_DIR/manifests.sha256sum"

# DEV-7 测试专用故障注入（枚举写死，默认无操作；cron 白名单传不进 CASS_BACKUP_FAULT，
# 见 plan 偏离登记 DEV-7）。两个注入点都作用于 NAS 侧刚落地的 .incomplete 副本，模拟
# 「A（staging）与 B（NAS）不一致」——step 11 的 O_DIRECT 读回必须抓住它们。
case "$FAULT" in
  flip-nas-db)
    printf '\xFF' | dd of="$INCOMPLETE_DIR/db" bs=1 count=1 seek=0 conv=notrunc status=none 8>&-
    ;;
  unlink-nas-db-before-readback)
    rm -f "$INCOMPLETE_DIR/db"
    ;;
  corrupt-manifest-after-snapshot)
    # Task 10 carry-forward（14a e2e）：manifests 快照刚落 NAS，翻转第一个 manifest
    # 文件一个字节——`manifests.sha256sum` 是在同一时刻用未被破坏的字节算出来的，
    # 故 step 14a 的完整性门必须能抓到这处调包（14a FAIL e2e 路径，此前一直不可达）。
    first_manifest="$(find "$INCOMPLETE_DIR/manifests" -maxdepth 1 -type f | sort | head -n1)"
    if [ -n "$first_manifest" ]; then
      printf '\xFF' | dd of="$first_manifest" bs=1 count=1 seek=0 conv=notrunc status=none 8>&-
    fi
    ;;
  *)
    : # 无操作——生产默认（空值）与尚未消费的未来枚举值（Task 12/13 复用并扩充）
    ;;
esac

# ---------------------------------------------------------------------------
# step 11 — O_DIRECT 读回校验（spec §6.4）：五腿门通过的是 staging 上的 A，落 NAS
# 的是另一份拷贝 B，必须证明 A ≡ B。脚本全局 set -euo pipefail：dd 失败会让 shell
# 在 RC 捕获之前直接退出，fail_incomplete 变成死代码——必须临时关闭再恢复
# （codex R2-P0）。`dd | sha256sum` 的 `$?` 是 sha256sum 的（dd 读失败仍会吐出
# 空输入的哈希），必须 `RC=("${PIPESTATUS[@]}")` 紧跟管道一次性整数组捕获；禁止
# 用 `管道 || true` 抑制 -e——`|| true` 分支执行后 PIPESTATUS 会被 true 覆盖成
# 单元素数组。
# ---------------------------------------------------------------------------
set +e +o pipefail
dd if="$INCOMPLETE_DIR/db" bs=1M iflag=direct status=none 8>&- | sha256sum 8>&- > "$STG/r.h"
RC=("${PIPESTATUS[@]}")
set -e -o pipefail
[ "${RC[0]}" -eq 0 ] && [ "${RC[1]}" -eq 0 ] || fail_incomplete "O_DIRECT readback failed (dd rc=${RC[0]}, sha256sum rc=${RC[1]})"
[ "$(cut -d' ' -f1 <"$STG/r.h")" = "$LOCAL_SHA" ] || fail_incomplete "db readback mismatch (staging A != NAS B)"

# ---------------------------------------------------------------------------
# step 12 — blobs 池：共享目录，只增不改。`--ignore-existing` 只准用于 blobs/
# （spec §11 硬约束——用在 manifests/ 会冻结 db_links，用在 sessions/ 会永久截断
# 半截会话）。`raw-mirror/v1/tmp/` 排除出备份。
# ---------------------------------------------------------------------------
mkdir -p "$DEST/raw-mirror/v1/blobs"
rsync -a --ignore-existing --exclude='v1/tmp/' --stats \
    "$CASS_DATA_DIR/raw-mirror/v1/blobs/" "$DEST/raw-mirror/v1/blobs/" 8>&- > "$STG/blobs.stats" \
  || fail_incomplete "blobs rsync failed"
# 供 V11 断言复用：STG 在脚本退出时被 cleanup() 清掉，测试拿不到 $STG/blobs.stats，
# 故把关键行 echo 到 stdout。
BLOBS_TRANSFERRED="$(sed -n 's/^Number of regular files transferred: \([0-9,]*\).*/\1/p' "$STG/blobs.stats")"
echo "[backup] blobs rsync: transferred ${BLOBS_TRANSFERRED:-?} files"

# ---------------------------------------------------------------------------
# step 13a — 共享权威状态 $DEST/sessions.state.tsv 的存在性门（spec §6.3.1 step
# 13a）：**state 消失是完整性事件，只有显式 ADOPT 能重建**——含首晚（部署程序见
# Task 18）。用 `fail_incomplete` 而非裸 `exit 1`：这一判据发生在 `INCOMPLETE_DIR`
# 已落地 db/manifests 之后（step 10-14b 都在它前面），裸退会被 EXIT trap 直接
# `rm -rf` 掉本可取证的半成品；`fail_incomplete` 改名成 `INCOMPLETE-$STAMP` 保留
# 现场，且同样以 exit 1 收尾（满足 spec 字面「state 缺失且未给 ADOPT → exit 1」）。
# 存在但首行校验不符的情形不在此处判断——它由下游 check-source/update-state/
# publish-gate 对 `state_read` 的 `StateCorrupt` 异常自然产生非零 exit 覆盖。
# ---------------------------------------------------------------------------
SESSIONS_STATE_PATH="$DEST/sessions.state.tsv"
if [ ! -f "$SESSIONS_STATE_PATH" ] && [ -z "$ADOPT_SESSIONS" ]; then
  fail_incomplete "sessions.state.tsv missing and CASS_BACKUP_ADOPT_SESSIONS not set — state loss requires explicit ADOPT, even on first run (spec §6.3.1 step 13a)"
fi
if [ -f "$SESSIONS_STATE_PATH" ]; then
  SESSIONS_STATE_ARG="$SESSIONS_STATE_PATH"
else
  SESSIONS_STATE_ARG="NONE"
fi

# ---------------------------------------------------------------------------
# step 13b — sessions 通道 A：源端前缀校验（spec §6.3.1 step 13b）。
# ---------------------------------------------------------------------------
CHECK_SOURCE_ARGS=(
  check-source --state "$SESSIONS_STATE_ARG" --roots "$SESSION_ROOTS" --out-exclude-dir "$STG"
)
if [ -n "$QUARANTINE_SESSIONS" ]; then
  CHECK_SOURCE_ARGS+=(--quarantine "$QUARANTINE_SESSIONS" --quarantine-reason "$QUARANTINE_REASON")
fi
SESSIONS_FAIL=0
SESSIONS_CHECK_RC=0
"$VENV_PY" "$LIB/cass_sessions.py" "${CHECK_SOURCE_ARGS[@]}" 8>&- || SESSIONS_CHECK_RC=$?
# 9>&- 不再需要——写锁已在 step 8 后释放（exec 9>&- 见上）。exit 语义（本 CLI
# 契约 + spec §6.3.1）：0=全净 / 3=有异常文件（healthy 部分仍照常同步，但整次
# 备份最终不发布，判定见 step 13d 之后）/ 其余=内部错误，立即响亮失败。
if [ "$SESSIONS_CHECK_RC" -eq 3 ]; then
  echo "[backup] ALERT: session source-check found anomalous file(s) — excluded from" \
    "this sync, run will not publish (spec §6.3.1)"
  SESSIONS_FAIL=1
elif [ "$SESSIONS_CHECK_RC" -ne 0 ]; then
  fail_incomplete "session check-source failed (rc=$SESSIONS_CHECK_RC)"
fi

# ---------------------------------------------------------------------------
# step 13c/13d — sessions 通道 A：jsonl-only include 过滤 + itemize 解析
# （每个 root 一次 rsync；spec §6.3.1 / 数据流 step 13d）。
#
# ⚠ filter 顺序是硬约束：rsync filter 规则 first-match——`--exclude-from` 必须
# 排在 `--include='*.jsonl'` 之前，否则 `.jsonl` 先命中 include，被 check-source
# 判定异常的文件会绕过 exclude 照常 `--append`（codex R1-P1，沙箱实测：调错顺序
# 时 exclude 点名的 f.jsonl 仍输出 `>f+++++++++`；调对顺序后正确不传）。
#
# SESSION_ROOTS 解析约定（与 cass_sessions.py 的 _parse_roots 同一契约）：冒号
# 分隔对、等号分隔键值——**路径不得含冒号**（无转义机制，会被错误切分）；alias
# 禁 `/`（relpath 按首个 `/` 切 alias）、禁 `|`（下方 sed 用它当定界符）、禁 `,`
# （quarantine 列表按逗号切分）。三个固定 alias（claude-projects /
# codex-sessions / openclaw-agents）是唯一预期形态。
# ---------------------------------------------------------------------------
: > "$STG/transferred.all"
IFS=':' read -ra SESSION_ROOT_PAIRS <<<"$SESSION_ROOTS"
for pair in "${SESSION_ROOT_PAIRS[@]}"; do
  [ -n "$pair" ] || continue
  session_alias="${pair%%=*}"
  session_root_path="${pair#*=}"
  mkdir -p "$DEST/sessions/$session_alias"

  # 整根源目录消失（不是单个文件缺失——单个文件缺失由 13b 的「此刻源端也没有，
  # 跳过比对」处理，见 cass_sessions.check_source）：`rsync ... "$path/" ...`
  # 对不存在的源目录会硬失败（"change_dir ... failed: No such file or
  # directory"），但这不是事故——这个 alias 这一轮压根没有源可同步，NAS 上已有
  # 的内容原样不动即可。13f 的全量回读会把该 alias 下所有 present 记录结转成
  # absent_at_source（Task 11 reviewer 留的验证项）。
  if [ ! -d "$session_root_path" ]; then
    echo "[backup] session root vanished for alias $session_alias — skipping rsync" \
      "(publish-gate will reconcile affected records to absent_at_source)"
    : > "$STG/itemize.$session_alias"
    : > "$STG/transferred.$session_alias"
    continue
  fi

  # DEV-7 故障注入（V12j）：`CASS_BACKUP_FAULT=rewrite-src-mid-rsync` 在这个 root
  # 的 rsync 启动**前**后台起一个延迟 0.2s 的改写子进程，抢在源端第一个 jsonl 文
  # 件的前缀上；配合下面的 `--bwlimit=1` 把这次 rsync 拖慢，让改写真的落在传输
  # 窗口内（真实 TOCTOU，不是测试里事后伪造的数字）。找不到 jsonl 文件（该 root
  # 为空）则什么也不做——不能对不存在的路径 `dd`，那会凭空造出一个坏文件。
  RSYNC_EXTRA_ARGS=()
  if [ "$FAULT" = "rewrite-src-mid-rsync" ]; then
    fault_target="$(find "$session_root_path" -name '*.jsonl' -type f 2>/dev/null | sort | head -n1)"
    if [ -n "$fault_target" ]; then
      (sleep 0.2; printf 'BAD' | dd of="$fault_target" conv=notrunc bs=1 count=3 2>/dev/null) &
      RSYNC_EXTRA_ARGS+=(--bwlimit=1)
    fi
  fi

  rsync -ai --append --prune-empty-dirs "${RSYNC_EXTRA_ARGS[@]}" \
      --exclude-from="$STG/exclude.$session_alias" \
      --include='*/' --include='*.jsonl' --exclude='*' \
      "$session_root_path/" "$DEST/sessions/$session_alias/" 8>&- \
      > "$STG/itemize.$session_alias" \
    || fail_incomplete "sessions rsync failed for root $session_alias"

  "$VENV_PY" "$LIB/cass_sessions.py" parse-itemize --in "$STG/itemize.$session_alias" 8>&- \
      > "$STG/transferred.$session_alias" \
    || fail_incomplete "session itemize parse failed for root $session_alias (fail-closed on unknown line)"
  sed "s|^|$session_alias/|" "$STG/transferred.$session_alias" >> "$STG/transferred.all"
done

# ---------------------------------------------------------------------------
# step 13e — sessions 通道 B：rsync 一返回就更新共享状态（spec §6.3.1 step 13e）
# ——**不等本次备份后续是否成功**：SESSIONS_FAIL=1（13b 判过异常）时健康部分的
# 进度也要落地，最终是否发布由本文件末尾的 SESSIONS_FAIL 判定收口，不在这里。
#
# 两个 DEV-7 故障注入点都发生在 update-state 调用**之前**（brief 逐字约束）：
#   kill-after-sessions-rsync —— 13d 完成、13e 还没跑就 SIGKILL 整个脚本，模拟
#     「NAS 已经变大、清单还没来得及记」的崩溃窗口（V12m 的构造前提）。
#   drop-one-itemize —— 模拟 update-state **自己的**记录漏了一个已传输文件（这
#     是 13e 内部的失败模式，不是「rsync 到底传没传」这件事本身出错）。故障只
#     能作用于 update-state 消费的那份拷贝（`UPDATE_STATE_TRANSFERRED`）——
#     publish-gate 仍必须拿到 `$STG/transferred.all` **未删减的原始版本**当「本
#     轮真传输了什么」的 ground truth，否则 13f 没法把「13e 漏记的已传输文件」
#     （V12k2，该自愈）与「本轮根本没传输、凭空冒出来的陌生文件」（V12f，该
#     --adopt）区分开——这正是 codex R3-P1 binding 的字面要求。
# ---------------------------------------------------------------------------
UPDATE_STATE_TRANSFERRED="$STG/transferred.all"
case "$FAULT" in
  kill-after-sessions-rsync)
    kill -9 $$
    ;;
  drop-one-itemize)
    cp "$STG/transferred.all" "$STG/transferred.for-update-state"
    sed -i '1d' "$STG/transferred.for-update-state"
    UPDATE_STATE_TRANSFERRED="$STG/transferred.for-update-state"
    ;;
esac

"$VENV_PY" "$LIB/cass_sessions.py" update-state \
    --state "$SESSIONS_STATE_PATH" --sessions-root "$DEST/sessions" \
    --transferred "$UPDATE_STATE_TRANSFERRED" 8>&- \
  || fail_incomplete "session update-state (13e) failed"

# ---------------------------------------------------------------------------
# step 13f/13g — sessions 通道 B：发布门全量回读（spec §6.3.1 step 13f/13g）。
# `--transferred` 在这里必须传未删减的 `$STG/transferred.all`（见上方注释）。
# `--adopt`/`--adopt-reason` 只在 `CASS_BACKUP_ADOPT_SESSIONS` 非空时传——cron
# 的 clean allowlist env 传不进它，只有人在 shell 里能触发（同 §5.7 rebaseline
# 的安全属性）。产出的 `.incomplete-$STAMP/sessions.tsv` 供 step 14c 的
# digest.json 取 sha256（Task 13）。
# ---------------------------------------------------------------------------
PUBLISH_GATE_ARGS=(
  --state "$SESSIONS_STATE_PATH" --sessions-root "$DEST/sessions" --roots "$SESSION_ROOTS"
  --transferred "$STG/transferred.all" --out-tsv "$INCOMPLETE_DIR/sessions.tsv"
)
if [ -n "$ADOPT_SESSIONS" ]; then
  PUBLISH_GATE_ARGS+=(--adopt --adopt-reason "$ADOPT_REASON")
fi
"$VENV_PY" "$LIB/cass_sessions.py" publish-gate "${PUBLISH_GATE_ARGS[@]}" 8>&- \
  || fail_incomplete "session publish-gate (13f/13g full-readback) failed"

# ---------------------------------------------------------------------------
# step 14a — manifest 快照完整性门（spec §6 step 14a）：对 .incomplete-$STAMP/
# manifests/ 的每个文件读前 fadvise(DONTNEED) 后核对 manifests.sha256sum。裸
# `sha256sum -c` 不带 fadvise，绕不过「刚写完立刻读，读到的是本地页缓存」这一类
# 问题（同 §6.4 db 读回的机理）——故用 cass_common.sha256_file(fadvise=True) 逐文件
# 核对，而不是直接 shell 出 `sha256sum -c`。任一不符 → fail_incomplete。
#
# **不能只靠 14b**：把某个 manifest 整体换成另一份真实自洽的 manifest，14b 的四项
# （形状/basename/存在/st_size/内容 hash）全过（因为它校验的是替换后那份 manifest
# 自己的 blob）——只有这里的 manifests.sha256sum 能抓出「内容被整体调包」。
# ---------------------------------------------------------------------------
if ! LIB="$LIB" MANIFESTS_DIR="$INCOMPLETE_DIR/manifests" SHA256SUM_FILE="$INCOMPLETE_DIR/manifests.sha256sum" \
    "$VENV_PY" 8>&- - <<'PYEOF'
import os
import pathlib
import sys

sys.path.insert(0, os.environ["LIB"])
import cass_manifest_census  # noqa: E402

ok, problems = cass_manifest_census.verify_manifests_sha256sum(
    pathlib.Path(os.environ["MANIFESTS_DIR"]), pathlib.Path(os.environ["SHA256SUM_FILE"])
)
if not ok:
    for p in problems:
        print(f"[step14a] {p}", file=sys.stderr)
    sys.exit(1)
print("[step14a] manifests.sha256sum verified")
sys.exit(0)
PYEOF
then
  fail_incomplete "step 14a manifest snapshot integrity gate (manifests.sha256sum) failed"
fi

# ---------------------------------------------------------------------------
# step 14b — 发布前闭合检查（spec §6 step 14b）：遍历 manifests，`blob_relative_path`
# 形状 + basename/目录一致性 + NAS 池文件存在 + st_size + fadvise 重算 BLAKE3。
# 路径永远只由 blob_blake3 推导（cass_manifest_census.py 的 blob_path_for），
# blob_relative_path 只做形状/一致性校验，绝不参与文件系统操作（V13d）。
# ---------------------------------------------------------------------------
"$VENV_PY" "$LIB/cass_manifest_census.py" --publish-check \
    --manifests-dir "$INCOMPLETE_DIR/manifests" --blobs-root "$DEST/raw-mirror/v1/blobs" 8>&- \
  || fail_incomplete "step 14b publish-check (manifest/blob pool closure) failed"

# ---------------------------------------------------------------------------
# sessions 通道 A 收尾判定：step 13b 若判定过异常文件（SESSIONS_FAIL=1），
# 无论 13c/13d 是否顺利同步了健康部分，整次备份都不能发布——落
# `INCOMPLETE-$STAMP/` + exit 非零（spec §6.3.1「异常文件排除出本次同步，整次
# 备份 exit 非零、不写 COMPLETE」；codex R4-P1：只断言 exit 非零挡不住「照常
# 发布再非零退出」的骗绿实现，必须真的不留 `COMPLETE`/`cass-*/`）。必须在这里
# ——成功出口清空 TRAP_INCOMPLETE 之前——判断，晚了 fail_incomplete 就没有半
# 成品可回收。
# ---------------------------------------------------------------------------
if [ "$SESSIONS_FAIL" = 1 ]; then
  fail_incomplete "session source-check failed"
fi

# DEV-7 故障注入：sessions.tsv（13g）与 manifests.sha256sum（10）都已落地、
# digest.json 还没写——验证「顺序契约」（spec §6 step 14c）：崩在这里必须留下
# 一个没有 digest.json 也没有 COMPLETE 的 `.incomplete-$STAMP/`（V15e）。
if [ "$FAULT" = "kill-before-digest" ]; then
  kill -9 $$
fi

# ---------------------------------------------------------------------------
# step 14c — 组装 digest.json（spec §6 step 14c / plan「关键接口」字段全集）。
#
# backup_name/generation/prev_backup_name/prev_sidecar_sha256 由本轮 STAMP +
# `cass_common.latest_published(DEST)` 推导（首晚 prev 全部记空串、generation=1）；
# db_sha256/census_sha256/schema_fingerprint/tables/meta_watermarks 复用 step
# 7-9 已经算出的产物（`$STG/gate.json` 与 step 10 的 `$LOCAL_SHA`），不重算；
# sessions_tsv_sha256/manifests_sha256sum_sha256 现算（两个源文件此刻已落
# `$INCOMPLETE_DIR`）。人工通道留痕：rebaseline 直接抄 `gate.json` 里已有的
# `rebaselined_from`/`reason`（`cass_backup_gate.py` 写的，键名同构，spec
# §5.7/§8.3-C2 原文，链校验按它判）；adopt/quarantine/retention_reset 从本轮
# env 读（成对性已在 step 1 校验过，这里只管有没有实际触发）。
#
# **顺序契约**：必须在 `sessions.tsv`（13g）与 `manifests.sha256sum`（10）之后、
# `COMPLETE`（step 15）之前落盘——本段落点正是那个位置。
# ---------------------------------------------------------------------------
if ! LIB="$LIB" DEST="$DEST" STG="$STG" INCOMPLETE_DIR="$INCOMPLETE_DIR" STAMP="$STAMP" \
    DB_SHA256="$LOCAL_SHA" \
    ADOPT_SESSIONS="$ADOPT_SESSIONS" ADOPT_REASON="$ADOPT_REASON" \
    QUARANTINE_SESSIONS="$QUARANTINE_SESSIONS" QUARANTINE_REASON="$QUARANTINE_REASON" \
    RETENTION_RESET="$RETENTION_RESET" RETENTION_RESET_REASON="$RETENTION_RESET_REASON" \
    "$VENV_PY" 8>&- - <<'PYEOF'
import json
import os
import pathlib
import sys

sys.path.insert(0, os.environ["LIB"])
import cass_common  # noqa: E402

dest = pathlib.Path(os.environ["DEST"])
stg = pathlib.Path(os.environ["STG"])
incomplete_dir = pathlib.Path(os.environ["INCOMPLETE_DIR"])
stamp = os.environ["STAMP"]

gate = json.loads((stg / "gate.json").read_bytes())

prev = cass_common.latest_published(dest)
if prev is None:
    generation = 1
    prev_backup_name = ""
    prev_sidecar_sha256 = ""
else:
    prev_name, prev_digest = prev
    generation = prev_digest["generation"] + 1
    prev_backup_name = prev_name
    prev_sidecar_sha256 = cass_common.sha256_file(dest / prev_name / "digest.json")

digest: dict = {
    "backup_name": f"cass-{stamp}",
    "generation": generation,
    "prev_backup_name": prev_backup_name,
    "prev_sidecar_sha256": prev_sidecar_sha256,
    "db_sha256": os.environ["DB_SHA256"],
    "census_sha256": gate["census_sha256"],
    "sessions_tsv_sha256": cass_common.sha256_file(incomplete_dir / "sessions.tsv"),
    "manifests_sha256sum_sha256": cass_common.sha256_file(incomplete_dir / "manifests.sha256sum"),
    "schema_fingerprint": gate["schema_fingerprint"],
    "tables": gate["tables"],
    "meta_watermarks": gate["meta_watermarks"],
}

# rebaseline 留痕：gate.json 的键名已经同构（cass_backup_gate.py 写的），直接抄。
if "rebaselined_from" in gate:
    digest["rebaselined_from"] = gate["rebaselined_from"]
    digest["reason"] = gate["reason"]

if os.environ.get("ADOPT_SESSIONS"):
    digest["adopt_reason"] = os.environ["ADOPT_REASON"]

quarantine_sessions = os.environ.get("QUARANTINE_SESSIONS", "")
if quarantine_sessions:
    digest["quarantined_sessions"] = [
        s.strip() for s in quarantine_sessions.split(",") if s.strip()
    ]
    digest["quarantine_reason"] = os.environ["QUARANTINE_REASON"]

if os.environ.get("RETENTION_RESET"):
    digest["retention_reset"] = True
    digest["retention_reset_reason"] = os.environ["RETENTION_RESET_REASON"]

(incomplete_dir / "digest.json").write_bytes(cass_common.dumps_canonical(digest))
print(f"[step14c] digest.json written: generation={generation} prev={prev_backup_name or '(none)'}")
PYEOF
then
  fail_incomplete "step 14c digest.json assembly failed"
fi

# ---------------------------------------------------------------------------
# step 15 — 发布序列（spec §6 step 15 逐字）：mountpoint 重验 → touch COMPLETE →
# 断言目标不存在 → `mv -T` → 最终断言。任一失败即 `fail_incomplete`/`exit 1`，
# 成功后清空 `TRAP_INCOMPLETE`（发布成功，trap 不再碰它）。
# ---------------------------------------------------------------------------
if [[ "$DEST" == "$NAS_PREFIX"* ]]; then
  mountpoint -q "$SHARE_ROOT" 2>/dev/null \
    || fail_incomplete "NAS mountpoint re-verify failed before publish (step 15)"
fi
sync

touch "$INCOMPLETE_DIR/COMPLETE"
sync

# DEV-7 故障注入：`touch COMPLETE` 已完成、`mv -T` 尚未执行（V15l）——下一轮
# step 4 必须把它当 RECOVERABLE 救援，不能当垃圾清掉（它是完整且已全部校验
# 通过的备份载荷）；同时反例断言朴素「删超 1 天 .incomplete-*」的 glob 会命中它。
if [ "$FAULT" = "kill-after-complete-marker" ]; then
  kill -9 $$
fi

PUBLISHED_DIR="$DEST/cass-$STAMP"
test ! -e "$PUBLISHED_DIR" || fail_incomplete "publish target already exists: $PUBLISHED_DIR"
mv -T "$INCOMPLETE_DIR" "$PUBLISHED_DIR"

# DEV-7 故障注入：`mv -T` 已完成、`sync`/最终断言尚未执行（V15k）——此刻已发布
# 的 `cass-$STAMP/` 必须完好；下一轮 `.incomplete-*` 清理不会误删它（它已经不
# 叫这个名字了，glob 匹配不到）。
if [ "$FAULT" = "kill-after-publish-mv" ]; then
  kill -9 $$
fi

sync
test -f "$PUBLISHED_DIR/COMPLETE" && test ! -e "$INCOMPLETE_DIR" || exit 1
TRAP_INCOMPLETE=""   # 发布成功，trap 不再碰它

# ---------------------------------------------------------------------------
# rebaseline / retention_reset 成功 TG（DEV-2/DEV-3，spec §5.7「rebaseline 的
# 运行即使成功也发 TG」）：这两个都是人工审计事件——脚本自身 curl
# `$CASS_BACKUP_TG_ENV` 的 token/chat_id（source 进子 shell，仓内零密钥）。
# env 文件缺失或 curl 失败 ⇒ 备份已发布、不回滚，但必须 exit 非零提醒人工去
# 查（这条消息没有自动重投机制，漏发等于没人知道）。
# ---------------------------------------------------------------------------
TG_ALERT=0
TG_TEXT="$(DIGEST_PATH="$PUBLISHED_DIR/digest.json" "$VENV_PY" 8>&- - <<'PYEOF'
import json
import os

with open(os.environ["DIGEST_PATH"], "rb") as f:
    d = json.load(f)

lines = []
if "rebaselined_from" in d:
    lines.append(f"rebaseline: replaced {d['rebaselined_from']} — reason: {d.get('reason', '')}")
if d.get("retention_reset"):
    lines.append(f"retention_reset — reason: {d.get('retention_reset_reason', '')}")

if lines:
    print(f"CASS backup {d.get('backup_name', '')}")
    for line in lines:
        print(line)
PYEOF
)"

if [ -n "$TG_TEXT" ]; then
  if ! ( set +u; source "$CASS_BACKUP_TG_ENV" 2>/dev/null; curl -sf -m 10 \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TELEGRAM_CHAT_ID}" --data-urlencode text="$TG_TEXT" >/dev/null ) 9>&- 8>&-
  then
    echo "[backup] ALERT: rebaseline/retention_reset TG notification failed" \
      "(TG env file missing or curl error) — needs manual follow-up. message was: $TG_TEXT"
    TG_ALERT=1
  fi
fi

# ---------------------------------------------------------------------------
# 最终 exit 语义：备份本身已发布成功（cass-$STAMP/ 完好），但两类人工告警
# 若发生仍要 exit 非零（DEV-6 的 RECOVERABLE 救援 / 上面的 TG 发送失败）——
# 告警不能因为「主线成功」就被吞掉。keep-N 轮转（step 16-17）与周校验
# （step 18）是 Task 14/16 的范围，尚未实现。
# ---------------------------------------------------------------------------
if [ "$ALERT_FLAG" = 1 ]; then
  echo "[backup] gate passed but a stale RECOVERABLE-* alert was raised above — exiting non-zero (DEV-6)"
fi
if [ "$ALERT_FLAG" = 1 ] || [ "$TG_ALERT" = 1 ]; then
  exit 1
fi
echo "[backup] published: $PUBLISHED_DIR (keep-N rotation / weekly verify not yet implemented — Task 14/16)"
exit 0
