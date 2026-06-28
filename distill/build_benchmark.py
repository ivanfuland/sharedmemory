"""dev 自检+校准 + secret 门 + 回填至35 + reliability 门 + fingerprint resume + partial→final 原子落盘。"""
import os, json, hashlib
from collections import Counter
from distill import gold_gen, quality_eval, secret_scan
EVAL_N=35; CALIB_MARGIN=0.15; UNION_ONLY_CEIL=0.50; RUBRIC_VERSION="v1.0"
FINAL="fixtures/m4-distill-eval.json"; PARTIAL="fixtures/m4-distill-eval.partial.json"; RESUME="fixtures/m4-goldgen-resume.jsonl"
def _cfg():
    j={"model":os.environ["JUDGE_MODEL"],"base_url":os.environ["JUDGE_BASE_URL"],"api_key":os.environ["JUDGE_API_KEY"]}
    return {"goldgen":{"model_a":os.environ["GOLDGEN_MODEL_A"],"model_b":os.environ["GOLDGEN_MODEL_B"],
                       "base_url":os.environ["GOLDGEN_BASE_URL"],"api_key":os.environ["GOLDGEN_API_KEY"],"temp_a":1,"temp_b":0},"judge":j}
def _judge_cfg(cfg): return {"distill":{**cfg["judge"]},"derived":{"distill_timeout_s":90}}   # 含 derived 防 selfcheck 崩
def fingerprint(cfg):
    """benchmark 指纹：rubric 版本 + 所有 gold_gen/judge prompt + 模型名 + seed + secret_scan 版本。
    任一变即令旧 record 失配（防新旧制度 gold 混入，R3 FIX-2/NEW）。"""
    parts=[RUBRIC_VERSION,gold_gen.RUBRIC_PROMPT,gold_gen._DEDUP_SYS,gold_gen._FAITHFUL_SYS,gold_gen._ALIGN_SYS,
           gold_gen._DUP_SYS,cfg["goldgen"]["model_a"],cfg["goldgen"]["model_b"],cfg["judge"]["model"],
           "seed20260627",getattr(secret_scan,"VERSION","1")]
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:16]
def selfcheck_on_control(cfg,ledger):
    ctrl=json.load(open("fixtures/m4-synthetic-control.json",encoding="utf-8")); jc=_judge_cfg(cfg)
    tg=te=tm=dups=na=nb=both2=0
    for i,s in enumerate(ctrl):
        r=gold_gen.build_gold(s["span"],cfg,ledger=ledger,span_id=f"ctrl-{i}"); gen=r["atoms"]; ag=r["agreement"]
        dups+=ag["residual_dups"]; na+=ag["n_a"]; nb+=ag["n_b"]; both2+=2*ag["both"]
        m=min(quality_eval.match_count(s["gold"],gen,jc,None),len(s["gold"]),len(gen)) if (s["gold"] or gen) else 0
        tg+=len(s["gold"]); te+=len(gen); tm+=m
    uo=round(1-both2/(na+nb),3) if (na+nb) else 0.0
    return {"precision":round(tm/te,3) if te else 1.0,"recall":round(tm/tg,3) if tg else 1.0,"residual_dups":dups,"union_only_frac":uo}
def main():
    cfg=_cfg(); fp=fingerprint(cfg); os.makedirs("fixtures",exist_ok=True)
    # resume：从独立缓存读、且 _fp 匹配（per-span gold 缓存正当复用；FINAL 只由 rename 产生，不持有未过门数据）
    done={}
    if os.path.exists(RESUME):
        for line in open(RESUME,encoding="utf-8"):
            x=json.loads(line)
            if x.get("_fp")==fp: done[(x["_meta"]["conv_id"],x["_meta"]["win_start"])]=x
    with open("fixtures/m4-goldgen-ledger.jsonl","a",encoding="utf-8") as ledger, open(RESUME,"a",encoding="utf-8") as rf:
        sc=selfcheck_on_control(cfg,ledger)
        print(f"[dev selfcheck] P={sc['precision']} R={sc['recall']} dups={sc['residual_dups']} dev_union_only={sc['union_only_frac']}")
        # dev 门 v1.1（Ivan option A，2026-06-28）：召回为主。手写 gold 的精确粒度是主观尺，
        # 在 compound-spec/协议 边界与 pipeline 分歧惩罚了可辩护的更细选择；全 atom faithful 已由 build_gold 接地门保证。
        # 真判别力在 Task 8 flash-vs-mini 配对(两者都对同一份 pipeline 共识 gold)。
        assert sc["recall"]>=0.9 and sc["precision"]>=0.75 and sc["residual_dups"]==0, \
            f"dev 自检: R={sc['recall']}(需≥0.9) P={sc['precision']}(软地板≥0.75) dups={sc['residual_dups']}(需0) → 补 prompt（只在 dev 调）"
        uo_max=min(UNION_ONLY_CEIL, sc["union_only_frac"]+CALIB_MARGIN)   # 用 dev 实测校准 real 阈值（非拍脑袋）
        pool=json.load(open("fixtures/m4-real-pool.json",encoding="utf-8"))
        dev=json.load(open("fixtures/m4-synthetic-control.json",encoding="utf-8"))
        kept=[]; skipped=0; gen_failed=0; agreements=[]
        for s in pool:
            if len(kept)>=EVAL_N: break
            key=(s["_meta"]["conv_id"],s["_meta"]["win_start"])
            if key in done: rec=done[key]
            else:
                hits=secret_scan.scan_span(s)
                if hits: skipped+=1; print(f"[secret] skip {key} hits={hits}"); continue
                try:
                    g=gold_gen.build_gold(s["span"],cfg,ledger=ledger,span_id=f"real-{key[0]}-{key[1]}")
                except Exception as e:   # 一条坏样本(模型返空/超时 retry 耗尽)不杀整 run → skip+refill（120 候选够回填）
                    gen_failed+=1; print(f"[goldgen-fail] skip {key}: {type(e).__name__} {str(e)[:80]}"); continue
                rec={"span":s["span"],"gold":g["atoms"],"split":"real","cluster":s["cluster"],"_meta":s["_meta"],"_agreement":g["agreement"],"_fp":fp}
                rf.write(json.dumps(rec,ensure_ascii=False)+"\n"); rf.flush()   # per-span 缓存，crash 可续
            kept.append(rec); agreements.append(rec["_agreement"])
        assert len(kept)==EVAL_N, f"real eval {len(kept)}/{EVAL_N}（pool 耗尽/secret {skipped}/genfail {gen_failed}）→ 扩 pool_size/降 min_chars"
        total=sum(a["n_a"]+a["n_b"] for a in agreements); union=sum(a["union_only"] for a in agreements)
        residual=sum(a["residual_dups"] for a in agreements); uo=round(union/total,3) if total else 0.0
        per_uo=[(a["union_only"]/(a["n_a"]+a["n_b"])) if (a["n_a"]+a["n_b"]) else 0 for a in agreements]
        sens={t:sum(1 for x in per_uo if x>t) for t in (0.2,0.3,0.45)}   # 多阈值 sensitivity（非单点）
        print(f"[reliability] real_union_only={uo} 校准上限={uo_max} residual={residual} secret_skipped={skipped} sens={sens}")
        assert residual==0, f"eval 残留 {residual} 漏并 → HOLD"
        assert uo<=uo_max, f"real union_only={uo} > 校准上限 {uo_max}（dev {sc['union_only_frac']}+{CALIB_MARGIN}）→ 分歧过大 HOLD"
        # 全门过 → 写 PARTIAL 再原子 rename FINAL（无效 fixture 绝不落 FINAL，防污染 resume）
        dist=Counter(r["cluster"] for r in kept)
        json.dump(dev+kept,open(PARTIAL,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
        json.dump({"n":len(kept),"fingerprint":fp,"by_agent":dict(dist),"real_union_only":uo,"union_only_ceiling":uo_max,
                   "sensitivity":sens,"secret_skipped":skipped,"spans":[r["_meta"] for r in kept]},
                  open("fixtures/m4-real-manifest.json","w",encoding="utf-8"),ensure_ascii=False,indent=2)
        os.replace(PARTIAL,FINAL)
        print(f"OK: {len(dev)+len(kept)} samples → {FINAL} (fp={fp}, by_agent={dict(dist)})")
if __name__=="__main__": main()
