#!/usr/bin/env bash
# everos-prod nightly backup — tar 整个实例基目录(root/ + PIN + server.log + env.redacted)到 NAS,keep-N 轮转。
# “省钱备份”性质(spec §3 backup 单元):CASS 可重炼,卡/索引可重建;tar 活目录接受
# crash-consistent 级一致性(sqlite WAL/lancedb 撕裂概率低且可弃)。LOUD on trouble。
# 明文 env 排除、脱敏副本顶替(KEY/TOKEN/SECRET 值抹掉,拓扑保留):NAS 自动同步云端,
# 凭证不出机器是既有备份体系先例(gbrain/cass 均零 secrets);restore 从 env.redacted 重建 env。
# 顺带检测嵌套 SKILL.md(上游「skill 名含 /」bug 的检测面):期望布局
# skills/<skill_dir>/SKILL.md;更深的 SKILL.md = 名字带 / 被展开成子目录 → 打标记行告警。
set -euo pipefail
ENVSH="${EVEROS_PROD_ENV:?set EVEROS_PROD_ENV to the private env file}"
# shellcheck disable=SC1090
source "$ENVSH"
ROOT="${EVEROS_PROD_ROOT:?env 缺 EVEROS_PROD_ROOT}"
DEST="${EVEROS_BACKUP_DEST:?env 缺 EVEROS_BACKUP_DEST}"
KEEP="${EVEROS_BACKUP_KEEP:-7}"
BASE="$(dirname "$ROOT")"          # ~/everos-prod
BASENAME="$(basename "$BASE")"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOCKFILE="${TMPDIR:-/tmp}/everos-backup.lock"

# guard 0: single-flight(照 backup-gbrain.sh)
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "[backup] another everos backup holds the lock; skipping"; exit 0
fi
# guard 1: KEEP 必须正整数(0/负数/非数字会让轮转比较在 set -e 下滑进 rm 全删)
if ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
  echo "[backup] FATAL: EVEROS_BACKUP_KEEP must be a positive integer, got '$KEEP'"; exit 1
fi
# guard 2: DEST 在 NAS 前缀下时,share 必须真挂载(否则静默备到本地盘)
NAS_PREFIX="$HOME/nas/"
if [[ "$DEST" == "$NAS_PREFIX"* ]]; then
  rest="${DEST#"$NAS_PREFIX"}"; share="${rest%%/*}"; SHARE_ROOT="$NAS_PREFIX$share"
  ls "$SHARE_ROOT" >/dev/null 2>&1 || true
  if ! mountpoint -q "$SHARE_ROOT" 2>/dev/null; then
    echo "[backup] FATAL: NAS share not mounted at $SHARE_ROOT — refusing local-disk backup"; exit 1
  fi
fi
[ -d "$ROOT" ] || { echo "[backup] FATAL: EVEROS_PROD_ROOT missing: $ROOT"; exit 1; }
mkdir -p "$DEST"

# 脱敏 env 副本(值级抹除,变量名与拓扑保留——restore 据此重建 env,只需重填 key)。
# env 缺失 = 备份不可恢复,fail-loud(R2-P2-2:不许出"没有 env.redacted 的绿备份");
# 先写 .tmp 再 mv,防半写副本;tar 内强断言 redacted 存在,防 stale/漏打。
[ -f "$BASE/env" ] || { echo "[backup] FATAL: $BASE/env missing; cannot create restorable backup"; exit 1; }
# 标记词网放宽到常见凭证命名 + 容忍 export 前缀(T6 评审 Important:KEY|TOKEN|SECRET 三词漏
# PASSWORD/AUTH 类;env 命名契约见 plan env 模板头注,双保险)
sed -E 's/^(export[[:space:]]+)?([A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|CREDENTIAL)[A-Za-z0-9_]*)=.*/\1\2=REDACTED/' \
  "$BASE/env" > "$BASE/env.redacted.tmp"
mv "$BASE/env.redacted.tmp" "$BASE/env.redacted"

OUT="$DEST/everos-$STAMP.tar.gz"
# 中途被杀不留孤儿(codex PR58-P2);INT/TERM 必须显式退出——单一 handler 挂三信号会吞掉
# 终止信号,让"被 kill 的备份"跑完并返回 0(codex R2 信号探针实证)。
cleanup() { rm -f "$OUT.tmp" "$BASE/env.redacted.tmp"; }
trap cleanup EXIT
trap 'cleanup; trap - EXIT; exit 130' INT
trap 'cleanup; trap - EXIT; exit 143' TERM
# tar 活目录:feeder 24/7 写实例文件,读中撞写时 GNU tar 报 "file changed as we read it"
# 并 exit 1——crash-consistent 备份的设计内警告(见文件头注),不是失败;exit ≥2 才是真错。
# 2026-07-16 实证:不区分会让备份被随机判死(试点补跑撞上 worker 写入,event 触发 FAIL)。
_tar_rc=0
tar -C "$(dirname "$BASE")" --exclude="$BASENAME/env" --exclude="$BASENAME/env.redacted.tmp" \
  -czf "$OUT.tmp" "$BASENAME" || _tar_rc=$?
if [ "$_tar_rc" -ge 2 ]; then
  echo "[backup] FATAL: tar create failed rc=$_tar_rc"; exit 1
elif [ "$_tar_rc" -eq 1 ]; then
  echo "[backup] WARN: tar exit 1 (file changed as we read it) — crash-consistent 设计内,继续"
fi
tar -tzf "$OUT.tmp" > /dev/null            # 读回验证:archive 可枚举才算写成
if tar -tzf "$OUT.tmp" | grep -qx "$BASENAME/env"; then   # 防呆:明文 env 绝不入 tar
  rm -f "$OUT.tmp"; echo "[backup] FATAL: plaintext env leaked into archive"; exit 1
fi
tar -tzf "$OUT.tmp" | grep -qx "$BASENAME/env.redacted" || {   # 防呆:可恢复性载体必须在
  rm -f "$OUT.tmp"; echo "[backup] FATAL: env.redacted missing from archive"; exit 1; }
mv "$OUT.tmp" "$OUT"
echo "[backup] wrote $OUT ($(du -h "$OUT" | cut -f1))"

# 轮转 keep-N(只匹配本脚本产物模式)
mapfile -t _everos_backups < <(ls -1dt "$DEST"/everos-*.tar.gz 2>/dev/null)
for old in "${_everos_backups[@]:$KEEP}"; do
  rm -- "$old"; echo "[backup] rotated out $old"
done

# 嵌套 SKILL.md 检测(告警不拦备份:标记行给 Inngest 包装转 TG)
deep="$(find "$ROOT" -path '*/skills/*/*/SKILL.md' 2>/dev/null | head -20 || true)"
if [ -n "$deep" ]; then
  echo "SKILL_NEST_ALERT: skill 名含 / 的上游 bug 产生了嵌套 SKILL.md(不入检索面):"
  echo "$deep"
fi
echo "[backup] done"
