# distill/gold_gen.py
"""M4 gold 生成：双强模型按 rubric 抽 → 对齐计一致率 → 去重 → faithful 接地门 → duplicate 审计（LLM+词法双查）。无人工；retry+ledger。"""
import json, time, difflib
from distill import distiller

def _norm(a): return (a["entity"] + "|" + a["fact"]).lower().replace(" ", "")
def _lexical_dup_pairs(atoms, threshold=0.88):
    """确定性词法近重复对（difflib，不依赖 LLM，破单模型循环信任，codex R3 FIX-1）。"""
    pairs = []
    for i in range(len(atoms)):
        for k in range(i + 1, len(atoms)):
            if difflib.SequenceMatcher(None, _norm(atoms[i]), _norm(atoms[k])).ratio() >= threshold:
                pairs.append((i, k))
    return pairs

RUBRIC_PROMPT = (
    "你是记忆抽取标注器。按粒度规范从会话片段抽取值得长期记住的原子事实，输出 JSON。\n"
    "总原则=拆细：每条={entity,fact}，能作为某个独立问题的答案；宁拆细勿揉合。\n"
    "R1 不同维度/槽位各一条；R2 每个独立可查的参数/取值各一条；R3 多实体各自角色各一条（按各自实体归属）；R4「用A不用B」拆2。\n"
    "M1 同点不同措辞只留一条；M2 A↔B 关系只记一条（按被查端）；M3「X决定Y」中 X决定=来源属性不另开条；约束+理由同条；成就+多指标拆开。\n"
    "例外不炸开(v1.1)：①同质枚举——同一类事物的列表(一串工具名/组名/型号/示例)整体算1条，不逐项拆；②单条连贯的教训/原则即使含多个解释分句也算1条，不逐句拆；③选项枚举『A或B』(同一槽位多个可选)算1条。\n"
    "跳过噪声：heartbeat/寒暄/工具回执/无信息量来回 → atoms 为 []。每条 fact 必须被原文直接支撑，不外推。\n"
    "只输出 JSON 对象：{\"atoms\":[{\"entity\":\"<串>\",\"fact\":\"<串>\"}]}\n"
    "示例：[idx=0 user] 老兰定 LFT 前6月只模拟盘，资金5-10万封顶 → "
    "{\"atoms\":[{\"entity\":\"LFT\",\"fact\":\"前6个月只跑模拟盘\"},{\"entity\":\"LFT\",\"fact\":\"资金5-10万封顶\"}]}\n"
    "示例：[idx=0 user] 用 Qlib 不用 backtrader → {\"atoms\":[{\"entity\":\"LFT\",\"fact\":\"底座用 Qlib\"},{\"entity\":\"LFT\",\"fact\":\"不用 backtrader\"}]}\n"
    "示例(同质列表算1条)：[idx=0 user] 偏好压制组 FRDS、mUHD、ADE、CHD、CMCT → {\"atoms\":[{\"entity\":\"Ivan\",\"fact\":\"偏好知名压制组(FRDS/mUHD/ADE/CHD/CMCT)出品\"}]}\n"
    "示例(连贯教训算1条)：[idx=0 user] 教训：验证必须端到端，看到日志行不等于功能正常，须真实触发观察末端 → {\"atoms\":[{\"entity\":\"教训\",\"fact\":\"验证必须端到端，看到日志行不等于功能正常，须真实触发观察末端\"}]}\n"
    "示例：[idx=0 assistant] HEARTBEAT_OK → {\"atoms\":[]}"
)
_FAITHFUL_SYS="你是接地裁判。判断 atom 的 fact 是否被原文直接蕴含（entailment）；需外推/不被支撑=false。只输出 JSON：{\"faithful\":true|false}。"
_DEDUP_SYS="你是去重器。只合并**表达同一个事实的重复 atom**：(a) 同一事实的不同措辞(M1)；(b) 同一关系在两个实体名下各记一遍(M2，保留被查端一条)。**严禁过度合并**：同一实体/主题的不同属性、不同指标(如不同 FPS/功耗/设备数各算一条)、不同参数、不同配置项、『用A』与『不用B』、不同的决策/教训/待办——这些是各自独立的 atom，必须逐条保留，绝不揉成一条。宁可漏合并(有 dup 审计兜底)也不要过合并。保留信息最全的规范措辞。只输出 JSON：{\"atoms\":[{\"entity\":\"..\",\"fact\":\"..\"}]}。"
_ALIGN_SYS="你是对齐器。统计列表 A、B 中语义指同一事实的配对数。只输出 JSON：{\"matched\": <int>}。"
_DUP_SYS="你是重复审计器。只标**表达完全相同的一个事实(仅措辞不同)**的对为重复。**严禁误判**：约束与其后果/机理、决策与其理由、规则与其示例、同一主题/实体的不同方面——这些是各自独立的 atom，不是重复。宁可漏标也不要误标。只输出 JSON：{\"dups\":[[i,j],...]}，i<j 为完全同一事实的重复对；无则 []。"

def _cfg(b,k,m): return {"distill":{"base_url":b,"api_key":k,"model":m},"derived":{"distill_timeout_s":90}}
def _chat_retry(body,cfg,chat,attempts=3,ledger=None,stage="",span_id=""):
    chat=chat or distiller._chat_http; last=None
    for i in range(attempts):
        try:
            out=chat(body,cfg)
            if ledger is not None:
                ledger.write(json.dumps({"span":span_id,"stage":stage,"model":body.get("model"),"attempt":i,"raw":out},ensure_ascii=False)+"\n"); ledger.flush()
            return out
        except Exception as e: last=e; time.sleep(min(2**i,8))
    raise last
def _atoms_from(resp):
    assert isinstance(resp,dict) and "atoms" in resp, f"缺 atoms: {resp}"
    out=[]
    for a in resp["atoms"]:
        assert isinstance(a,dict) and {"entity","fact"}<=set(a), f"atom 非法: {a}"
        assert isinstance(a["entity"],str) and a["entity"].strip() and isinstance(a["fact"],str) and a["fact"].strip(), f"空字段: {a}"
        out.append({"entity":a["entity"].strip(),"fact":a["fact"].strip()})
    return out
def _body(model,sys,user,temp=0,mx=4000):
    return {"model":model,"temperature":temp,"max_tokens":mx,"response_format":{"type":"json_object"},
            "messages":[{"role":"system","content":sys},{"role":"user","content":user}]}
def extract_atoms(span_rows,model,base,key,temp,chat=None,ledger=None,span_id=""):
    return _atoms_from(_chat_retry(_body(model,RUBRIC_PROMPT,distiller._render(span_rows),temp),_cfg(base,key,model),chat,ledger=ledger,stage="extract",span_id=span_id))
def align_count(a,b,j,chat=None,ledger=None,span_id=""):
    if not a or not b: return 0
    u=f"A={json.dumps(a,ensure_ascii=False)}\nB={json.dumps(b,ensure_ascii=False)}"
    return int(_chat_retry(_body(j["model"],_ALIGN_SYS,u,0,4000),_cfg(j["base_url"],j["api_key"],j["model"]),chat,ledger=ledger,stage="align",span_id=span_id).get("matched",0))
def dedup_atoms(atoms,j,chat=None,ledger=None,span_id=""):
    if len(atoms)<=1: return [{"entity":a["entity"],"fact":a["fact"]} for a in atoms]
    u=f"atoms={json.dumps([{'entity':a['entity'],'fact':a['fact']} for a in atoms],ensure_ascii=False)}"
    return _atoms_from(_chat_retry(_body(j["model"],_DEDUP_SYS,u),_cfg(j["base_url"],j["api_key"],j["model"]),chat,ledger=ledger,stage="dedup",span_id=span_id))
def is_faithful(atom,span_rows,j,chat=None,ledger=None,span_id=""):
    u=f"原文：\n{distiller._render(span_rows)}\n\n待判 atom：{json.dumps(atom,ensure_ascii=False)}"
    return bool(_chat_retry(_body(j["model"],_FAITHFUL_SYS,u,0,4000),_cfg(j["base_url"],j["api_key"],j["model"]),chat,ledger=ledger,stage="faithful",span_id=span_id).get("faithful"))
def duplicate_audit(atoms,j,chat=None,ledger=None,span_id=""):
    if len(atoms)<=1: return []
    u=f"atoms={json.dumps([{'i':i,'entity':a['entity'],'fact':a['fact']} for i,a in enumerate(atoms)],ensure_ascii=False)}"
    out=_chat_retry(_body(j["model"],_DUP_SYS,u,0,4000),_cfg(j["base_url"],j["api_key"],j["model"]),chat,ledger=ledger,stage="dupaudit",span_id=span_id)
    return [tuple(p) for p in out.get("dups",[]) if isinstance(p,(list,tuple)) and len(p)==2]
def _all_dups(atoms,j,chat,ledger,span_id):
    """LLM 审计 ∪ 确定性词法：任一发现重复即算（不把可信只押在同族 LLM 上）。"""
    return sorted(set(duplicate_audit(atoms,j,chat,ledger,span_id)) | set(_lexical_dup_pairs(atoms)))
def build_gold(span_rows,cfg,chat=None,ledger=None,span_id=""):
    g,j=cfg["goldgen"],cfg["judge"]
    a=extract_atoms(span_rows,g["model_a"],g["base_url"],g["api_key"],g.get("temp_a",1),chat,ledger,span_id)
    b=extract_atoms(span_rows,g["model_b"],g["base_url"],g["api_key"],g.get("temp_b",0),chat,ledger,span_id)
    both=min(align_count(a,b,j,chat,ledger,span_id),len(a),len(b))   # cap 防 union_only 负值（R3 FIX-7a）
    deduped=dedup_atoms(a+b,j,chat,ledger,span_id)
    grounded=[x for x in deduped if is_faithful(x,span_rows,j,chat,ledger,span_id)]
    dups=_all_dups(grounded,j,chat,ledger,span_id)
    if dups:                                            # dedup 漏并（LLM 或词法发现）→ 再 dedup + 接地 一次
        grounded=[x for x in dedup_atoms(grounded,j,chat,ledger,span_id) if is_faithful(x,span_rows,j,chat,ledger,span_id)]
        dups=_all_dups(grounded,j,chat,ledger,span_id)
    union_only=len(a)+len(b)-2*both     # both 已 cap，非负
    return {"atoms":grounded,"agreement":{"n_a":len(a),"n_b":len(b),"both":both,"union_only":union_only,
                                          "n_gold":len(grounded),"residual_dups":len(dups)}}
