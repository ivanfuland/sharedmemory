#!/usr/bin/env bash
# CASS 单库新世界全新重摄入 v2：备份老库 → fork 全量 index(JSONL→DB+词法+bge-m3语义,DB向量域)
# 进临时新目录 → serving readiness(status --json) → 召回门≥0.55(四分退出码) → 原子 swap 进规范路径。
# 零迁移（不碰老库）。幂等 + flock + fail-loud。SWAP 仅在召回门 PASS 后执行。
#
# v2 变更(适配单库新世界，骨架/锁/备份/swap 逻辑原样保留，只换新世界不成立的判据)：
#   - 词法/语义判据统一改读 `status --json`（index.exists / db_vector_domain），脚本内不再直读
#     sqlite3 或看 index/ 目录大小 —— 新世界词法在 DB 内 FTS5(fts_lex)，语义在 DB 向量域，无 index/ 目录。
#   - 语义摄入改用 `index --semantic --embedder infinity`（=db_vector_catchup：genesis 播种→洞驱动
#     嵌入→七项审计→事务内切换激活），不用旧世界 `models backfill --scheduled --batch-conversations` 循环。
#   - 召回门退出码四分（0 过/1 回归/2 前置/3 陈旧），不再一刀切 `|| FAIL`。
#   - 备份文件名改读 `$BIN --version` 动态生成，不再硬编码 0.6.13。
#   - 新增硬前置：候选二进制版本 + status schema 断言、NEW 目录必须为空、sources 快照留痕。
#
# v2.2 变更：摄入两遍改为等 --full 自然退出（去掉「稳定即 kill」探停，它会误杀第二遍），并断言收尾汇总存在。
#
# v2.1 变更：
#   - MIRROR_HOME=<假HOME目录> 时先物化镜像跑一遍摄入（补老库源文件已被本机清理、只剩 raw-mirror
#     归档的会话），水位重置后再用真 HOME 跑第二遍补差；不设时行为与 v2 完全一致，只跑一遍。
#   - 两遍法之间必须做水位重置：CASS 按 connector 增量水位扫描，扫过假 HOME 会把 last_scan_ts
#     推进到镜像那次的时间点，第二遍真 HOME 增量时会把早于该水位的真实新会话当"已扫过"漏摄
#     （根因见 `~/projects/cc-cass-w1-artifacts/W1_ARTIFACTS/w1c-watermark-bleed-rootcause.md`）。
#   - conv_count 探针改读 `stats --json` 顶层 conversations，不读 `status --json` 的
#     `database.conversations`：候选二进制在大库上 `include_counts` 受
#     `STATUS_COUNT_SCAN_MAX_DB_BYTES` 门控，超阈值 `counts_skipped=true` → 该字段 null →
#     `or 0` 吃成 0，稳定判据永远不满足；`stats --json` 无此门控，返回真数。
#
# 用法：bash full-reingest.sh            # 跑 backup→index→gate（到 gate 停，打印 ready-to-swap）
#       SWAP=1 bash full-reingest.sh     # gate PASS 后再带 SWAP=1 跑一次，执行 swap
#       MIRROR_HOME=/path bash full-reingest.sh   # 先跑镜像 HOME 一遍补差，再跑真 HOME 一遍
# 中断重入：NEW 目录非空时脚本硬拒跑（见下方硬前置②）；`rm -rf "$NEW"` 后重跑（fresh）。
# 摄入用 --full 探停(external_id 去重幂等)。
set -euo pipefail
CANON="${CANON_DATA_DIR:-$HOME/.local/share/coding-agent-search}"
NEW="${NEW_DATA_DIR:-$CANON.new}"
URL="${CASS_INFINITY_URL:-http://127.0.0.1:7997}"
BIN="${CASS_BIN:-$HOME/.local/bin/cass-infinity}"
RECALL="${RECALL_RUN:-$HOME/projects/cc-workspace/docs/projects/shared-memory/recall-regression/run.py}"
LOCK="${CASS_WRITE_LOCK:-$HOME/.local/share/.cass-write.lock}"   # 全局写锁，与 index-pull.sh 共用（codex P1#1：防 pull 在 swap 窗口写 canonical）；可覆盖以便沙盒干跑不碰生产锁
MIN_VERSION="0.6.17"   # 单库新世界（db_vector_domain / index --semantic）的最低版本线

exec 9>"$LOCK"; flock -n 9 || { echo "another cass write holds lock (reingest/pull)"; exit 0; }
curl -sf -m5 "$URL/health" >/dev/null || { echo "FAIL: Infinity down"; exit 2; }

# 0a) 硬前置①：候选二进制必须是「单库新世界」版本，老二进制直接拒跑
BIN_VERSION="$("$BIN" --version 2>/dev/null | awk '{print $2}')"
[ -n "$BIN_VERSION" ] || { echo "FAIL: 读不到 $BIN --version"; exit 1; }
if [ "$(printf '%s\n%s\n' "$MIN_VERSION" "$BIN_VERSION" | sort -V | head -1)" != "$MIN_VERSION" ]; then
  echo "FAIL: $BIN 版本 $BIN_VERSION < $MIN_VERSION，拒跑（老二进制没有 db_vector_domain / index --semantic）"
  exit 1
fi
PROBE_DIR="$(mktemp -d)"
trap 'rm -rf "$PROBE_DIR"' EXIT
CASS_DATA_DIR="$PROBE_DIR" "$BIN" status --json 2>/dev/null \
  | python3 -c "import sys,json; sys.exit(0 if 'db_vector_domain' in json.load(sys.stdin) else 1)" \
  || { echo "FAIL: $BIN status --json 缺 db_vector_domain 键，非单库新世界二进制"; exit 1; }
echo "前置①通过：$BIN 版本 $BIN_VERSION ≥ $MIN_VERSION，status schema 含 db_vector_domain"

# 0b) 硬前置②：NEW 目录必须不存在或为空 —— 新二进制绝不对旧世界残留库跑增量（PR2-R2-2 死亡判据）
if [ -e "$NEW" ] && [ -n "$(ls -A "$NEW" 2>/dev/null)" ]; then
  echo "FAIL: $NEW 已存在且非空——可能是旧世界残留库，新二进制绝不能对它跑增量。请核实内容后手动清理再重跑"
  exit 1
fi

# 0c) sources 快照留痕（只读，不改配置；语料排除项由二进制+源配置负责，这里只存证）
"$BIN" sources list --json > /tmp/cc-reingest-sources-snapshot.json 2>&1 \
  || echo "警告：sources list 快照失败（不阻断，继续）"
echo "sources 快照 -> /tmp/cc-reingest-sources-snapshot.json"

# 1) 备份老库（回滚后路；幂等：当日已备份则跳过）
BK="$CANON.$BIN_VERSION.bak.$(date +%Y%m%d)"
if [ -d "$CANON" ] && [ ! -e "$BK" ]; then cp -a "$CANON" "$BK"; echo "backed up -> $BK"; else echo "backup skip ($BK or no canon)"; fi

# 2a) 摄入 + 词法。⚠ 绝不用 `index --full` 一把梭跑到底：finalize 阶段的并行 Tantivy/frankensqlite FTS5
#     归并在内存压力/大语料下有史可查的死锁族——upstream #244（`index --full` 25h finalize 卡死，
#     2026-05 修复）的姊妹问题 #305（`index --semantic` 同一 finalize 病灶：0.6.16 复现、本脚本最低版本线
#     0.6.17 修过、但 0.6.22 在更大语料上再次复现）说明这类死锁在该版本族没被彻底根除，只是触发阈值变了。
#     小语料干跑里 `--full` 顺利秒退不能当作已解决的证据（干跑语料太小，压不出这个 bug），故仍分两步：
#       ① `--full` 只借来「摄入 JSONL→DB」，探到 conv 数稳定即停（不等它可能撞上 finalize 死锁）
#       ② 词法走 `index --force-rebuild`（候选二进制自报 lexical_strategy_reason=
#          "force_rebuild_uses_readonly_authoritative_canonical_db_rebuild_only"，证实这是与 --full
#          finalize 不同的轻量只读重建路径，0.6.17 实测仍存在且可用）
mkdir -p "$NEW"
export CASS_INDEX_STALL_ABORT_SECS=0   # stall 检测降为只报告(留日志)；死锁靠下面"探停"处理，不靠它中止

# conv_count()：探针改读 stats --json 顶层 conversations（探针为何用 stats 不用 status，见头部注释）
conv_count() {
  CASS_DATA_DIR="$NEW" "$BIN" stats --json 2>/dev/null \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('conversations') or 0)" 2>/dev/null \
    || echo 0
}

# ① 摄入：--full 后台跑，轮询 conv 数稳定即停 --full（探针 conv_count 见上；脚本内不直读 sqlite3）
#    抽成函数 ingest_pass LABEL，供 MIRROR_HOME 两遍法复用；单遍行为与 v2 完全一致，只多了 LABEL 分日志。
ingest_pass() {
  local LABEL="$1"
  rm -f "$NEW/index-run.lock" "$NEW/index-run.lock.meta" 2>/dev/null || true
  CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --full --json >"/tmp/cc-reingest-ingest-$LABEL.log" 2>&1 &
  local ING=$!
  # v2.2：不再「稳定即 kill」——第二遍(live)对已入库文件重扫时 conv 数长时间不变，探停会在 24s 内误杀
  #        --full（2026-09-03 08:22 生产实证：live 遍 0 新会话、日志止于 progress 事件）；mirror 遍同样被截断。
  #        0.6.17 在本语料上 --full 可自然退出（同日首跑 896s 收尾汇总完整），改为等自然退出 + 收尾汇总断言。
  local cur ncv ing_rc=0
  while kill -0 "$ING" 2>/dev/null; do
    sleep 60
    cur=$(conv_count)
    echo "[$LABEL] 进行中: $cur conv $(date +%H:%M:%S)"
  done
  wait "$ING" || ing_rc=$?
  [ "$ing_rc" = "0" ] || { echo "FAIL: index --full[$LABEL] 退出码 $ing_rc"; tail -3 "/tmp/cc-reingest-ingest-$LABEL.log"; exit 1; }
  grep -q '"total_conversations"' "/tmp/cc-reingest-ingest-$LABEL.log" \
    || { echo "FAIL: index --full[$LABEL] 日志无收尾汇总（可能被外部中断）"; exit 1; }
  ncv=$(conv_count)
  [ "${ncv:-0}" -gt 0 ] || { echo "FAIL: 摄入无数据[$LABEL]"; exit 1; }
  echo "摄入完成[$LABEL]: $ncv conv（--full 自然退出，收尾汇总在）"
  rm -f "$NEW/index-run.lock" "$NEW/index-run.lock.meta" 2>/dev/null || true   # 清被杀 --full 的残留锁
}

# 水位重置：跨 HOME 两遍法之间必须做（C3 实证的跨 HOME 串味修法，exec20 交接件原句；为何要重置见头部注释）
reset_watermarks() {
  python3 - "$NEW/agent_search.db" <<'PYEOF'
import sqlite3, sys

db = sys.argv[1]
con = sqlite3.connect(db, timeout=30)
con.execute("PRAGMA busy_timeout=30000")


def dump(label):
    print(f"水位{label} ({db}):")
    for row in con.execute(
        "SELECT key, value FROM meta WHERE key='last_scan_ts' OR key LIKE 'last_scan_ts:connector:%'"
    ):
        print(" ", row)


dump("重置前 SELECT")
con.execute(
    "UPDATE meta SET value='0' WHERE key='last_scan_ts' OR key LIKE 'last_scan_ts:connector:%'"
)
con.commit()
dump("重置后 SELECT")
con.close()
PYEOF
}

# MIRROR_HOME 非空：先物化假 HOME 跑一遍摄入补老库源文件已被清理但仍在库里的差量（archive 覆盖），
# 水位重置后再用真 HOME 跑第二遍补差；为空则行为与 v2 完全一致，只跑一遍。
MIRROR_HOME="${MIRROR_HOME:-}"
if [ -n "$MIRROR_HOME" ]; then
  { [ -d "$MIRROR_HOME" ] && { [ -d "$MIRROR_HOME/.claude" ] || [ -d "$MIRROR_HOME/.codex" ]; }; } \
    || { echo "FAIL: MIRROR_HOME=$MIRROR_HOME 不存在或不含 .claude/.codex 子目录，拒跑（防空树静默漏摄）"; exit 1; }
  echo "MIRROR_HOME=$MIRROR_HOME 文件数: $(find "$MIRROR_HOME" -type f | wc -l)"
  HOME="$MIRROR_HOME" ingest_pass mirror
  reset_watermarks
  ingest_pass live
else
  ingest_pass live
fi

# ② 词法：force-rebuild（从 DB 重建 Tantivy，绕 --full finalize 死锁族；判据改读 status --json）
CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --force-rebuild --json >/tmp/cc-reingest-lexical.log 2>&1 \
  || { echo "FAIL: 词法 force-rebuild"; tail -3 /tmp/cc-reingest-lexical.log; exit 1; }
lex_ok=$(CASS_DATA_DIR="$NEW" "$BIN" status --json 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('1' if d.get('index',{}).get('exists') else '0')")
[ "$lex_ok" = "1" ] || { echo "FAIL: 词法索引未就绪(status.index.exists=false)"; exit 1; }
echo "词法建好: status.index.exists=true"

# 2b) 语义：DB 向量域（新世界）。`index --semantic --embedder infinity` = db_vector_catchup
#     (genesis 播种→洞驱动嵌入→七项审计→事务内切换激活)，一次调用跑完；
#     `models backfill --tier quality --embedder infinity` 指向同一 catchup，二选一，这里选前者。
sem_out=$(CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" "$BIN" index --semantic --embedder infinity --json 2>>/tmp/cc-reingest-semantic.err) \
  || { echo "FAIL: 语义摄入 index --semantic 出错"; tail -5 /tmp/cc-reingest-semantic.err; exit 1; }
printf '%s' "$sem_out" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ok = bool(d.get('success')) and bool(d.get('activated'))
print('index --semantic: success=%s activated=%s' % (d.get('success'), d.get('activated')))
sys.exit(0 if ok else 1)
" || { echo "FAIL: 语义摄入未 activated（success/activated 字段不满足）"; exit 1; }

# 3) serving 完整性（status --json 为唯一判据 oracle：词法 + 语义 DB 向量域）
CASS_DATA_DIR="$NEW" "$BIN" status --json > /tmp/cc-reingest-status.json 2>&1
python3 -c "
import json
d = json.load(open('/tmp/cc-reingest-status.json'))
idx = d.get('index', {})
dv = d.get('db_vector_domain', {})
ok = (idx.get('exists') is True
      and dv.get('active') is True
      and dv.get('audit_status') == 'passed'
      and dv.get('error') is None
      and (dv.get('embedded_count') or 0) > 0)
import sys
sys.exit(0 if ok else 1)
" || { echo "FAIL: serving dir 不完整（index.exists / db_vector_domain 未同时就绪）"; cat /tmp/cc-reingest-status.json; exit 1; }
echo "serving 完整性通过：index.exists=true, db_vector_domain.active=true/audit=passed"

# 4) 召回门 ≥0.55（退出码四分：0 过/1 回归/2 前置/3 陈旧；非 0 一律不 swap，但只有 1 是真回归）
set +e
CASS_DATA_DIR="$NEW" CASS_INFINITY_URL="$URL" python3 "$RECALL" "$BIN"
rc=$?
set -e
case "$rc" in
  0) echo "GATE PASSED." ;;
  1) echo "FAIL: 召回门回归(semantic relevance@5 退化)，真 regression，不 swap"; exit 1 ;;
  2) echo "HOLD: 召回门 PRECONDITION（环境/索引未就绪，非回归），不 swap，先修前置再重跑"; exit 2 ;;
  3) echo "HOLD: 召回门 INDEX_STALE（manifest/db 指纹不一致，非回归），不 swap，需重建后重跑"; exit 3 ;;
  *) echo "FAIL: 召回门未知退出码 rc=$rc，按不可判定处理，不 swap"; exit "$rc" ;;
esac

# 5) swap（仅 SWAP=1）。老库已在步骤1另备份，此处再留一份 .swapped 双保险。
#    并发安全：全脚本持全局写锁 → index-pull/另一次 reingest 不能并发写 canonical（codex P1#1 writer 竞争已堵）。
#    残留：两次 mv 间 canonical 短暂不存在（亚毫秒），仅影响并发 READER（search）——失败重试即可，不损坏。
#    单用户 + swap 仅全量重建(罕见/手动)，不值得为此上 renameat2(RENAME_EXCHANGE)。
if [ "${SWAP:-0}" = "1" ]; then
  [ -d "$CANON" ] && mv "$CANON" "$CANON.$BIN_VERSION.bak.swapped.$(date +%Y%m%d-%H%M%S)"
  mv "$NEW" "$CANON"
  CASS_DATA_DIR="$CANON" "$BIN" status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
opened = d.get('database', {}).get('opened')
dv = d.get('db_vector_domain', {})
print('canon opened:', opened, '| db_vector_domain.active:', dv.get('active'))
"
  echo "OK: swapped -> $CANON 现为 $BIN_VERSION fork 库"
else
  echo "READY TO SWAP: 召回门已过。带 SWAP=1 再跑一次执行 swap，或手动 mv。"
fi
