#!/usr/bin/env python3
"""P5 §Task 7:LLM 参照臂(deepseek v4 flash 经 LiteLLM 逐卡判相关性)。

**定位**:六个确定性机制臂(`everos_eval.probe_arms.ARMS`)已在真数据上全部 FAIL
(预注册结局)。本脚本是这份 FAIL 报告的关键参照系——量化"判官式方法(LLM 逐卡
判相关性)在同一套 guard 判据门上的表现",**非生产候选**(`production_candidate:
false`,只是诊断/对照,不进入 Layer 2 幸存臂排名)。

用法:
    scripts/probe_llm_reference.py \
        --data-dir <probe-2b/data> --second-judge-dir <probe-2b/second_judge> \
        --out-dir <probe-2b/out> --prompt-path <probe-2b/data/judge-prompts-frozen.md>

env(零硬编码,PUBLIC 仓不得出现真实拓扑字面量):
    LITELLM_LLM_BASE   - OpenAI 兼容 base(如 http://<host>:<port>/v1)
    LITELLM_API_KEY    - 优先;缺省回退 LITELLM_ADMIN_KEY
    PROBE_PROMPT_PATH  - 冻结 prompt 副本路径(--prompt-path 未传时的默认来源)

**判定范围**:synthetic 990 对全量(30 query × 33 候选,候选经
`everos_eval.probe_candidates.load_candidates` 归一 canonical id,与 gold 生成
协议同构)。**逐卡调用**(一次一 query + 一卡,不合批 —— 单条调用之间无共享
上下文,job 顺序不影响结果,不需要额外打散)。

**prompt 忠实复现**:从 `--prompt-path` 指向的冻结 md 精确抽取「## 口径 A」到
「## 口径 B」之间的段落(标题行到下一标题前,`.strip()` 掉首尾空白),语义零
改动;该抽取段的 sha256 记入产出,供审查核对未被悄悄改写。运行时只在**内存**
里对这段文本追加输出 schema 包装(要求模型只输出一行 JSON:
`{"relevant": bool, "useful": bool, "reason": "..."}`,覆盖口径 A 原文里面向
批量判定设计的 job_id 字段要求),不落盘、不改源文件、不进 sha。

**判据引擎复用(冻结,一行不改)**:LLM 的"放行集合" = 每 query 下 relevant=true
的候选;把它包成 `everos_eval.probe_arms.Arm`(`apply` 无视 theta,直接查放行
集合——LLM 判定本身就是布尔值,没有可调阈值)喂给
`everos_eval.probe_metrics.compute_layer1_floors`/`compute_returned_for_query`。
gold 用 `everos_eval.probe_gold.load_gold(...)["primary"]`;floor 用真数据全量跑
`out/results.json` 里已推导的 0.31(`CONTAMINATION_FLOOR` 常量,不重算)。

**完整性规则**:990 对全部判定成功(ledger 无 error、无缺项)才计算精确三 floor
(`completeness: "complete"`);只要有一个 error 或缺项,直接标 `"incomplete"`,
只报"缺失候选按全放行 / 全拦截"两个边界假设下的三 floor(`bounds` 字段),不
假装能算出精确值。

**断点续跑**:回执台账 `out/llm_verdicts.jsonl` 按 job_id 幂等(ok/error 两种
终态都跳过,不重新发请求),中断重跑不重复计费。

**本脚本本身不发起真实调用**——实现与测试全部对着本机 fake HTTP server 走;
真数据全量跑由控制面在审查通过后另行执行(花费管控),执行时把
`--litellm-base`/`--litellm-key` 指到真实 LiteLLM 端点即可,代码不用改。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from everos_eval.probe_arms import Arm, ScoredQuery
from everos_eval.probe_candidates import load_candidates
from everos_eval.probe_gold import load_gold
from everos_eval.probe_metrics import (
    check_floors_pass,
    compute_layer1_floors,
    compute_returned_for_query,
)

# 真数据全量跑(P5 phase7)已推导的 contamination floor(见 out/results.json
# .contamination_floor.floor)。本臂是对照/诊断,原样复用同一份门,不重新推导
# ——重新推导会让"同一套 guard 判据门"这个比较基准本身漂移,失去参照意义。
CONTAMINATION_FLOOR = 0.31

MAX_RETRIES = 2  # 坏行重试 ≤2(总尝试 ≤ MAX_RETRIES+1)
MAX_TOKENS = 200
DEFAULT_TIMEOUT = 60

_SECTION_A_RE = re.compile(r"(?ms)^## 口径 A.*?(?=^## 口径 B)")

_OUTPUT_WRAPPER = (
    "\n\n---\n"
    "本次调用只针对一个 (查询, 记忆卡) 对，忽略上面输出格式里的 job_id 字段要求，"
    "只输出一行 JSON，不要输出任何其它文字（不要 markdown 代码块、不要多行、"
    "不要在 JSON 前后加任何说明）：\n"
    '{"relevant": true 或 false, "useful": true 或 false, "reason": "一句话依据"}'
)


class RunnerError(RuntimeError):
    """不可恢复的配置/协议错误(模型发现失败、prompt 抽取失败、必需数据缺失等)
    ——fail-loud,不重试、不降级。"""


# ======================================================================
# prompt 忠实抽取(口径 A)
# ======================================================================

def extract_criterion_a(frozen_md_text: str) -> str:
    """从冻结 judge-prompts md 精确抽取「## 口径 A」标题到「## 口径 B」标题
    之间的段落(含标题行,`.strip()` 去掉首尾空白行),语义零改动。抽不到直接
    `RunnerError`(fail-loud——不允许在边界标题找不到时静默退回整份文档或猜测
    边界,那样会悄悄改变喂给模型的口径)。"""
    m = _SECTION_A_RE.search(frozen_md_text)
    if not m:
        raise RunnerError(
            "judge-prompts-frozen.md 中未找到「## 口径 A」到「## 口径 B」之间的段落"
            "(冻结文件结构是否变了？抽取边界依赖这两个标题字面存在)"
        )
    return m.group(0).strip()


def build_system_prompt(criterion_a: str) -> str:
    """口径 A 原文 + 运行时内存追加的输出 schema 包装。**不落盘、不进 sha**——
    `extract_criterion_a` 的返回值才是被 sha256 记录、审查用的那份忠实抽取。"""
    return criterion_a + _OUTPUT_WRAPPER


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ======================================================================
# 模型发现(GET $LITELLM_BASE/models,deepseek+flash 实证匹配)
# ======================================================================

def discover_model(base: str, key: str, timeout: int = 30) -> str:
    """启动时 `GET {base}/models` 实证有且仅有一个同时含 'deepseek' 与 'flash'
    (大小写不敏感)的 model id;零个或多于一个都直接报错停,把候选列出来供人工
    判断——绝不猜测挑一个。"""
    req = Request(f"{base.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except HTTPError as e:
        raise RunnerError(f"GET {base}/models HTTP {e.code}: {e.read().decode()[:500]}") from e
    except URLError as e:
        raise RunnerError(f"GET {base}/models 连接失败: {e}") from e

    ids = sorted(m["id"] for m in body.get("data", []) if isinstance(m.get("id"), str))
    matched = sorted(i for i in ids if "deepseek" in i.lower() and "flash" in i.lower())
    if len(matched) != 1:
        raise RunnerError(
            f"模型发现失败:同时含 'deepseek'+'flash' 的候选数={len(matched)}(须恰为 1)。"
            f"候选={matched!r}；/models 全部可见 id={ids!r}"
        )
    return matched[0]


# ======================================================================
# 逐卡判定调用 + 回执解析校验
# ======================================================================

def _post_chat(base: str, key: str, model: str, system_prompt: str, user_prompt: str,
                timeout: int) -> dict:
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }  # 绝不开 thinking/reasoning 参数——结构化输出阶段开 thinking 会把 token 吃光产出不可解析
    req = Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        raise RuntimeError(f"POST chat/completions HTTP {e.code}: {e.read().decode()[:500]}") from e
    except URLError as e:
        raise RuntimeError(f"POST chat/completions 连接失败: {e}") from e


def parse_verdict(raw_content: str) -> dict:
    """回执解析校验:JSON 单行、字段类型、`useful⇒relevant`。任一违反抛
    `ValueError`(调用方据此触发重试,不静默放行坏回执)。"""
    text = raw_content.strip()
    if "\n" in text:
        raise ValueError(f"回执非单行: {text[:200]!r}")
    try:
        v = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"回执非合法 JSON: {text[:200]!r}") from e
    if not isinstance(v, dict):
        raise ValueError(f"回执顶层非 JSON object: {v!r}")
    relevant, useful, reason = v.get("relevant"), v.get("useful"), v.get("reason")
    if not isinstance(relevant, bool) or not isinstance(useful, bool):
        raise ValueError(f"回执字段类型错误(relevant/useful 须为 bool): {v!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"回执 reason 字段缺失/非串: {v!r}")
    if useful and not relevant:
        raise ValueError(f"回执违反 useful⇒relevant: {v!r}")
    return {"relevant": relevant, "useful": useful, "reason": reason}


_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def judge_one(base: str, key: str, model: str, system_prompt: str, query: str, card_text: str,
              *, timeout: int = DEFAULT_TIMEOUT, max_retries: int = MAX_RETRIES) -> dict:
    """一对 (query, card) 的判定,坏行(HTTP 失败 / 回执解析失败)重试
    ≤`max_retries` 次(总尝试 ≤ `max_retries+1`)。

    返回 `{"ok": True, "verdict": {...}, "usage": {...}, "attempts": n}` 或
    `{"ok": False, "error": "...", "usage": {...}, "attempts": n}`(终态失败,
    调用方据此记 error 台账,不再重试)。`usage` 累加**全部尝试**(含失败尝试)
    的 token 数——失败调用同样计费,不能只记最后一次成功的用量。
    """
    user_prompt = f"任务查询：{query}\n\n记忆卡：\n{card_text}"
    usage_total = dict(_ZERO_USAGE)
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = _post_chat(base, key, model, system_prompt, user_prompt, timeout)
            usage = resp.get("usage") or {}
            for k in usage_total:
                usage_total[k] += int(usage.get(k, 0) or 0)
            content = resp["choices"][0]["message"]["content"]
            verdict = parse_verdict(content)
            return {"ok": True, "verdict": verdict, "usage": usage_total, "attempts": attempt + 1}
        except Exception as e:  # noqa: BLE001 —— HTTP 失败与解析失败统一走重试(同 distill/distiller.py 纪律)
            last_err = e
    return {"ok": False, "error": str(last_err), "usage": usage_total, "attempts": max_retries + 1}


# ======================================================================
# 任务加载(990 对:30 query × 33 候选,canonical id 经 load_candidates 归一)
# ======================================================================

def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_jobs(data_dir: Path) -> list[dict]:
    """`queryset.jsonl × retrieval.jsonl(variant=="synthetic")` 派生的全量对,
    每条 `{job_id, query_id, query, canonical_card_id, mem_type, source_rank,
    card_text}`。job_id 用独立 `"llm:"` 前缀——与既有 `l1:`/`top5:`/`fs:`/`sj:`
    命名空间不冲突,这是判官式参照臂自己的台账,不复用/不污染既有 judge_io 协议
    (`everos_eval.judge_io.parse_verdicts` 按 kind 前缀校验,混用前缀会被那边
    的完整性断言拒绝)。"""
    queries = _read_jsonl(data_dir / "queryset.jsonl")
    card_text_by_id = {c["card_id"]: c["text"] for c in _read_jsonl(data_dir / "cards.jsonl")}

    candidates_by_qid: dict[str, list[dict]] = {}
    for row in _read_jsonl(data_dir / "retrieval.jsonl"):
        if row.get("variant") != "synthetic":
            continue
        candidates_by_qid[row["query_id"]] = load_candidates(row)

    missing = {q["query_id"] for q in queries} - candidates_by_qid.keys()
    if missing:
        raise RunnerError(f"缺 synthetic 检索行(retrieval.jsonl): {sorted(missing)}")

    jobs: list[dict] = []
    for q in queries:
        for c in candidates_by_qid[q["query_id"]]:
            cid = c["canonical_card_id"]
            if cid not in card_text_by_id:
                raise RunnerError(f"候选 {cid!r}(query={q['query_id']!r}) 不在 cards.jsonl 里")
            jobs.append({
                "job_id": f"llm:{q['query_id']}:{cid}",
                "query_id": q["query_id"],
                "query": q["query"],
                "canonical_card_id": cid,
                "mem_type": c["mem_type"],
                "source_rank": c["source_rank"],
                "card_text": card_text_by_id[cid],
            })
    return jobs


# ======================================================================
# 断点续跑台账(out/llm_verdicts.jsonl,按 job_id 幂等)
# ======================================================================

def load_ledger(ledger_path: Path) -> dict[str, dict]:
    if not ledger_path.exists():
        return {}
    done: dict[str, dict] = {}
    for rec in _read_jsonl(ledger_path):
        jid = rec.get("job_id")
        if isinstance(jid, str):
            done[jid] = rec  # 后写覆盖先写(正常不该重复,防御性处理)
    return done


def run_jobs(jobs: list[dict], ledger_path: Path, *, base: str, key: str, model: str,
             system_prompt: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, dict]:
    """逐 job 判定,ok/error 终态都落 ledger(job_id 幂等——已判过的跳过,不重新
    发请求,中断重跑不重复计费)。返回全部已判定记录(含续跑前已存在的)
    `{job_id: record}`。"""
    done = load_ledger(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as f:
        for job in jobs:
            jid = job["job_id"]
            if jid in done:
                continue
            result = judge_one(base, key, model, system_prompt, job["query"], job["card_text"],
                                timeout=timeout)
            record = {"job_id": jid, "query_id": job["query_id"],
                      "canonical_card_id": job["canonical_card_id"], **result}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            done[jid] = record
    return done


# ======================================================================
# 判据引擎复用(冻结,只 import 调用):LLM 放行集合 → compute_layer1_floors
# ======================================================================

def _allowed_by_qid(jobs: list[dict], ledger: dict[str, dict], *, fill_missing: bool
                     ) -> dict[str, set]:
    """LLM 的放行集合 = 每 query 下 relevant=true 的候选(已判定 ok 才算数)。
    `fill_missing`:缺失/error 候选如何处理——True(全放行边界)把它加入放行
    集合,False(全拦截边界)不加入(保持"未放行"的默认状态)。"""
    allowed: dict[str, set] = {}
    for job in jobs:
        qid, cid = job["query_id"], job["canonical_card_id"]
        allowed.setdefault(qid, set())
        rec = ledger.get(job["job_id"])
        if rec is not None and rec.get("ok"):
            if rec["verdict"]["relevant"]:
                allowed[qid].add(cid)
        elif fill_missing:
            allowed[qid].add(cid)
    return allowed


def _scored_queries(jobs: list[dict]) -> dict[str, ScoredQuery]:
    by_qid: dict[str, list[dict]] = {}
    for job in jobs:
        by_qid.setdefault(job["query_id"], []).append({
            "canonical_card_id": job["canonical_card_id"],
            "mem_type": job["mem_type"],
            "source_rank": job["source_rank"],
        })
    return {
        qid: ScoredQuery(query_id=qid, candidates=tuple(cands),
                          decoy_ce_by_type={"agent_case": (), "agent_skill": ()})
        for qid, cands in by_qid.items()
    }  # decoy_ce_by_type 空:llm_reference 臂的 apply() 不读它(只有 null_ref 用 decoy 分)


def _make_llm_arm(allowed_by_qid: dict[str, set]) -> Arm:
    def _apply(sq: ScoredQuery, theta) -> set[str]:  # noqa: ARG001 —— theta 未用:LLM 判定是布尔值,无阈值语义
        return allowed_by_qid.get(sq.query_id, set())

    return Arm("llm_reference", _apply)


def _compute_scenario(jobs: list[dict], ledger: dict[str, dict], sq_by_qid: dict[str, ScoredQuery],
                       gold_primary: dict, *, fill_missing: bool) -> dict:
    allowed = _allowed_by_qid(jobs, ledger, fill_missing=fill_missing)
    arm = _make_llm_arm(allowed)
    sq_list = [sq_by_qid[qid] for qid in sorted(sq_by_qid)]
    floors = compute_layer1_floors(sq_list, arm, theta=0, gold_variant=gold_primary)
    returned_by_qid = {
        qid: sorted(c["canonical_card_id"]
                    for c in compute_returned_for_query(sq_by_qid[qid], allowed.get(qid, set())))
        for qid in sq_by_qid
    }
    return {
        "floors": {k: v for k, v in floors.items() if k != "per_query"},
        "passed": check_floors_pass(floors, CONTAMINATION_FLOOR),
        "returned_by_qid": returned_by_qid,
    }


# ======================================================================
# 产出组装
# ======================================================================

def build_output(jobs: list[dict], ledger: dict[str, dict], sq_by_qid: dict[str, ScoredQuery],
                  gold_primary: dict, *, model: str, prompt_sha: str) -> dict:
    total = len(jobs)
    errors = [{"job_id": jid, "reason": rec.get("error")}
              for jid, rec in ledger.items() if not rec.get("ok")]
    ok_records = [rec for rec in ledger.values() if rec.get("ok")]
    relevant_count = sum(1 for rec in ok_records if rec["verdict"]["relevant"])
    useful_count = sum(1 for rec in ok_records if rec["verdict"]["useful"])

    token_usage = dict(_ZERO_USAGE)
    for rec in ledger.values():
        usage = rec.get("usage") or {}
        for k in token_usage:
            token_usage[k] += usage.get(k, 0)

    out: dict = {
        "model": model,
        "prompt_criterion_a_sha256": prompt_sha,
        "production_candidate": False,
        "contamination_floor": CONTAMINATION_FLOOR,
        "total_pairs": total,
        "judged_ok": len(ok_records),
        "errors": errors,
        "verdict_stats": {"relevant_count": relevant_count, "useful_count": useful_count},
        "token_usage": token_usage,
    }

    if not errors and len(ok_records) == total:
        scenario = _compute_scenario(jobs, ledger, sq_by_qid, gold_primary, fill_missing=False)
        out["completeness"] = "complete"
        out.update(scenario)
    else:
        out["completeness"] = "incomplete"
        allow_scn = _compute_scenario(jobs, ledger, sq_by_qid, gold_primary, fill_missing=True)
        block_scn = _compute_scenario(jobs, ledger, sq_by_qid, gold_primary, fill_missing=False)
        out["bounds"] = {
            "missing_treated_as_allow": allow_scn,
            "missing_treated_as_block": block_scn,
        }
    return out


# ======================================================================
# CLI
# ======================================================================

def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--second-judge-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--prompt-path", default=os.environ.get("PROBE_PROMPT_PATH"))
    ap.add_argument("--litellm-base", default=os.environ.get("LITELLM_LLM_BASE"))
    ap.add_argument("--litellm-key",
                     default=os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_ADMIN_KEY"))
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if not args.prompt_path:
        raise RunnerError("缺 --prompt-path(或 env PROBE_PROMPT_PATH)")
    if not args.litellm_base:
        raise RunnerError("缺 --litellm-base(或 env LITELLM_LLM_BASE)")
    if not args.litellm_key:
        raise RunnerError("缺 --litellm-key(或 env LITELLM_API_KEY / LITELLM_ADMIN_KEY)")

    data_dir = Path(args.data_dir)
    sj_dir = Path(args.second_judge_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frozen_text = Path(args.prompt_path).read_text(encoding="utf-8")
    criterion_a = extract_criterion_a(frozen_text)
    prompt_sha = sha256_text(criterion_a)
    system_prompt = build_system_prompt(criterion_a)

    model = discover_model(args.litellm_base, args.litellm_key, timeout=args.timeout)

    jobs = load_jobs(data_dir)
    gold = load_gold(data_dir, sj_dir)
    sq_by_qid = _scored_queries(jobs)

    ledger_path = out_dir / "llm_verdicts.jsonl"
    ledger = run_jobs(jobs, ledger_path, base=args.litellm_base, key=args.litellm_key, model=model,
                       system_prompt=system_prompt, timeout=args.timeout)

    output = build_output(jobs, ledger, sq_by_qid, gold["primary"], model=model, prompt_sha=prompt_sha)
    out_path = out_dir / "llm_reference.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"model={model} total={output['total_pairs']} ok={output['judged_ok']} "
          f"completeness={output['completeness']} -> {out_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RunnerError as e:
        print(f"RunnerError: {e}", file=sys.stderr)
        sys.exit(1)
