import json, os, sys, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(__file__))
from audit import audit_append

SYNTHETIC = "张三在 2026-06-23 决定把蒸馏模型从本地 27b 换成云 API。"   # 红线：仅合成文本
SCHEMA = {
    "name": "distill_extract",
    "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["entities", "facts"],
        "properties": {
            "entities": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "kind"],
                "properties": {"name": {"type": "string"}, "kind": {"type": "string"}}}},
            "facts": {"type": "array", "items": {"type": "string"}},
        }},
    "strict": True,
}

def _validate(d):
    # 精确 key 匹配 = 验 schema 的 additionalProperties:false（>= 会放过 provider 没真守 strict 的额外字段）
    assert isinstance(d, dict) and set(d) == {"entities", "facts"}, f"顶层非 strict（有额外字段?）: {set(d)}"
    assert isinstance(d["entities"], list) and isinstance(d["facts"], list)
    for e in d["entities"]:
        assert isinstance(e, dict) and set(e) == {"name", "kind"}, f"entity 非 strict: {e}"
        assert isinstance(e["name"], str) and isinstance(e["kind"], str)
    for f in d["facts"]:
        assert isinstance(f, str)
    return d

def distill_once(audit_path=None):
    base = os.environ["DISTILL_BASE_URL"].rstrip("/"); key = os.environ["DISTILL_API_KEY"]
    model = os.environ["DISTILL_MODEL"]
    body = {"model": model, "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": SCHEMA},
            "messages": [{"role": "system", "content": "你是实体抽取器，严格按 schema 输出。"},
                         {"role": "user", "content": SYNTHETIC}]}
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        if e.code in (400, 422) and any(k in detail for k in ("response_format", "json_schema", "schema")):
            raise AssertionError(f"provider 不支持 strict json_schema（M1 暴露）: {e.code} {detail}")
        kind = {401: "auth: API key 无效", 403: "auth: 无权限",
                404: "model 不存在", 429: "rate/quota 超限"}.get(e.code, f"HTTP {e.code}")
        raise AssertionError(f"distill 端点失败 [{kind}]: {e.code} {detail}")
    parsed = _validate(json.loads(out["choices"][0]["message"]["content"]))
    audit_append(session_ref="SYNTHETIC", bytes_out=len(SYNTHETIC.encode()), model=model, path=audit_path)
    return parsed

if __name__ == "__main__":
    print(json.dumps(distill_once(), ensure_ascii=False, indent=2))
