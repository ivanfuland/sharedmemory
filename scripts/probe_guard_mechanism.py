#!/usr/bin/env python3
"""P5 §Task 6:guard 机制探针 runner(阶段化,唯一执行入口)。

    scripts/probe_guard_mechanism.py \
        --data-dir <probe-2b/data> --second-judge-dir <probe-2b/second_judge> \
        --infinity-base http://<infinity-host>:<port> --cache-path out/score_cache.json \
        --out-dir out/

阶段顺序是硬约束(执行门):phase0(完整性,sha 校验源+副本、数据地形)→
phase1(live known-control,阻断断言不过即停——**这是本 runner 首次接触真实
分数的地方**)→ phase2(打分,cos/ce/native + decoy ce,写 ScoreCache)→
phase3(全臂 × primary gold 的 cv/final,幸存判定,Layer 2)→ phase4(幸存臂的
敏感性:full spec、D2/D3 leave-one-decoy-out、sens-REL/sens-IRR 运输、
score-desc 排序诊断)→ phase5(数值漂移:N 轮打乱重打分绕缓存,θ*-最近分值
间隔门 + fold θ 跨轮运输一致性门)→ phase6(guard_overhead_p95 延迟账,
40-passage 冻结 fixture)→ phase7(写 results.json)。任一阶段失败,写
`{"status": "error", "failed_phase": ..., "error": ...}` 终态到 results.json
并非零退出——不是让调用方去解析裸 traceback。

**本文件本身不接触真实分数**——它是可以对着任意 Infinity/LiteLLM 端点跑的
通用机制;Step 4(本次任务)只要求它在 fake 端点下走通全部阶段(smoke),
真数据全量跑是 Step 5,由控制面在审查通过后另行执行,执行时把
`--infinity-base` 指到真实 cc-infinity 即可,代码不用改。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from everos_eval.probe_arms import ARMS, ScoredQuery
from everos_eval.probe_candidates import assert_closure, load_candidates
from everos_eval.probe_gold import load_gold
from everos_eval.probe_metrics import (
    apply_fixed_fold_thetas,
    baseline_macro_fdr,
    compute_returned_for_query,
    direction_stability,
    final_fit,
    grouped_loocv,
    layer2_select,
    survives,
    summarize_oof_for_layer2,
    transport_check,
)
from everos_eval.retrieve import canonical_id
from everos_eval.probe_passage import (
    build_passage,
    passage_spec_sha,
    run_window_probe,
)
from everos_eval.probe_scores import (
    CACHE_META_FIELDS,
    ScoreCache,
    cosine,
    embed,
    rerank,
    run_known_control_checks,
    select_known_control_cards,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DECOY_DIR = REPO_ROOT / "everos_eval" / "data"
GOLD_VARIANTS = ("primary", "sens_rel", "sens_irr")
LATENCY_SEED = 20260715


# ======================================================================
# 小工具:jsonl / sha256
# ======================================================================

def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _git_head_sha(repo_dir: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                          capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _manifest_sha(data_dir: Path, second_judge_dir: Path) -> str:
    files = sorted(list(data_dir.glob("*.jsonl")) + list(second_judge_dir.glob("*.jsonl")))
    parts = [f"{f.relative_to(data_dir.parent)}:{_sha256_file(f)}" for f in files]
    return _sha256_text("|".join(parts))


def _decoy_sha(decoy_dir: Path) -> str:
    sha_file = decoy_dir / "decoys.sha256"
    if sha_file.exists():
        return _sha256_text(sha_file.read_text(encoding="utf-8"))
    files = sorted(decoy_dir.glob("decoys_*.jsonl"))
    parts = [f"{f.name}:{_sha256_file(f)}" for f in files]
    return _sha256_text("|".join(parts))


class RunnerError(RuntimeError):
    """阶段级 fail-loud 错误——runner 捕获后落 error 终态,不是让它裸崩。"""


# ======================================================================
# phase0:完整性(sha 校验源+副本、数据地形、window probe)
# ======================================================================

def phase0_integrity(args) -> dict:
    data_dir = Path(args.data_dir)
    sj_dir = Path(args.second_judge_dir)

    window = run_window_probe(args.infinity_base)

    gold = load_gold(data_dir, sj_dir)

    cards = _read_jsonl(data_dir / "cards.jsonl")
    cards_by_id = {c["card_id"]: c for c in cards}
    cards_ids = set(cards_by_id)
    gold_ids = set(cards_ids)  # load_gold 已核对 sj/L1 candidate id 与 cards.jsonl 同构闭合

    candidates_by_qid: dict[str, list[dict]] = {}
    top5_by_qid: dict[str, list[str]] = {}
    for row in _read_jsonl(data_dir / "retrieval.jsonl"):
        if row.get("variant") != "synthetic":
            continue
        cands = load_candidates(row)
        assert_closure(cands, cards_ids, gold_ids)
        candidates_by_qid[row["query_id"]] = cands
        top5_by_qid[row["query_id"]] = [
            canonical_id(item["id"], item["mem_type"]) for item in row["top5"]
        ]

    queries = _read_jsonl(data_dir / "queryset.jsonl")
    query_text_by_id = {q["query_id"]: q["query"] for q in queries}
    raw_text_by_qid = {q["query_id"]: q["raw_baseline"] for q in queries}

    manifest_sha = _manifest_sha(data_dir, sj_dir)
    decoy_sha = _decoy_sha(DECOY_DIR)
    code_git_sha = args.code_git_sha or _git_head_sha(REPO_ROOT)
    uv_lock_sha = _sha256_file(REPO_ROOT / "uv.lock")

    topology = {
        "n_queries": len(queries),
        "n_cards": len(cards),
        "primary_covered": len(gold["primary"]["covered"]),
        "primary_uncovered": len(gold["primary"]["uncovered"]),
        "primary_baseline_useful": len(gold["primary"]["baseline_useful"]),
        "n_groups": len(gold["primary"]["groups"]),
        "n_excluded": len(gold["primary"]["excluded"]),
    }

    return {
        "window": window,
        "gold": gold,
        "cards_by_id": cards_by_id,
        "candidates_by_qid": candidates_by_qid,
        "top5_by_qid": top5_by_qid,
        "query_text_by_id": query_text_by_id,
        "raw_text_by_qid": raw_text_by_qid,
        "manifest_sha": manifest_sha,
        "decoy_sha": decoy_sha,
        "code_git_sha": code_git_sha,
        "uv_lock_sha": uv_lock_sha,
        "topology": topology,
    }


# ======================================================================
# contamination floor 自动推导(执行门已拍定「公式值」路线)
# ======================================================================

FLOOR_FORMULA = ("floor = round(baseline / 2, 2); baseline = 未过滤 top5 按 §P5 正式计分规则"
                 "(剔除卡最坏向计 irrelevant、returned 全剔除卡 FDR=1、空放行 FDR=0)"
                 "在 primary covered 查询上的宏平均 FDR")


def derive_contamination_floor(ctx: dict, args) -> dict:
    """runner 自动从未过滤 top5 实算基线宏 FDR 并按冻结公式推导 floor。
    `--contamination-floor-override` 仅供测试注入(smoke 构造边界用),生产路径
    不传——floor 值、基线值、公式串全部落 results.json 审计留痕。"""
    if args.contamination_floor_override is not None:
        return {
            "floor": args.contamination_floor_override,
            "baseline_macro_fdr": None,
            "formula": FLOOR_FORMULA,
            "source": "override(测试注入,生产路径不用)",
        }
    baseline = baseline_macro_fdr(ctx["top5_by_qid"], ctx["gold"]["primary"])
    return {
        "floor": round(baseline / 2, 2),
        "baseline_macro_fdr": baseline,
        "formula": FLOOR_FORMULA,
        "source": "derived",
    }


# ======================================================================
# phase1:live known-control(**首次接触真实分数**,阻断断言不过即停)
# ======================================================================

def _passage_payload(candidate: dict, cards_by_id: dict) -> dict:
    """passage 组装的字段来源:候选自带的检索 payload 优先,cards.jsonl 记录
    补漏(两边字段名冲突时以检索 payload 为准——它是这次探针实际检索到的
    内容)。两边都缺所需字段时,`build_passage` 会原生 KeyError(fail-loud,
    不静默填空)。"""
    card = cards_by_id.get(candidate["canonical_card_id"], {})
    merged = dict(card)
    merged.update(candidate.get("payload") or {})
    return merged


def _make_rerank_fn(infinity_base: str, rerank_model: str, timeout: int):
    def _fn(query: str, docs: list[str]) -> list[float]:
        return rerank(query, docs, base_url=infinity_base, model=rerank_model, timeout=timeout)
    return _fn


def _make_embed_fn(infinity_base: str, embed_model: str, timeout: int):
    def _fn(texts: list[str]) -> list[list[float]]:
        return embed(texts, base_url=infinity_base, model=embed_model, timeout=timeout)
    return _fn


def phase1_known_control(ctx: dict, args) -> dict:
    gold = ctx["gold"]
    candidates_by_qid = ctx["candidates_by_qid"]
    cards_by_id = ctx["cards_by_id"]
    window = ctx["window"]

    selection = select_known_control_cards(gold, candidates_by_qid)
    query_text = ctx["query_text_by_id"][selection.q_star]
    cards = [selection.relevant, selection.same_type_irrelevant]
    passages = [
        build_passage(_passage_payload(c, cards_by_id), c["mem_type"], spec="prod",
                       cap=window.cap)
        for c in cards
    ]
    expected_native_scores = {c["canonical_card_id"]: c["native_score"] for c in cards}

    result = run_known_control_checks(
        selection,
        query_text=query_text,
        passages=passages,
        cards_ids=set(cards_by_id),
        gold_ids=set(cards_by_id),
        rerank_fn=_make_rerank_fn(args.infinity_base, args.rerank_model, args.timeout),
        embed_fn=_make_embed_fn(args.infinity_base, args.embed_model, args.timeout),
        expected_native_scores=expected_native_scores,
    )
    return {
        "q_star": selection.q_star,
        "relevant_card": selection.relevant["canonical_card_id"],
        "same_type_irrelevant_card": selection.same_type_irrelevant["canonical_card_id"],
        "warnings": result.warnings,
    }


# ======================================================================
# phase2:打分(cos/ce/native + decoy ce),写 ScoreCache
# ======================================================================

def _load_decoys(set_name: str) -> dict[str, list[str]]:
    """`set_name` ∈ {d1, d2, d3}。返回 {"agent_case": [...texts], "agent_skill": [...]}"""
    out = {}
    for mem_type, prefix in (("agent_case", "case"), ("agent_skill", "skill")):
        path = DECOY_DIR / f"decoys_{prefix}_{set_name}.jsonl"
        out[mem_type] = [r["text"] for r in _read_jsonl(path)]
    return out


def build_scored_queries(ctx: dict, *, spec: str, cache: ScoreCache, spec_sha: str,
                          args, decoy_set: str = "d1") -> dict[str, ScoredQuery]:
    """对 `candidates_by_qid` 里的每条查询打 cos/ce,并对同一批冻结 decoy 文本各自
    重打分(cross-encoder 分数是 query-dependent 的——decoy ce 必须对**这条查询**
    重算,不能用固定锚点代表所有查询;这也是 guard_overhead 延迟账里 null_ref
    "N+16 对"算在同一次查询级 rerank 调用里的原因)。缓存命中的候选/decoy 跳过
    网络调用。
    """
    candidates_by_qid = ctx["candidates_by_qid"]
    cards_by_id = ctx["cards_by_id"]
    window = ctx["window"]
    query_text_by_id = ctx["query_text_by_id"]

    decoy_texts = _load_decoys(decoy_set)

    sq_by_qid: dict[str, ScoredQuery] = {}
    for qid, candidates in candidates_by_qid.items():
        query_text = query_text_by_id[qid]
        passages = [
            build_passage(_passage_payload(c, cards_by_id), c["mem_type"], spec=spec, cap=window.cap)
            for c in candidates
        ]
        cids = [c["canonical_card_id"] for c in candidates]

        need_cos = [i for i in range(len(cids))
                    if cache.get("cos", spec_sha, "synthetic", qid, cids[i]) is None]
        need_ce = [i for i in range(len(cids))
                   if cache.get("ce", spec_sha, "synthetic", qid, cids[i]) is None]

        if need_cos:
            vectors = embed([query_text] + [passages[i] for i in need_cos],
                             base_url=args.infinity_base, model=args.embed_model,
                             timeout=args.timeout)
            q_vec, doc_vecs = vectors[0], vectors[1:]
            for i, vec in zip(need_cos, doc_vecs):
                cache.put("cos", spec_sha, "synthetic", qid, cids[i], cosine(q_vec, vec))

        if need_ce:
            scores = rerank(query_text, [passages[i] for i in need_ce],
                             base_url=args.infinity_base, model=args.rerank_model,
                             timeout=args.timeout)
            for i, score in zip(need_ce, scores):
                cache.put("ce", spec_sha, "synthetic", qid, cids[i], score)

        scored_candidates = tuple(
            {**c, "cos": cache.get("cos", spec_sha, "synthetic", qid, c["canonical_card_id"]),
             "ce": cache.get("ce", spec_sha, "synthetic", qid, c["canonical_card_id"])}
            for c in candidates
        )

        decoy_ce_by_type: dict[str, tuple[float, ...]] = {}
        for mem_type, texts in decoy_texts.items():
            need_idx = [i for i in range(len(texts))
                        if cache.get("decoy_ce", spec_sha, decoy_set, qid, f"{mem_type}_{i}") is None]
            if need_idx:
                fresh = rerank(query_text, [texts[i] for i in need_idx],
                                base_url=args.infinity_base, model=args.rerank_model,
                                timeout=args.timeout)
                for i, score in zip(need_idx, fresh):
                    cache.put("decoy_ce", spec_sha, decoy_set, qid, f"{mem_type}_{i}", score)
            decoy_ce_by_type[mem_type] = tuple(
                cache.get("decoy_ce", spec_sha, decoy_set, qid, f"{mem_type}_{i}")
                for i in range(len(texts))
            )

        sq_by_qid[qid] = ScoredQuery(query_id=qid, candidates=scored_candidates,
                                      decoy_ce_by_type=decoy_ce_by_type)

    return sq_by_qid


def _combined_spec_sha(spec: str, cap: int) -> str:
    """prod/full spec 对 case/skill 各有独立 sha(`_SPEC_DESC` 按 mem_type 拆)——
    合成成一个值供 ScoreCache 的 `passage_spec_sha` meta 字段用,任一类型的规格
    描述变了都必须反映到这一个值上(否则只变了 skill 侧字段时,单独用 case 侧
    sha 的话缓存会误判"规格没变"而复用过期的 skill passage 分数)。"""
    case_sha = passage_spec_sha(spec, cap, "agent_case")
    skill_sha = passage_spec_sha(spec, cap, "agent_skill")
    return _sha256_text(f"{case_sha}|{skill_sha}")


def phase2_scoring(ctx: dict, args) -> dict:
    window = ctx["window"]
    spec_sha = _combined_spec_sha("prod", window.cap)

    meta = {
        "manifest_sha": ctx["manifest_sha"],
        "embed_model": window.embed_model_id,
        "embed_model_revision": window.embed_model_revision,
        "rerank_model": window.rerank_model_id,
        "rerank_model_revision": window.rerank_model_revision,
        "tokenizer_artifact_sha": _sha256_text(
            f"{window.rerank_model_id}@{window.rerank_model_revision}"),
        "embedding_dim": args.embedding_dim,
        "cap": window.cap,
        "pair_budget": args.pair_budget,
        "passage_spec_sha": spec_sha,
        "decoy_sha": ctx["decoy_sha"],
        "code_git_sha": ctx["code_git_sha"],
        "uv_lock_sha": ctx["uv_lock_sha"],
    }
    cache = ScoreCache(meta, path=Path(args.cache_path) if args.cache_path else None)
    cache_rejected = cache.rejected

    sq_by_qid = build_scored_queries(ctx, spec="prod", cache=cache, spec_sha=spec_sha, args=args)

    # 注意:这里不 save()——phase4(full spec / D2/D3)复用同一个 cache 对象继续写,
    # 落盘统一放到 main() 里全部阶段跑完之后一次性做,避免 phase4 写进去的条目
    # 因为"早存了一次"而丢失(见 phase4_sensitivity 的复用纪律)。

    return {
        "sq_by_qid": sq_by_qid,
        "spec_sha": spec_sha,
        "meta": meta,
        "cache": cache,
        "cache_rejected": cache_rejected,
    }


# ======================================================================
# phase3:全臂 × primary gold 的 cv/final,幸存判定,Layer 2
# ======================================================================

def phase3_fit_and_cv(ctx: dict, scoring: dict, args) -> dict:
    sq_by_qid = scoring["sq_by_qid"]
    gold = ctx["gold"]
    floor = args.contamination_floor

    per_arm = {}
    survivors_for_layer2 = []
    survivor_ctx = {}

    for arm_name, arm in ARMS.items():
        cv = grouped_loocv(sq_by_qid, arm, gold["primary"], floor)
        fin = final_fit(sq_by_qid, arm, gold["primary"], floor)
        surv = survives(cv, fin)
        oof_summary = summarize_oof_for_layer2(cv["oof_returned"], gold["primary"])

        per_arm[arm_name] = {
            "cv_floors": cv["cv_floors"],
            "cv_pass": cv["cv_pass"],
            "fold_thetas": cv["fold_thetas"],
            "final_theta_star": fin["theta_star"],
            "final_resub_floors": fin["resub_floors"],
            "final_feasible": fin["feasible"],
            "survives": surv,
            "oof_summary": oof_summary,
        }
        if surv:
            survivors_for_layer2.append({"arm_name": arm_name, "oof_summary": oof_summary})
            survivor_ctx[arm_name] = {"arm": arm, "cv": cv, "final": fin}

    layer2 = layer2_select(survivors_for_layer2, performance_source="oof")

    return {"per_arm": per_arm, "layer2": layer2, "survivor_ctx": survivor_ctx}


# ======================================================================
# phase4:幸存臂敏感性(full spec / D2/D3 leave-one-decoy-out / sens-REL-IRR 运输 /
# score-desc 排序诊断)
# ======================================================================

def _score_desc_returned(sq: ScoredQuery, allowed: set, score_field: str, limit: int = 5) -> list[dict]:
    """诊断专用(非冻结管线):若改用"score 降序"合并而非冻结的 skill-first
    交错,returned 会不会变——只用于 phase4 敏感性诊断列,Layer 1/2 判据绝不
    读这个函数的输出。"""
    ranked = sorted(sq.candidates, key=lambda c: c.get(score_field, float("-inf")), reverse=True)
    return [c for c in ranked if c["canonical_card_id"] in allowed][:limit]


def phase4_sensitivity(ctx: dict, scoring: dict, phase3: dict, args) -> dict:
    gold = ctx["gold"]
    floor = args.contamination_floor
    sq_by_qid = scoring["sq_by_qid"]
    cache = scoring["cache"]
    out = {}

    # 跨幸存臂只建一次(不是每个臂都重打分一遍全量 40-50 张卡):full spec 与
    # D2/D3 都复用 phase2 那个持久化的 ScoreCache 对象——既避免同一次 run 内对
    # 每个臂重复打同一批分,也让"缓存续跑"对 phase4 同样生效(不是只对 phase2 生效)。
    full_spec_sha = _combined_spec_sha("full", ctx["window"].cap)
    sq_full: dict[str, ScoredQuery] | None = None
    decoy_variant_sq: dict[str, dict[str, ScoredQuery]] = {}

    for arm_name, surv in phase3["survivor_ctx"].items():
        arm = surv["arm"]
        theta_star = surv["final"]["theta_star"]
        entry: dict = {}

        # ---- sens-REL / sens-IRR 运输(不重新拟合) ----
        for variant in ("sens_rel", "sens_irr"):
            entry[variant] = transport_check(theta_star, arm, sq_by_qid, gold[variant], floor)

        # ---- full spec(若非 no-op:native_pertype 不依赖 passage 内容,天然 no-op) ----
        if arm_name != "native_pertype":
            if sq_full is None:
                sq_full = build_scored_queries(ctx, spec="full", cache=cache,
                                                spec_sha=full_spec_sha, args=args)
            entry["full_spec"] = transport_check(theta_star, arm, sq_full, gold["primary"], floor)
        else:
            entry["full_spec"] = {"no_op": True}

        # ---- D2/D3 leave-one-decoy-out(只对 null_ref 有实质影响,其余臂忠实跑一遍诊断) ----
        decoy_variants = {}
        for decoy_set in ("d2", "d3"):
            if decoy_set not in decoy_variant_sq:
                decoy_variant_sq[decoy_set] = build_scored_queries(
                    ctx, spec="prod", cache=cache, spec_sha=scoring["spec_sha"],
                    args=args, decoy_set=decoy_set)
            sq_alt = decoy_variant_sq[decoy_set]
            decoy_variants[decoy_set] = transport_check(theta_star, arm, sq_alt, gold["primary"], floor)
        if arm_name == "null_ref":
            # 留一 decoy 出:对每条查询自己已算好的 decoy ce 分数(不是原始文本)逐个剔除
            # 一个再跑一遍 transport_check——decoy ce 是 query-dependent 的,LODO 必须
            # 在"分数"这一层操作,不能重新对文本做子集再假装是分数。
            lodo = []
            sample_sq = next(iter(sq_by_qid.values()))
            for mem_type in ("agent_case", "agent_skill"):
                n_decoys = len(sample_sq.decoy_ce_by_type[mem_type])
                for i in range(n_decoys):
                    sq_lodo = {
                        qid: ScoredQuery(
                            query_id=sq.query_id, candidates=sq.candidates,
                            decoy_ce_by_type={
                                mt: (tuple(v for j, v in enumerate(sq.decoy_ce_by_type[mt]) if j != i)
                                     if mt == mem_type else sq.decoy_ce_by_type[mt])
                                for mt in sq.decoy_ce_by_type
                            },
                        )
                        for qid, sq in sq_by_qid.items()
                    }
                    check = transport_check(theta_star, arm, sq_lodo, gold["primary"], floor)
                    lodo.append({"mem_type": mem_type, "removed_index": i, "passed": check["passed"]})
            decoy_variants["leave_one_decoy_out"] = {
                "all_passed": all(r["passed"] for r in lodo),
                "detail": lodo,
            }
        entry["decoy_variants"] = decoy_variants

        # ---- score-desc 排序诊断(非冻结管线,只诊断不判定) ----
        score_field = {
            "native_pertype": "native_score", "cos_unified": "cos", "cos_pertype": "cos",
            "ce_fixed": "ce", "ce_znorm": "ce", "null_ref": "ce",
        }[arm_name]
        mismatches = 0
        for sq in sq_by_qid.values():
            allowed = arm.apply(sq, theta_star)
            skill_first = {c["canonical_card_id"] for c in compute_returned_for_query(sq, allowed)}
            desc = {c["canonical_card_id"] for c in _score_desc_returned(sq, allowed, score_field)}
            if skill_first != desc:
                mismatches += 1
        entry["score_desc_sensitivity"] = {"queries_with_order_dependent_returned_set": mismatches}

        # ---- 方向稳定性诊断(P1-12):同一份 OOF 预测在三套 gold 下各算误差方向标签 ----
        entry["direction_stability"] = direction_stability(surv["cv"]["oof_returned"], gold)

        out[arm_name] = entry

    return {"arms": out, "raw_diagnostic": raw_diagnostic(ctx, scoring, phase3, args)}


# ======================================================================
# raw variant 敏感性(冻结条文;纯诊断,不进幸存判定)
# ======================================================================

def _raw_eligibility(raw_text_by_qid: dict[str, str]) -> tuple[dict[str, str], list[dict]]:
    """eligibility 过滤:raw_baseline 完全重复的查询组只保 query_id 最小者,
    其余记 ineligible(带保留者与原因,入 results 审计)。"""
    by_text: dict[str, list[str]] = {}
    for qid, text in raw_text_by_qid.items():
        by_text.setdefault(text, []).append(qid)

    eligible: dict[str, str] = {}
    ineligible: list[dict] = []
    for text, qids in by_text.items():
        qids.sort()
        eligible[qids[0]] = text
        for dup in qids[1:]:
            ineligible.append({"query_id": dup, "kept": qids[0],
                                "reason": "duplicate raw_baseline(完全重复组只保 query_id 最小者)"})
    ineligible.sort(key=lambda r: r["query_id"])
    return eligible, ineligible


def raw_diagnostic(ctx: dict, scoring: dict, phase3: dict, args) -> dict:
    """raw variant 敏感性:对 eligibility 过滤后的查询,用 raw_baseline 文本重打
    query 侧分数(卡侧 passage/native 分复用),θ* 原样运输算三 floor。**纯诊断,
    不进幸存判定**。raw 文本可能超 PAIR_BUDGET——超限(或其它打分校验拒绝)的
    查询记 skipped + 原因,不炸整跑。"""
    eligible, ineligible = _raw_eligibility(ctx["raw_text_by_qid"])
    cache = scoring["cache"]
    spec_sha = scoring["spec_sha"]
    window = ctx["window"]
    cards_by_id = ctx["cards_by_id"]
    decoy_texts = _load_decoys("d1")

    sq_raw: dict[str, ScoredQuery] = {}
    skipped: dict[str, str] = {}

    for qid in sorted(eligible):
        raw_text = eligible[qid]
        candidates = ctx["candidates_by_qid"][qid]
        passages = [
            build_passage(_passage_payload(c, cards_by_id), c["mem_type"], spec="prod",
                           cap=window.cap)
            for c in candidates
        ]
        cids = [c["canonical_card_id"] for c in candidates]

        try:
            # ce(先跑:rerank 的 PAIR_BUDGET 断言在发请求前,超限查询在这里被拦下)
            need_ce = [i for i in range(len(cids))
                       if cache.get("ce", spec_sha, "raw", qid, cids[i]) is None]
            if need_ce:
                scores = rerank(raw_text, [passages[i] for i in need_ce],
                                 base_url=args.infinity_base, model=args.rerank_model,
                                 timeout=args.timeout)
                for i, score in zip(need_ce, scores):
                    cache.put("ce", spec_sha, "raw", qid, cids[i], score)

            # decoy ce(同样 query-dependent,必须用 raw 文本重打;variant 槽位用
            # "raw-d1" 与 synthetic 的 "d1" 区分,不许串缓存)
            decoy_ce_by_type: dict[str, tuple[float, ...]] = {}
            for mem_type, texts in decoy_texts.items():
                need_idx = [i for i in range(len(texts))
                            if cache.get("decoy_ce", spec_sha, "raw-d1", qid, f"{mem_type}_{i}") is None]
                if need_idx:
                    fresh = rerank(raw_text, [texts[i] for i in need_idx],
                                    base_url=args.infinity_base, model=args.rerank_model,
                                    timeout=args.timeout)
                    for i, score in zip(need_idx, fresh):
                        cache.put("decoy_ce", spec_sha, "raw-d1", qid, f"{mem_type}_{i}", score)
                decoy_ce_by_type[mem_type] = tuple(
                    cache.get("decoy_ce", spec_sha, "raw-d1", qid, f"{mem_type}_{i}")
                    for i in range(len(texts))
                )

            # cos(query 侧换 raw 文本重 embed;passage 向量由 cosine 每对现算)
            need_cos = [i for i in range(len(cids))
                        if cache.get("cos", spec_sha, "raw", qid, cids[i]) is None]
            if need_cos:
                vectors = embed([raw_text] + [passages[i] for i in need_cos],
                                 base_url=args.infinity_base, model=args.embed_model,
                                 timeout=args.timeout)
                q_vec, doc_vecs = vectors[0], vectors[1:]
                for i, vec in zip(need_cos, doc_vecs):
                    cache.put("cos", spec_sha, "raw", qid, cids[i], cosine(q_vec, vec))
        except ValueError as e:
            skipped[qid] = f"{type(e).__name__}: {e}"
            continue

        scored_candidates = tuple(
            {**c, "cos": cache.get("cos", spec_sha, "raw", qid, c["canonical_card_id"]),
             "ce": cache.get("ce", spec_sha, "raw", qid, c["canonical_card_id"])}
            for c in candidates
        )
        sq_raw[qid] = ScoredQuery(query_id=qid, candidates=scored_candidates,
                                   decoy_ce_by_type=decoy_ce_by_type)

    per_arm = {}
    for arm_name, surv in phase3["survivor_ctx"].items():
        per_arm[arm_name] = transport_check(surv["final"]["theta_star"], surv["arm"], sq_raw,
                                             ctx["gold"]["primary"], args.contamination_floor)

    return {
        "ineligible": ineligible,
        "skipped": skipped,
        "n_eligible": len(eligible),
        "n_scored": len(sq_raw),
        "per_arm": per_arm,
        "note": "纯诊断:raw 三 floor 不参与幸存判定与 Layer 2 排名",
    }


# ======================================================================
# phase5:数值漂移(N 轮打乱重打分绕缓存,θ* 边距门 + fold θ 跨轮运输一致性门)
# ======================================================================

def _extract_signal_values(sq_by_qid: dict, arm_name: str, mem_type: str | None = None) -> list[float]:
    field = "native_score" if arm_name == "native_pertype" else (
        "cos" if arm_name in ("cos_unified", "cos_pertype") else "ce")
    vals = []
    for sq in sq_by_qid.values():
        for c in sq.candidates:
            if mem_type is not None and c["mem_type"] != mem_type:
                continue
            vals.append(c[field])
    return vals


def phase5_drift(ctx: dict, scoring: dict, phase3: dict, args) -> dict:
    rounds: list[dict[str, ScoredQuery]] = []
    fresh_cache_meta = dict(scoring["meta"])

    for r in range(args.drift_rounds):
        # 每轮用全新、不落盘的 ScoreCache——强制真实网络调用,绕过持久化缓存,
        # 拿到 Infinity 端确定性(或非确定性)的实测抖动。
        round_cache = ScoreCache(fresh_cache_meta)
        round_args = args
        round_ctx = dict(ctx)
        # 批内乱序(冻结 seed + 轮次偏移):候选顺序打乱不应改变最终分数归属
        # (P0-2 契约由 probe_scores 保证);这里只是让每轮的请求 batch 组成不同。
        rng = random.Random(args.seed + r)
        shuffled_candidates_by_qid = {}
        for qid, cands in ctx["candidates_by_qid"].items():
            shuffled = list(cands)
            rng.shuffle(shuffled)
            shuffled_candidates_by_qid[qid] = shuffled
        round_ctx["candidates_by_qid"] = shuffled_candidates_by_qid
        sq_round = build_scored_queries(round_ctx, spec="prod", cache=round_cache,
                                         spec_sha=scoring["spec_sha"], args=round_args)
        rounds.append(sq_round)

    max_abs_drift: dict[str, float] = {}
    for signal in ("cos", "ce"):
        per_key: dict[tuple, list[float]] = {}
        for sq_by_qid in rounds:
            for qid, sq in sq_by_qid.items():
                for c in sq.candidates:
                    per_key.setdefault((qid, c["canonical_card_id"]), []).append(c[signal])
        drift = max((max(v) - min(v) for v in per_key.values()), default=0.0)
        max_abs_drift[signal] = drift

    arm_reports = {}
    for arm_name, surv in phase3["survivor_ctx"].items():
        theta_star = surv["final"]["theta_star"]
        arm = surv["arm"]

        # ---- θ* 边距门:θ* 到两侧最近实测分值的距离必须 > max_abs_drift(该信号) ----
        if arm_name == "null_ref":
            signal_key = "ce"  # margin 基于 ce,但 drift 门用 ce 本身的抖动幅度作保守上界
        else:
            signal_key = "native_score" if arm_name == "native_pertype" else (
                "cos" if arm_name in ("cos_unified", "cos_pertype") else "ce")
        drift_for_signal = max_abs_drift.get(signal_key, 0.0) if signal_key in ("cos", "ce") else 0.0

        margin_ok = True
        if theta_star is not None and signal_key in ("cos", "ce"):
            if arm_name in ("native_pertype", "cos_pertype"):
                tc, ts = theta_star
                case_vals = _extract_signal_values(rounds[0], arm_name, "agent_case")
                skill_vals = _extract_signal_values(rounds[0], arm_name, "agent_skill")
                margins = [abs(tc - v) for v in case_vals] + [abs(ts - v) for v in skill_vals]
            else:
                vals = _extract_signal_values(rounds[0], arm_name)
                margins = [abs(theta_star - v) for v in vals]
            nearest = min(margins) if margins else float("inf")
            margin_ok = nearest > drift_for_signal

        # ---- fold θ 跨轮运输一致性门:round0 的 fold_thetas 运输到其余轮,OOF returned/verdict 必须一致 ----
        fold_thetas = surv["cv"]["fold_thetas"]
        base_returned = apply_fixed_fold_thetas(rounds[0], arm, ctx["gold"]["primary"], fold_thetas)
        base_sets = {qid: {c["canonical_card_id"] for c in v} for qid, v in base_returned.items()}
        transport_stable = True
        for sq_round in rounds[1:]:
            r_returned = apply_fixed_fold_thetas(sq_round, arm, ctx["gold"]["primary"], fold_thetas)
            r_sets = {qid: {c["canonical_card_id"] for c in v} for qid, v in r_returned.items()}
            if r_sets != base_sets:
                transport_stable = False
                break

        fail_fragile = (not margin_ok) or (not transport_stable)
        arm_reports[arm_name] = {
            "signal": signal_key,
            "theta_margin_to_nearest_score": nearest if theta_star is not None and signal_key in ("cos", "ce") else None,
            "margin_ok": margin_ok,
            "fold_theta_transport_stable": transport_stable,
            "verdict": "FAIL-fragile-score" if fail_fragile else "stable",
        }

    return {"rounds": len(rounds), "max_abs_drift": max_abs_drift, "arms": arm_reports}


# ======================================================================
# phase6:guard_overhead_p95 延迟账(40-passage 冻结 fixture)
# ======================================================================

def build_latency_fixture(ctx: dict, args) -> tuple[dict, str]:
    """20 case + 20 skill,不足用确定性最长 passage 填充。返回 (fixture, fixture_hash)。"""
    window = ctx["window"]
    cases, skills = [], []
    for qid in sorted(ctx["candidates_by_qid"]):
        for c in ctx["candidates_by_qid"][qid]:
            passage = build_passage(_passage_payload(c, ctx["cards_by_id"]), c["mem_type"],
                                     spec="prod", cap=window.cap)
            if c["mem_type"] == "agent_case" and len(cases) < 20:
                cases.append(passage)
            elif c["mem_type"] == "agent_skill" and len(skills) < 20:
                skills.append(passage)
        if len(cases) >= 20 and len(skills) >= 20:
            break

    if skills and len(skills) < 20:
        longest = max(skills, key=len)
        while len(skills) < 20:
            skills.append(longest)
    if cases and len(cases) < 20:
        longest = max(cases, key=len)
        while len(cases) < 20:
            cases.append(longest)

    fixture = {"cases": cases[:20], "skills": skills[:20]}
    fixture_hash = _sha256_text(json.dumps(fixture, ensure_ascii=False, sort_keys=True))
    return fixture, fixture_hash


_LATENCY_PAIR_COUNTS = {"ce": 40, "null_ref": 56}


def _p95_nearest_rank(times: list[float]) -> float:
    times = sorted(times)
    n = len(times)
    idx = max(0, math.ceil(0.95 * n) - 1)
    return times[idx]


def _latency_call(arm_name: str, query_text: str, fixture: dict, args) -> None:
    """按臂的真实调用图打一次分(不含 Python 侧 allowed 判定,只测打分开销)。
    `timeout=10s` 硬顶(协议冻结),timeout/error 由调用方捕获判该臂延迟门 FAIL。

    有效 pair 数断言(冻结:ce=40 / null_ref=56):发请求前显式核对本次调用图的
    pair 数与冻结值一致——fixture 或 decoy 文件被改动导致 pair 数漂移时,延迟账
    的数字就不再是协议冻结的那份账,必须 fail-loud(RunnerError,整跑中止),
    不允许静默量一份"别的账"。"""
    docs = fixture["cases"] + fixture["skills"]
    if arm_name == "native_pertype":
        return  # 0 次调用,native 分已在检索阶段拿到
    if arm_name in ("cos_unified", "cos_pertype"):
        embed([query_text], base_url=args.infinity_base, model=args.embed_model,
              timeout=args.latency_timeout)
        return
    if arm_name in ("ce_fixed", "ce_znorm"):
        expected = _LATENCY_PAIR_COUNTS["ce"]
        if len(docs) != expected:
            raise RunnerError(
                f"guard_overhead_p95: ce 调用图有效 pair 数 {len(docs)} != 冻结值 {expected}"
                "(fixture 组装漂移,延迟账协议失效,停工)"
            )
        rerank(query_text, docs, base_url=args.infinity_base, model=args.rerank_model,
               timeout=args.latency_timeout)
        return
    if arm_name == "null_ref":
        decoys = _load_decoys("d1")
        all_docs = docs + decoys["agent_case"] + decoys["agent_skill"]
        expected = _LATENCY_PAIR_COUNTS["null_ref"]
        if len(all_docs) != expected:
            raise RunnerError(
                f"guard_overhead_p95: null_ref 调用图有效 pair 数 {len(all_docs)} != 冻结值 {expected}"
                "(fixture/decoy 文件漂移,延迟账协议失效,停工)"
            )
        rerank(query_text, all_docs, base_url=args.infinity_base, model=args.rerank_model,
               timeout=args.latency_timeout)
        return
    raise RunnerError(f"guard_overhead_p95: 未知臂 {arm_name!r} 无延迟调用图定义")


def phase6_latency(ctx: dict, phase3: dict, args) -> dict:
    fixture, fixture_hash = build_latency_fixture(ctx, args)

    all_qids = sorted(ctx["query_text_by_id"])
    rng = random.Random(LATENCY_SEED)
    sweep_qids = list(all_qids)
    rng.shuffle(sweep_qids)
    sweep_qids = (sweep_qids * ((args.latency_queries // max(len(sweep_qids), 1)) + 1))[:args.latency_queries]

    report = {"fixture_hash": fixture_hash,
              "n_cases": len(fixture["cases"]), "n_skills": len(fixture["skills"]),
              "arms": {}}

    for arm_name in phase3["survivor_ctx"]:
        times: list[float] = []
        failed = False
        error_msg = None
        for qid in sweep_qids:
            query_text = ctx["query_text_by_id"][qid]
            for rep in range(args.latency_reps):
                start = time.monotonic()
                try:
                    _latency_call(arm_name, query_text, fixture, args)
                except RunnerError:
                    raise  # pair 数断言 = 协议完整性错误,整跑中止,不降级为单臂 FAIL
                except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as e:
                    # RuntimeError:probe_scores._post_json 把 HTTPError 重抛成 RuntimeError
                    # ——语义 = 该臂延迟门 FAIL(timeout/error 一律 FAIL,协议冻结),
                    # 但 phase6 继续跑其余臂,整跑不崩。
                    failed = True
                    error_msg = str(e)
                    break
                elapsed = time.monotonic() - start
                if rep >= args.latency_warmup:  # 每查询前 N 次弃(warm-up)
                    times.append(elapsed)
            if failed:
                break

        if failed:
            report["arms"][arm_name] = {"latency_gate": "FAIL", "error": error_msg}
            continue

        p95 = _p95_nearest_rank(times) if times else 0.0
        latency_gate = "PASS" if p95 <= args.latency_gate_seconds else "quality-only"
        report["arms"][arm_name] = {
            "n_timed_samples": len(times),
            "p95_seconds": p95,
            "gate_seconds": args.latency_gate_seconds,
            "latency_gate": latency_gate,
        }

    return report


# ======================================================================
# main
# ======================================================================

def _write_results(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--second-judge-dir", required=True)
    ap.add_argument("--infinity-base", required=True)
    ap.add_argument("--embed-model", default="BAAI/bge-m3")
    ap.add_argument("--rerank-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--embedding-dim", type=int, default=1024)
    ap.add_argument("--pair-budget", type=int, default=8192)
    ap.add_argument("--cache-path", default=None)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--code-git-sha", default=None)
    ap.add_argument("--contamination-floor-override", type=float, default=None,
                     help="仅供测试注入(smoke 构造边界用);生产路径不传——floor 由 runner "
                          "对未过滤 top5 按 §P5 正式计分规则实算基线宏 FDR 后 round(基线/2, 2) 自动推导")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--drift-rounds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260715)
    ap.add_argument("--latency-queries", type=int, default=30)
    ap.add_argument("--latency-reps", type=int, default=50)
    ap.add_argument("--latency-warmup", type=int, default=3)
    ap.add_argument("--latency-timeout", type=int, default=10)
    ap.add_argument("--latency-gate-seconds", type=float, default=2.0)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    results_path = out_dir / "results.json"
    state: dict = {"status": "running", "phases": {}}

    def _fail(phase_name: str, exc: Exception) -> int:
        state["status"] = "error"
        state["failed_phase"] = phase_name
        state["error"] = f"{type(exc).__name__}: {exc}"
        _write_results(results_path, state)
        print(f"probe_guard_mechanism: FAILED at {phase_name}: {exc}", file=sys.stderr)
        return 1

    try:
        ctx = phase0_integrity(args)
    except Exception as e:  # noqa: BLE001 - phase 级 fail-loud,终态写盘不裸崩
        return _fail("phase0_integrity", e)
    state["phases"]["phase0_integrity"] = ctx["topology"] | {
        "manifest_sha": ctx["manifest_sha"], "decoy_sha": ctx["decoy_sha"],
        "code_git_sha": ctx["code_git_sha"], "uv_lock_sha": ctx["uv_lock_sha"],
        "window": ctx["window"].as_meta(),
    }
    _write_results(results_path, state)

    # contamination floor 自动推导(phase3 之前;不依赖任何臂分数,只吃 gold + 未过滤 top5)
    try:
        floor_info = derive_contamination_floor(ctx, args)
    except Exception as e:
        return _fail("contamination_floor_derivation", e)
    args.contamination_floor = floor_info["floor"]  # 后续 phase3/4/5 统一从这里取
    state["contamination_floor"] = floor_info
    _write_results(results_path, state)

    try:
        kc = phase1_known_control(ctx, args)
    except Exception as e:
        return _fail("phase1_known_control", e)
    state["phases"]["phase1_known_control"] = kc
    _write_results(results_path, state)

    try:
        scoring = phase2_scoring(ctx, args)
    except Exception as e:
        return _fail("phase2_scoring", e)
    state["phases"]["phase2_scoring"] = {
        "spec_sha": scoring["spec_sha"], "cache_rejected": scoring["cache_rejected"],
        "n_scored_queries": len(scoring["sq_by_qid"]),
    }
    _write_results(results_path, state)

    try:
        phase3 = phase3_fit_and_cv(ctx, scoring, args)
    except Exception as e:
        return _fail("phase3_fit_and_cv", e)
    state["phases"]["phase3_fit_and_cv"] = {
        "per_arm": phase3["per_arm"],
        "layer2": phase3["layer2"],
    }
    _write_results(results_path, state)

    try:
        phase4 = phase4_sensitivity(ctx, scoring, phase3, args)
    except Exception as e:
        return _fail("phase4_sensitivity", e)
    state["phases"]["phase4_sensitivity"] = phase4
    _write_results(results_path, state)

    # phase2+phase4 共用同一个 ScoreCache 对象(full spec/D2/D3 都写进去了),统一在
    # 这里落盘一次——早存(比如 phase2 刚打完 prod 分就存)会让 phase4 追加的条目丢失。
    if args.cache_path:
        scoring["cache"].save()

    try:
        phase5 = phase5_drift(ctx, scoring, phase3, args)
    except Exception as e:
        return _fail("phase5_drift", e)
    state["phases"]["phase5_drift"] = phase5
    _write_results(results_path, state)

    try:
        phase6 = phase6_latency(ctx, phase3, args)
    except Exception as e:
        return _fail("phase6_latency", e)
    state["phases"]["phase6_latency"] = phase6

    state["status"] = "done"
    _write_results(results_path, state)
    print(f"probe_guard_mechanism: done -> {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
