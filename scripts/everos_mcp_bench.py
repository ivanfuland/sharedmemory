# scripts/everos_mcp_bench.py
"""Task 9:guard-overhead bench——影子检索 tool 调用本身的用户可见延迟。

设计要点(见任务简报,均为验收硬约束):
- 起一个**真实** `python -m everos_mcp.server` 子进程(不是 in-process 调用
  `_handle_search`)——外部计时量的是用户真正会体验到的路径:MCP 协议往返 +
  fastmcp 分发 + `_handle_search` 全链(ops fsync ×1 + 契约门 + checkpoint 读 +
  upstream.search + 候选快照/blobstore 写 + accepted fsync + ops terminal
  fsync)+ HTTP 响应序列化。这是"guard 本身的开销"这句话字面意义上要求的
  路径,in-process 调用会跳过 MCP 协议层和真实进程边界,不是同一件事。
- EverOS stub 按查询文本确定性生成 10+10 张唯一候选(`_unique_envelope_for_query`)
  ——固定候选只有第 1 轮产生新 blob,后面全是缓存热路径,冷路径观测会失真;
  逐查询内容唯一后,"30 条首见查询"在它们各自第一次出现时才是真正的冷路径,
  且同一查询的后续 49 次重复天然走缓存热路径(内容寻址,sha 相同即幂等)。
- 30 条固定查询 × 50 轮 = 1500 次调用,弃前 3 轮(热身:JIT/tokenizer 懒加载/
  首轮全冷 blob 写入),有效样本 47*30=1410 条,nearest-rank p95。
- 首见查询(每条查询第一次出现,必然落在第 0 轮,也就是被丢弃的热身轮内)
  的计时单独收集、单独报告,不参与 p95 门(它们本就该慢——冷路径是预期
  内的,不是需要断言的对象)。
- traffic_class=synthetic_bench,不计入 real_query_count/checkpoint 判据。
- 门:p95 <= 2000ms。

用法:
    uv run --frozen --group mcp-shadow python scripts/everos_mcp_bench.py \
        [--out /path/to/report.json] [--reps 50] [--discard-rounds 3]

退出码:0=通过门禁,1=未通过或运行失败。JSON 报告写 stdout,`--out` 额外落盘。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path

from scripts import _everos_mcp_stubs as stubs

_QUERY_COUNT = 30
_DEFAULT_REPS = 50
_DEFAULT_DISCARD_ROUNDS = 3
_P95_GATE_MS = 2000.0


def _bench_queries() -> list[str]:
    """30 条互不相同、合法(<=150 字符、无换行)的合成查询文本。"""
    return [f"everos-mcp-bench-synthetic-query-{i:02d}-guard-overhead-probe" for i in range(_QUERY_COUNT)]


def nearest_rank_percentile(samples: list[float], p: float) -> float:
    """nearest-rank 方法:排序后取 `ceil(p/100 * n)` 名(1-indexed)。"""
    if not samples:
        raise ValueError("nearest_rank_percentile: 空样本集")
    s = sorted(samples)
    n = len(s)
    rank = math.ceil(p / 100.0 * n)
    rank = max(1, min(rank, n))
    return s[rank - 1]


_MAX_ROUND_ATTEMPTS = 5


async def _run_one_round(client, queries: list[str], round_idx: int, seen: set[str]) -> list[dict]:
    round_samples: list[dict] = []
    for q_idx, q in enumerate(queries):
        cold = q not in seen
        seen.add(q)
        t0 = time.perf_counter()
        res = await client.call_tool("everos_search", {"task": q, "limit": 5})
        dt_ms = (time.perf_counter() - t0) * 1000.0
        round_samples.append({
            "round": round_idx, "query_index": q_idx, "ms": dt_ms,
            "cold_first_seen": cold, "status": res.data.get("status"),
        })
    return round_samples


async def _run_calls(url: str, token: str, queries: list[str], reps: int) -> tuple[list[dict], list[dict]]:
    """每轮(30 次调用)开一个新的 MCP session,且每轮带重试(见
    `_MAX_ROUND_ATTEMPTS`)——**不是**为了省协议握手开销而刻意用一个跨全程的
    持久 session:早期实测单一持久 streamable-http session 在跑到 ~30 轮附近
    偶发 `httpx.ReadTimeout`/连接被外部中断,且改成每轮重连后**同一现象仍在
    相近调用量附近复现**(独立重跑两次分别死在 call≈930/1104,quic 环境下
    尝试不同 timeout 参数均未根治)——判定为宿主/沙箱层面的瞬时中断,不是
    应用层 bug(单次调用延迟全程正常,无逐步变慢的迹象)。生产 MCP 客户端本就
    该能扛住偶发网络抖动,故这里按轮加重试:某轮任意一次调用抛异常,整轮弃
    重做(用新 session),最多 `_MAX_ROUND_ATTEMPTS` 次;`seen` 集合在重试前
    回滚到本轮开始前的快照,保证 cold_first_seen 判定不因重试而失真。连续
    失败到达上限才真正向上抛(那时才是需要人看的真故障)。

    P1e(重要口径变化):这套按轮重试机制原本是为了修一个**已经修好的** harness
    bug 而加的诊断/容错手段——但保留至今会把"这一轮其实撞到了真实传输层故障"
    这件事悄悄洗掉:重试成功后整轮样本照常计入 effective,gate 只看 p95,
    对"需要重试才能跑通"这件事视而不见。机制本身保留(诊断价值仍在——某轮
    偶发失败时不必让整个 900s bench 直接报废),但**每轮实际用掉几次 attempt**
    现在会被如实记录并随 samples 一起返回给调用方,由 `_build_report` 判定
    "通过的 run 必须是 0 retries"——重试机制救得回本轮数据,但救不回"这是一
    个零重试的干净 run"这个更强的断言。"""
    samples: list[dict] = []
    round_retries: list[dict] = []
    seen: set[str] = set()
    for round_idx in range(reps):
        round_t0 = time.perf_counter()
        seen_snapshot = set(seen)
        last_exc: Exception | None = None
        round_samples: list[dict] | None = None
        attempts_used = 0
        for attempt in range(_MAX_ROUND_ATTEMPTS):
            seen.clear()
            seen.update(seen_snapshot)
            attempts_used = attempt + 1
            try:
                client = stubs.Client(url, auth=token)
                async with client:
                    round_samples = await _run_one_round(client, queries, round_idx, seen)
                break
            except Exception as e:  # noqa: BLE001 —— 偶发连接中断,整轮重做,不让全程崩掉
                last_exc = e
                print(
                    f"[bench] round {round_idx + 1}/{reps} attempt {attempt + 1}/"
                    f"{_MAX_ROUND_ATTEMPTS} 失败: {type(e).__name__}: {e} —— 重连重做本轮",
                    file=sys.stderr, flush=True,
                )
        if round_samples is None:
            raise RuntimeError(
                f"round {round_idx + 1}/{reps} 连续 {_MAX_ROUND_ATTEMPTS} 次重试仍失败"
            ) from last_exc

        round_retries.append({"round": round_idx, "attempts": attempts_used})
        samples.extend(round_samples)
        print(
            f"[bench] round {round_idx + 1}/{reps} done in "
            f"{(time.perf_counter() - round_t0) * 1000.0:.0f}ms "
            f"(total calls so far: {len(samples)}, attempts used: {attempts_used})",
            file=sys.stderr, flush=True,
        )
    return samples, round_retries


def _build_report(samples: list[dict], round_retries: list[dict], *, discard_rounds: int, reps: int) -> dict:
    """P1e:gate 现在是三门联合(AND),不再只看 p95:
    ① p95 <= 门槛;② **全部**样本(含被丢弃的热身轮,不只是 effective 集合)
    零非 "hit" 状态——热身轮只是为了不把 JIT/tokenizer 懒加载/首轮冷 blob
    写入计入 p95 计时,不代表热身轮里出现的非 hit 状态可以被容忍:这套合成
    拓扑本就始终有候选,任何一轮(不管是不是被丢弃的热身轮)出现非 hit 状态
    都是真故障或 stub 拓扑坏了,必须 FAIL 而不是被悄悄放过(P1i:此前只查
    effective 集合,热身轮里的非 hit 状态会被完全忽略);③ 零重试(此前重试
    机制会把"这一轮撞到真实传输层故障"洗成"整轮正常完成",gate 对此视而不见
    ——现在任何一轮需要 >1 次 attempt 都直接判 FAIL,并在报告里点名是哪几轮)。"""
    effective = [s for s in samples if s["round"] >= discard_rounds]
    cold = [s for s in samples if s["cold_first_seen"]]
    warm_effective = [s for s in effective if not s["cold_first_seen"]]

    effective_ms = [s["ms"] for s in effective]
    p50 = nearest_rank_percentile(effective_ms, 50)
    p95 = nearest_rank_percentile(effective_ms, 95)
    p99 = nearest_rank_percentile(effective_ms, 99)

    cold_ms = [s["ms"] for s in cold]
    # P1i:非 hit 状态检查覆盖**全部**样本(含热身轮),不再局限于 effective 集合
    # ——热身轮只对 p95 计时门槛不计分,不代表它对"库存拓扑该始终命中"这条
    # 断言免检。
    non_hit_statuses = [s["status"] for s in samples if s["status"] != "hit"]

    retried_rounds = [r for r in round_retries if r["attempts"] > 1]
    total_retries = sum(r["attempts"] - 1 for r in round_retries)

    p95_ok = p95 <= _P95_GATE_MS
    all_hit_ok = not non_hit_statuses
    zero_retries_ok = total_retries == 0
    gate_passed = p95_ok and all_hit_ok and zero_retries_ok

    return {
        "gate": {
            "metric": "p95_ms_effective_warm_path", "threshold_ms": _P95_GATE_MS,
            "value_ms": p95, "passed": gate_passed,
            "p95_ok": p95_ok, "all_hit_ok": all_hit_ok, "zero_retries_ok": zero_retries_ok,
        },
        "config": {
            "query_count": _QUERY_COUNT, "reps": reps, "discard_rounds": discard_rounds,
            "total_calls": len(samples), "effective_sample_count": len(effective_ms),
            "expected_effective_sample_count": (reps - discard_rounds) * _QUERY_COUNT,
        },
        "effective_warm_path": {
            "sample_count": len(effective_ms), "p50_ms": p50, "p95_ms": p95, "p99_ms": p99,
            "min_ms": min(effective_ms), "max_ms": max(effective_ms),
            "mean_ms": sum(effective_ms) / len(effective_ms),
        },
        "cold_first_seen_path": {
            "sample_count": len(cold_ms),
            "min_ms": min(cold_ms) if cold_ms else None,
            "max_ms": max(cold_ms) if cold_ms else None,
            "mean_ms": (sum(cold_ms) / len(cold_ms)) if cold_ms else None,
            "note": "首见查询必然落在被丢弃的热身轮内,不计入门禁,单独报告仅供参考。",
        },
        "warm_only_sanity": {
            "sample_count": len(warm_effective),
            "note": "effective 集合里排除掉 cold_first_seen 后的子集——正常情况下应与 "
                     "effective_warm_path 几乎相同(discard_rounds>=1 时首见样本已被丢弃)。",
        },
        "non_hit_statuses": non_hit_statuses,
        "total_retries": total_retries,
        "retried_rounds": retried_rounds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="额外把 JSON 报告写到此路径")
    parser.add_argument("--reps", type=int, default=_DEFAULT_REPS)
    parser.add_argument("--discard-rounds", type=int, default=_DEFAULT_DISCARD_ROUNDS)
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    args = parser.parse_args(argv)

    queries = _bench_queries()

    with tempfile.TemporaryDirectory(prefix="everos-mcp-bench-") as tmp_str:
        tmp = Path(tmp_str)
        bin_dir = stubs.make_stub_docker(tmp / "bin")
        dirs = stubs.build_isolated_dirs(tmp / "iso")
        infinity = stubs.InfinityStub()
        everos = stubs.EverosStub()
        port = stubs.free_port()

        env = stubs.subprocess_env(
            bin_dir=bin_dir, ledger_dir=dirs["ledger_dir"], instance_dir=dirs["instance_dir"],
            pin_file=dirs["pin_file"], everos_base=everos.base_url, infinity_base=infinity.base_url,
            port=port, traffic_class="synthetic_bench",
        )
        token = env["EVEROS_MCP_TOKEN"]

        proc = stubs.spawn_server(env)
        report: dict = {}
        try:
            stubs.wait_ready(port, token, timeout=args.startup_timeout, proc=proc)

            url = f"http://127.0.0.1:{port}/mcp"
            t_wall_start = time.monotonic()
            samples, round_retries = asyncio.run(_run_calls(url, token, queries, args.reps))
            wall_seconds = time.monotonic() - t_wall_start

            report = _build_report(
                samples, round_retries, discard_rounds=args.discard_rounds, reps=args.reps
            )
            report["wall_seconds"] = wall_seconds
        finally:
            rc, out = stubs.terminate_and_collect(proc)
            report["server_exit_code"] = rc
            if rc not in (0, 143):
                report["server_output_tail"] = out[-4000:]
            infinity.shutdown()
            everos.shutdown()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = report.get("gate", {}).get("passed", False)
    if not passed:
        print(f"BENCH GATE FAILED: {report.get('gate')}", file=sys.stderr)
        if report.get("non_hit_statuses"):
            print(
                f"  non-hit statuses (any round, incl. discarded warmup): "
                f"{report['non_hit_statuses']}",
                file=sys.stderr,
            )
        if report.get("retried_rounds"):
            print(
                f"  rounds that needed a retry (must be 0 for a passing run): "
                f"{report['retried_rounds']}",
                file=sys.stderr,
            )
        return 1
    print(
        f"BENCH GATE PASSED: p95={report['gate']['value_ms']:.1f}ms "
        f"<= {report['gate']['threshold_ms']:.0f}ms "
        f"(n={report['effective_warm_path']['sample_count']}, "
        f"retries=0, all hit)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
