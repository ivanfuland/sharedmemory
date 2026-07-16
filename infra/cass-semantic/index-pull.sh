#!/usr/bin/env bash
# CASS 增量拉取 entrypoint（Inngest cass-index-daily 调用，镜像 distill/run-bridge.sh）。
# 词法 index 增量 + bge-m3 backfill 循环——**不用整合 `index --semantic`**（撞 upstream stall-abort
# #244/#258：语义阶段不推进 current 计数→watchdog exit 70；积压增量必现，codex P0#1）。
# 失败回滚 scan watermark：fork mid-batch 每 10s 存 scan_start_ts 到 last_scan_ts（mod.rs:12706，
# OOM-loop 规避），若词法摄入被杀于水位推进后/落库前，下轮 delta 扫描会跳过未落库文件→静默漏会话
# （codex P0#2）。本脚本词法失败时回滚水位；SIGKILL 无解但拆分后词法极快、窗口极小。
set -euo pipefail
CANON="${CASS_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
LOCK="$HOME/.local/share/.cass-write.lock"   # 全局写锁，与 full-reingest.sh 共用（codex P1#1）
DB="$CANON/agent_search.db"

emit_fail() { printf '{"ok":false,"error":"%s"}\n' "$1"; exit "${2:-1}"; }

exec 9>"$LOCK"; flock -n 9 || { echo '{"ok":false,"skipped":"another cass write holds lock"}'; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || emit_fail "Infinity down" 2
[ -d "$CANON" ] || emit_fail "canonical missing: $CANON"

# 日志按 run 留存(keep 48 ≈ 2 天)。2026-07-16 教训:单文件每 run 覆盖,深夜两次异常
# 全量回落的日志被后续 run 冲掉,根因无据可查。/tmp/cc-cass-pull.log 保留为指向最新
# run 的 symlink(人类习惯路径;runner.ts 只读 stdout JSON,不读此日志,契约不变)。
LOG_DIR=/tmp/cc-cass-pull-logs
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"
ln -sfn "$RUN_LOG" /tmp/cc-cass-pull.log
ls -1t "$LOG_DIR"/run-*.log 2>/dev/null | tail -n +49 | xargs -r rm --

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
CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" "$BIN" index >"$RUN_LOG" 2>&1 \
  || emit_fail "lexical index failed (watermark rolled back)"
lexical_ok=1   # 词法成功，文件已落库，水位正确——往后失败不回滚（backfill 自带 checkpoint 续跑）

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
      out=$(CASS_DATA_DIR="$CANON" CASS_INFINITY_URL="$URL" "$BIN" models backfill --tier quality --embedder infinity --scheduled --batch-conversations 999999 --json 2>>"$RUN_LOG") \
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
