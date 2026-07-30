#!/usr/bin/env bash
# CASS 增量拉取 entrypoint（Inngest cass-index-daily 调用，镜像 distill/run-bridge.sh）。
# 词法 index 增量 + bge-m3 backfill 循环——**不用整合 `index --semantic`**（撞 upstream stall-abort
# #244/#258：语义阶段不推进 current 计数→watchdog exit 70；积压增量必现，codex P0#1）。
# 失败回滚 scan watermark：fork mid-batch 每 10s 存 scan_start_ts 到 last_scan_ts（mod.rs:12706，
# OOM-loop 规避），若词法摄入被杀于水位推进后/落库前，下轮 delta 扫描会跳过未落库文件→静默漏会话
# （codex P0#2）。本脚本词法失败时回滚水位；SIGKILL 无解但拆分后词法极快、窗口极小。
set -euo pipefail
# 该逃生阀只属于下面的词法命令；拒绝继承父进程的同名值污染 semantic/backfill。
unset CASS_SKIP_PREFLIGHT_COUNT_TOTAL_MESSAGES
CANON="${CASS_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
LOCK="$HOME/.local/share/.cass-write.lock"   # 全局写锁，与 full-reingest.sh 共用（codex P1#1）
DB="$CANON/agent_search.db"

emit_fail() { printf '{"ok":false,"error":"%s"}\n' "$1"; exit "${2:-1}"; }

exec 9>"$LOCK"; flock -n 9 || { echo '{"ok":false,"skipped":"another cass write holds lock"}'; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || emit_fail "Infinity down" 2
[ -d "$CANON" ] || emit_fail "canonical missing: $CANON"

# 日志按 run 留存(keep 48 ≈ 2 天,含本次)。2026-07-16 教训:单文件每 run 覆盖,深夜两次
# 异常全量回落的日志被后续 run 冲掉,根因无据可查。/tmp/cc-cass-pull.log 保留为指向最新
# run 的 symlink(人类习惯路径;runner.ts 只读 stdout JSON,不读此日志,契约不变)。
# codex R1/R2 加固:#2 目录 0700+拒 symlink / #3 文件名带 pid 防同秒重试覆盖证据
# / #5 先建本次再轮转,真 keep-48 / R2-Part1#1 轮转用 find(空目录 rc=0 天然安静,
# 真实错误——权限/IO——照常非零 fail-loud,不像 `ls||true` 会把一切吞掉)。
LOG_DIR="${CASS_PULL_LOG_DIR:-/tmp/cc-cass-pull-logs}"   # env 缝隙仅供测试隔离(不劫持生产 symlink)
if [ -L "$LOG_DIR" ]; then emit_fail "log dir is a symlink: $LOG_DIR"; fi
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
RUN_LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S)-$$.log"
: > "$RUN_LOG"
ln -sfn "$RUN_LOG" "$LOG_DIR/latest.log"
[ -n "${CASS_PULL_LOG_DIR:-}" ] || ln -sfn "$RUN_LOG" /tmp/cc-cass-pull.log
# NUL 安全轮转(codex R3-P1:含空格路径下 \n+xargs 会拆词误删):全链 -z。
find "$LOG_DIR" -maxdepth 1 -name 'run-*.log' -printf '%T@\t%p\0' | sort -rzn | cut -z -f2- | tail -zn +49 | xargs -0r rm --

# 快照 scan watermark；trap 在词法未完成（清退或 SIGTERM 超时）时回滚（SIGKILL 无法 trap）。
WM=$(sqlite3 "$DB" "SELECT key||char(9)||value FROM meta WHERE key LIKE 'last_scan_ts:%';" 2>/dev/null || echo "")
lexical_ok=0
restore_wm() {
  [ "$lexical_ok" = 1 ] && return 0    # 词法已成功→水位正确，不回滚（文件已落库）
  [ -n "$WM" ] || return 0
  printf '%s\n' "$WM" | while IFS=$'\t' read -r k v; do
    [ -n "$k" ] && sqlite3 "$DB" "UPDATE meta SET value='$(printf '%s' "$v" | sed "s/'/''/g")' WHERE key='$k';" 2>/dev/null
  done
}
trap restore_wm EXIT

# 1) 词法 + DB 增量 index（不带 --semantic）。失败→trap 回滚水位。
# stall abort 放宽到 1500s。CASS 的 phase-2 判据是「连续零进展 N 秒即 exit 70」，而首个 ingest
# commit 前有一段前置开销（单线程 CPU 热循环，根因未定位）。它持续增长：2026-07-29 实测
# 05:00 轮 337.7s → 20:00 轮 429s（当日峰值 440s），14 轮线性拟合 4.01 s/小时（R²=0.765），
# 最近区间更快（约 7–9 s/小时）。前值 600（PR #66 止血）按此增速一两天内必被再次击穿——
# 300s 时代它已产生过 false positive：2026-07-29 的 01:00 与 02:00 两个整点 cron 及各自
# 约 5 分钟后的 retry，四次尝试全被掐，wrapper 包装成 lexical index failed。
#
# 选 1500 的预算依据（2026-07-29 推 connector 水位后的新样本；旧样本 post max 2699s 属
# 水位卡死期、重扫窗口 12 天的过期数据，勿再引用）：stall abort 是零进展计时器、不是整轮
# 时限，真实总耗时 = 前置 + 首个 commit 之后的工作；后者推水位后实测 max 1462s
# （13:00–20:00 八轮 1023–1462s）。三条边界按最坏观测反推：
#   cron 3600s 周期（软）  → 阈值 ≤ 3600 − 1462 = 2138s，超了下一轮在 Inngest 排队，不坏数据
#   夜备等锁 deadline（硬） → 完整式是「启动延迟 d + 阈值 + post ≤ 3900」（夜备 02:50 CST 起
#     等锁 flock -w 900，锁不到 FATAL）。整点准时启动（d≈0，实测 cron 延迟秒级）时阈值上限
#     = 3900 − 1462 = 2438s；但调度侧 admission 允许 cron 延迟至 90 分钟仍放行，延迟 d > 938s
#     的 02:00(CST) 轮若再撞上最坏 pre+post，当晚夜备确定性 FATAL。**该残余风险 600 时代已存在
#     （安全 d 上限 1838s），1500 把窗口收窄到 938s**——而 d > 15 分钟本身即前轮超时/积压信号，
#     应触发预算重估。根治要么收窄 admission 窗要么夜备错峰，属调度侧另立改动。
#   runner SIGKILL 7200s（硬）→ 阈值 ≤ 7200 − 1462 = 5738s，超了进程组被杀、上面的水位回滚 trap 失效
# 注意：这些是按 8 轮实测 post max 反推的经验预算，不是运行时强制——post 回升（罕见的全量
# backfill 回落、结构探针 150s 顶格等）就要重估，由每小时验收观测兜底。
# 取 1500：d≈0 时距 cron 软上限 638s、距夜备 FATAL 线 938s；按 4–9 s/小时增速约买 5–11 天。
# 取 1500 而非 0：词法段会推进 current 计数，wedge 保护应保留
# （semantic 段用 0 是 report-only，因为它本就不推进计数、watchdog 对它无意义）。
#
# ⚠ 止血不是根治：继续抬阈值是死路，每抬一次买几天，且很快撞上夜备与 SIGKILL 两堵硬墙。
# 根因——那段长时间无应用日志、无磁盘读的单线程 CPU 热循环在算什么——仍未定位，正在抓栈。
CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" CASS_SKIP_PREFLIGHT_COUNT_TOTAL_MESSAGES=1 \
  CASS_INDEX_STALL_ABORT_SECS=1500 \
  "$BIN" index >"$RUN_LOG" 2>&1 \
  || emit_fail "lexical index failed (watermark rolled back)"
lexical_ok=1   # 词法成功，文件已落库，水位正确——往后失败不回滚（backfill 自带 checkpoint 续跑）

# 1b) 结构探针(2026-07-16 fsqlite 丢写事故哨兵):两类已知损坏签名的小时级检查(秒级只读)。
#     命中即 fail-loud——1 小时内知道,而不是等 00:30 夜备五腿门(最坏 24h)。
#     它同时是「≥0.1.16 再损坏 → 全迁 rusqlite」触发线的自动哨兵。
#     codex R2-F1:外层再包 150s 硬超时(探针内部每查询另有 60s),绝不拖小时窗。
#     codex R2-F3:日志追加 best-effort(磁盘满不得抢在 emit_fail 前死);JSON 转义先反斜杠后引号。
PROBE="$(dirname "${BASH_SOURCE[0]}")/structure-probe.sh"
if probe_out=$(timeout --kill-after=10 150 bash "$PROBE" "$DB" 2>&1); then
  :
else
  echo "$probe_out" >> "$RUN_LOG" 2>/dev/null || true
  # 全部控制字符(\000-\037)一律清除(codex R3-P2:TAB/CR 也会打破末行 JSON 契约;换行已先转 ';')
  emit_fail "structure probe: $(printf '%s' "$probe_out" | head -2 | tr '\n' ';' | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')" 3
fi

# 2) bge-m3 语义。先判语义是否已与 DB 一致（manifest fp 的 conv/msg == DB）——一致则跳过，
#    避免无新内容时每日 backfill 空转重 walk 整语料（~12min CPU；codex 之外的效率加固）。
pub=""

# manifest 指纹 == DB 指纹？CASS 的 `content-v1:<会话总数>:<max(conversations.id)>:<max(messages.id)>`
# （注意第三段是 **max(messages.id)**，不是 COUNT(*)——两者只在 id 稠密无删除时相等；
#  原实现拿 COUNT(*) 比，一旦有删除就永久失配 → 每小时触发一次全量重建。）
sem_matches() {
  python3 -c "
import json,sqlite3
try:
    m=json.load(open('$CANON/vector_index/semantic_manifest.json'))['quality_tier']
    c=sqlite3.connect('file:$DB?mode=ro',uri=True)
    nc=c.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
    mc=c.execute('SELECT COALESCE(MAX(id),0) FROM conversations').fetchone()[0]
    mm=c.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]; c.close()
    want=f'content-v1:{nc}:{mc}:{mm}'
    print('yes' if (m.get('ready') and m.get('db_fingerprint')==want) else 'no')
except Exception: print('no')
" 2>/dev/null || echo no
}

if [ "$(sem_matches)" = yes ]; then
  echo "semantic 已与 DB 一致，跳过"; pub=True
else
  # 2a) **优先增量追加**（秒级）：`index --semantic --watch-once` 走 TargetedSemanticWatchOnceMode::
  #     AppendToExisting，只嵌 `message_id > artifact.max_message_id` 的新消息，向 FSVI 的 WAL 追加。
  #     前置：① fork 的 `semantic_tier_for_embedder_id("bge-m3") -> Quality`（否则 bail：unknown embedder tier）；
  #           ② `semantic_artifact_is_append_only_prefix` 成立（无删除、id 单调增）。
  #     watch-once **必须**给至少一个真实路径才会启用该分支；语义选择只看 DB 指纹，这个路径仅作触发器
  #     （它会被顺带 ingest 一次，取已在库的最新会话源文件即可，等价 no-op）。
  #     实测：5348 doc / 16s，替代原先 176669 doc / ~2h 的全量重建。
  SENTINEL=$(sqlite3 "file:$DB?mode=ro" \
    "SELECT source_path FROM conversations WHERE source_path IS NOT NULL ORDER BY id DESC LIMIT 20;" 2>/dev/null \
    | while read -r p; do [ -f "$p" ] && { printf '%s' "$p"; break; }; done)
  if [ -n "$SENTINEL" ]; then
    if CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" CASS_INDEX_STALL_ABORT_SECS=0 \
         "$BIN" index --semantic --embedder infinity --watch-once "$SENTINEL" >>"$RUN_LOG" 2>&1; then
      # 追加"成功退出"不等于"追上了"——必须用指纹复核，否则会把陈旧索引当新的
      [ "$(sem_matches)" = yes ] && { pub=True; echo "semantic 增量追加完成"; }
    fi
    [ "$pub" = "True" ] || echo "semantic 增量追加不可用，回落全量 backfill" >&2
  else
    echo "semantic 找不到可用的 watch-once 触发路径，回落全量 backfill" >&2
  fi

  # 2b) 回落：全量重建（前缀被破坏 / artifact 缺失 / 追加失败时的唯一出路）
  if [ "$pub" != "True" ]; then
    prev=-1
    for i in $(seq 1 200); do
      # env -u:backfill 必须拿不到 stall abort 阈值——真实 runner 整份继承进程环境,
      # 不显式清除的话父环境同名值会漏进来(接线契约由 wiring 测试带毒父环境锁定)。
      out=$(CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" env -u CASS_INDEX_STALL_ABORT_SECS "$BIN" models backfill --tier quality --embedder infinity --scheduled --batch-conversations 999999 --json 2>>"$RUN_LOG") \
        || emit_fail "backfill errored"
      read -r pub off < <(printf '%s' "$out" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('published'),d.get('last_offset'))" 2>/dev/null) \
        || emit_fail "backfill parse"
      [ "$pub" = "True" ] && break
      [ "$off" = "$prev" ] && emit_fail "backfill stalled at offset $off"
      prev=$off
    done
    [ "$pub" = "True" ] || emit_fail "bge-m3 not published"
  fi
fi

# 3) 末行 JSON 报告（conv/msg 计数供观测增量进展）
conv=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM conversations' 2>/dev/null || echo -1)
msg=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM messages' 2>/dev/null || echo -1)
ready=$(python3 -c "import json;print(str(json.load(open('$CANON/vector_index/semantic_manifest.json'))['quality_tier'].get('ready')).lower())" 2>/dev/null || echo unknown)
printf '{"ok":true,"conversations":%s,"messages":%s,"semantic_ready":"%s"}\n' "$conv" "$msg" "$ready"
