#!/usr/bin/env python3
"""restore-cass.sh step 8 的 doctor 验证门（spec §4.3 step 8 / §2.10）。

从 **stdin** 读 `cass doctor --json` 输出，判据：
  - `raw_mirror.summary` 的 5 个零计数器全部 == 0，且
  - `verified_blob_count > 0`。

`verified_blob_count > 0` 是硬断言：**零错误与「根本没检查」在计数器上长得一模一样**——
受限沙箱里 doctor 曾 1.14 s 返回 status=verified 却没真校验 ~1.74 GB blob（spec §2.10 / 行 147）。
删一个 blob 会让 status verified→warn、missing_blob_count 2、verified_blob_count 少 1。

exit 0 = 通过；非 0 = 失败（stderr 带原因）。纯 stdlib（`json`），无 blake3 依赖，任何 python3 可跑。
"""
from __future__ import annotations

import json
import sys

# spec §4.3 step 8：这 5 个必须全 0（缺失也算失败——不能验证 = 不通过）
_ZERO_COUNTERS = (
    "missing_blob_count",
    "checksum_mismatch_count",
    "manifest_checksum_mismatch_count",
    "invalid_manifest_count",
    "interrupted_capture_count",
)


def _as_int(value) -> int | None:
    """**严格**：只接受真正的 JSON int（排除 bool；数字字符串也拒）。doctor 计数器本该是 int，
    非 int（含 "0"/"3284" 这类数字字符串）即异常 → fail-closed 拒绝，对齐仓内 Tier0 gate 口径。"""
    if isinstance(value, bool):
        return None  # bool 是 int 子类，但计数器不该是布尔
    if type(value) is int:
        return value
    return None


def check(doctor_json: str) -> str:
    """通过返回一行成功文案；失败抛 SystemExit(带原因，非 0)。fail-closed：任何异常/不一致即拒。"""
    try:
        d = json.loads(doctor_json)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[restore] FATAL: doctor json 解析失败: {e}")
    if not isinstance(d, dict):
        raise SystemExit("[restore] FATAL: doctor json 顶层不是对象")
    rm = d.get("raw_mirror", {})
    if not isinstance(rm, dict):
        raise SystemExit("[restore] FATAL: doctor 缺 raw_mirror")
    # raw_mirror.status 必须 verified（删 blob 会让它 verified→warn，spec 行 1361）。summary 全 0 但
    # status=warn 是自相矛盾 → fail-closed 拒。注意：这是 raw_mirror.status，不是有假阳性的顶层 status。
    status = rm.get("status")
    if status != "verified":
        raise SystemExit(f"[restore] FATAL: raw_mirror.status={status!r} != 'verified'（fail-closed）")
    summ = rm.get("summary", {})
    if not isinstance(summ, dict) or not summ:
        raise SystemExit("[restore] FATAL: doctor 缺 raw_mirror.summary（计数器路径是 raw_mirror.summary.*）")

    bad = []
    for k in _ZERO_COUNTERS:
        v = _as_int(summ.get(k))
        if v is None or v != 0:
            bad.append((k, summ.get(k)))
    if bad:
        raise SystemExit(f"[restore] FATAL: raw_mirror.summary 非零/缺失: {bad}")

    vbc = _as_int(summ.get("verified_blob_count"))
    if vbc is None or vbc <= 0:
        raise SystemExit(
            f"[restore] FATAL: verified_blob_count={summ.get('verified_blob_count')!r} <= 0"
            "（零错误与「没检查」在计数器上同形，必须 >0 证明真验过 blob）"
        )
    return f"[restore] step 8 OK：raw_mirror.summary 全 0，verified_blob_count={vbc}"


if __name__ == "__main__":
    print(check(sys.stdin.read()))
