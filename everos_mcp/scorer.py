# everos_mcp/scorer.py
"""影子打分 worker + 可复现 pins 采集 + reconciliation 扫描(P4 Task 7)。

规则(见任务简报,均为审查阻断项):
- `collect_pins(cfg)`:键集 = `materialize.PIN_KEYS` 精确一致;任一子项采集
  失败一律 raise(`PinCollectionError`),不落 "unknown"。`infinity_image_digest`
  取法:`docker inspect $INFINITY_CONTAINER --format '{{.Config.Image}}'`(该值
  本就是 digest 引用,因为 unit ExecStart 按 digest 启动);非 digest 形式则回退
  `docker image inspect $(docker inspect ... --format '{{.Image}}') --format
  '{{index .RepoDigests 0}}'`。`model_artifact_fp` 必须 `docker exec` 进容器内
  遍历 HF 缓存(权重在 named volume 里,宿主不可直读),follow symlink,对
  config.json/tokenizer 文件/`*.safetensors.index.json`/全部 `*.safetensors`
  分片按路径排序 sha256sum 后再整体 sha256。
- **marker 双取(逐 attempt,不是逐 sweep)**:每次 scoring attempt 读缓存前取
  一次容器 marker(`.Image`+`.State.StartedAt`),embed/rerank 完成后再取一次;
  两次不一致,或与当前已知 pin bundle 的 marker 不一致 -> 丢弃本次结果、
  **原子重建整份 pin bundle**(image digest + artifact fp + embedding_dim 一起
  换,不许只换一部分)并让向量缓存自然随 artifact_fp 变化失效,按重试路径重打。
- `collect_config_fp(cfg)`:accepted 行 config fingerprint 的生产者(Task 8
  消费);`everos_pin` 是 `cfg.pin_file` 的原文两因子,只读不重算,文件缺失即 raise。
- `collect_lib_counts(cfg)`:AgentCase 按**文件求和 entry_count**(不是按文件数
  ——一个日聚合文件可能有多条);目录不可读/frontmatter 解析失败 ->
  `LibCountsError`,调用方让该次 lib_counts 缺席 + 记 score_error_detail,不糊 0
  (lib_counts 不在 PIN_KEYS,不影响 `materialize.healthy` 的健康判定)。
- `ScoreWorker`:**单一后台线程消费单一队列**——这是"实时打分与
  reconciliation 互斥、并发 1"(spec 冻结,P1d)的实现方式:队列里的每一项
  记录 `(rid, producer, done_event)`,`_run()` 是唯一调用 `_score_once` 的
  地方,不存在第二个线程能触发打分计算。`enqueue(rid)` 非阻塞投递
  producer="realtime"、队列满丢任务返回 False(accepted 已落账,
  reconciliation 兜底补打);`enqueue_reconcile(rid)` 是 `reconcile()` 的唯一
  投递口,producer="reconciliation",同样非阻塞、满则丢弃(下一轮 sweep 会
  重新发现,不是数据丢失);`manual_rescore(rid)` 走同一队列(阻塞 `put` +
  等待 `done_event`,产出与旧版"直接同步调用"一致的调用方语义,但打分计算
  仍然只在 worker 线程执行)。打分:accepted 行 + 快照 ->
  `probe_scores.embed`(query 与缺缓存卡向量同批,注入 `http.post_json`)×磁盘
  卡向量缓存(键 = `(passage_sha, model, artifact_fp)`,天然随 artifact_fp
  变化失效)算 cos -> `probe_scores.rerank` 算 ce -> 构造 scored 行(不含
  attempt_no,由 writer 分配)提交 `Ledger.submit_scored`。瞬时失败(含 marker
  漂移)重试 <= 3 次(指数退避),仍败落 `retryable_error` 行。
- `reconcile(...)`:score_eligible accepted 与 terminal scored 的 anti-join
  + 累计失败计数(同 rid retryable_error >= 5 次 -> 落 `permanent_failure`,
  此后不再进入待补打集合)——这部分是纯 ledger 读写,不涉及打分计算,继续
  同步执行、不受"并发 1"约束。真正需要补打的 rid 一律经注入的 `enqueue_fn`
  (通常是 `ScoreWorker.enqueue_reconcile`)投递,逐条限速(批间隔默认 1s,
  限速的是"投递节奏"而不是"打分完成节奏"——打分本身异步发生在 worker 线程,
  reconcile() 本身不阻塞等打分结果)。
- 打分调用全在 worker 线程(同步 urllib 底座不进事件循环);出站一律经
  `everos_mcp.http.post_json` 注入进 `probe_scores.embed/rerank`。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from everos_eval import probe_passage, probe_scores
from everos_mcp import http
from everos_mcp.blobstore import BlobStore
from everos_mcp.config import Config
from everos_mcp.ledger import Ledger, effective_status, iter_rows, read_abort_rids, scored_row
from everos_mcp.materialize import PIN_KEYS, _card_key, fold, healthy, score_eligible

# ======================================================================
# 常量
# ======================================================================

# accepted 行 config_fp 的固定字段(spec 冻结值;文案版本变更时递增)。
TOP_K = 20
METHOD = "hybrid"
PAYLOAD_CAP = 8000
TOOL_DESC_VERSION = 1

# 容器内 HF 缓存位置——与 infra/systemd/cc-infinity.service 的
# `-e HF_HOME=/app/.cache/huggingface` 逐字一致(该 unit 文件已在仓内公开,
# 非新增拓扑字面量)。
_HF_HOME_IN_CONTAINER = "/app/.cache/huggingface"

# 模型指纹需要覆盖的文件类别:config + tokenizer 全家桶 + 权重分片索引/分片本体。
_ARTIFACT_FIND_NAMES = (
    "config.json",
    "*tokenizer*",
    "*.safetensors.index.json",
    "*.safetensors",
    "vocab.txt",
    "vocab.json",
    "merges.txt",
    "spm.model",
    "special_tokens_map.json",
)

_MAX_TRANSIENT_RETRIES = 3
_DEFAULT_RETRY_BACKOFF_BASE = 0.5  # seconds;指数退避 base * 2**attempt_index
_DEFAULT_RECONCILE_INTERVAL = 600.0
_DEFAULT_RECONCILE_BATCH_GAP = 1.0
_DEFAULT_FAIL_THRESHOLD = 5

# scored 行 producer 冻结枚举(spec 冻结值;materialize.py 的在线健康率只认
# "realtime" 字面量,别处出现的任何拼写漂移都会让本该算健康完成率的行悄悄
# 漏计)。P2:此前 `reconcile()` 落 `permanent_failure` 行时手误写成
# "reconcile"(非枚举值),与 `enqueue_reconcile`/`_WorkItem` 实际使用的
# "reconciliation" 不一致——两者都指向 scored 行的 `producer` 字段,必须
# 统一取自本枚举,不再各处手写字面量。
PRODUCERS = frozenset({"realtime", "reconciliation", "manual"})

_FILE_MODE = 0o600
_DIR_MODE = 0o700

_SENTINEL = object()


@dataclass
class _WorkItem:
    """`ScoreWorker` 单一队列里的一项(P1d):`rid`+`producer` 供 `_score_once`
    使用;`done_event` 仅 `manual_rescore` 用来阻塞等待完成,`enqueue`/
    `enqueue_reconcile` 走 fire-and-forget 路径,不设置它。"""

    rid: str
    producer: str
    done_event: "threading.Event | None" = None


# ======================================================================
# 异常
# ======================================================================

class PinCollectionError(Exception):
    """collect_pins/内部子项采集任一失败——绝不落 "unknown",直接向上抛。"""


class LibCountsError(Exception):
    """collect_lib_counts 采集失败(目录不可读/frontmatter 解析失败)。调用方
    (ScoreWorker)捕获后让该次 scored 行 lib_counts 缺席 + 记
    score_error_detail,不糊 0——lib_counts 不在 PIN_KEYS,不参与健康判定。"""


class MarkerDrift(Exception):
    """打分 attempt 期间容器 marker 漂移(双取不一致,或与当前 pin bundle 不
    一致)——调用方已原子重建 pin bundle,本异常只是让外层退避重试。"""


# ======================================================================
# 小工具:repo root / git sha / uv.lock sha
# ======================================================================

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_rev_parse_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise PinCollectionError(f"git rev-parse HEAD 失败: {result.stderr.strip()}")
    sha = result.stdout.strip()
    if not sha:
        raise PinCollectionError("git rev-parse HEAD 返回空")
    return sha


def _uv_lock_sha(repo_root: Path) -> str:
    path = repo_root / "uv.lock"
    if not path.is_file():
        raise PinCollectionError(f"uv.lock 不存在: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ======================================================================
# docker helpers(测试经 monkeypatch `_run_docker` 注入 fake,零真实 docker 依赖)
# ======================================================================

def _run_docker(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_inspect_format(container: str, fmt: str, timeout: float = 30.0) -> str:
    result = _run_docker(["inspect", container, "--format", fmt], timeout=timeout)
    if result.returncode != 0:
        raise PinCollectionError(
            f"docker inspect {container} --format {fmt!r} 失败"
            f"(rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def get_image_digest(container: str) -> str:
    """`RepoDigests` 是 image 对象字段,container inspect 没有——先取
    `.Config.Image`(unit 按 digest 启动,该值即 digest 形式);非 digest 形式则
    回退到 image 对象的 `RepoDigests[0]`。"""
    image_ref = _docker_inspect_format(container, "{{.Config.Image}}")
    if "@sha256:" in image_ref:
        return image_ref
    image_id = _docker_inspect_format(container, "{{.Image}}")
    result = _run_docker(["image", "inspect", image_id, "--format", "{{index .RepoDigests 0}}"])
    if result.returncode != 0:
        raise PinCollectionError(
            f"docker image inspect {image_id} 失败(rc={result.returncode}): {result.stderr.strip()}"
        )
    digest = result.stdout.strip()
    if not digest:
        raise PinCollectionError(f"容器 {container} 镜像 RepoDigests 为空,无法取 digest")
    return digest


def get_container_marker(container: str) -> str:
    """marker = 容器 `.Image` + `.State.StartedAt`——权重在 Infinity 启动时
    加载,容器不重启则运行中模型不变,这是重刷判据的机制来源。"""
    image = _docker_inspect_format(container, "{{.Image}}")
    started_at = _docker_inspect_format(container, "{{.State.StartedAt}}")
    if not image or not started_at:
        raise PinCollectionError(
            f"容器 {container} marker 字段缺失(.Image={image!r}, .State.StartedAt={started_at!r})"
        )
    return f"{image}@{started_at}"


def _artifact_find_expr() -> str:
    return " -o ".join(f"-name '{name}'" for name in _ARTIFACT_FIND_NAMES)


def compute_model_artifact_fp(container: str, timeout: float = 90.0) -> str:
    """容器内(HF 权重在 named volume,宿主不可直读)遍历 HF 缓存,follow
    symlink(HF snapshot 本是链接结构),对匹配文件排序 sha256sum 后整体 sha256。
    分片必须进指纹——否则单文件替换权重不改 index 即可骗过。"""
    script = (
        f"find -L {_HF_HOME_IN_CONTAINER}/hub -type f \\( {_artifact_find_expr()} \\) "
        "-exec sha256sum {} \\; | sort -k 2"
    )
    result = _run_docker(["exec", container, "sh", "-c", script], timeout=timeout)
    if result.returncode != 0:
        raise PinCollectionError(
            f"docker exec {container} 遍历 HF 缓存失败(rc={result.returncode}): {result.stderr.strip()}"
        )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        raise PinCollectionError(f"容器 {container} 内 HF 缓存目录未匹配到任何权重/tokenizer 文件")
    lines.sort(key=lambda ln: ln.split(None, 1)[1] if len(ln.split(None, 1)) == 2 else ln)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ======================================================================
# 本机(host)tokenizer artifact sha——host 侧独立 HF 缓存,不经过 docker
# ======================================================================

def compute_tokenizer_artifact_sha() -> str:
    """复用 `probe_passage` 的 tokenizer 定位(pinned RERANK 模型 revision)。
    本机 pinned HF snapshot 是宿主自己拉取的独立副本(与容器内权重无关),
    `local_files_only=True` 缺失直接抛(不静默降级)。"""
    from huggingface_hub import snapshot_download

    snap_dir = Path(snapshot_download(
        repo_id=probe_passage.RERANK_MODEL_ID,
        revision=probe_passage.RERANK_MODEL_REVISION,
        local_files_only=True,
    ))
    files = sorted(p for p in snap_dir.rglob("*") if p.is_file())
    if not files:
        raise PinCollectionError(f"tokenizer 快照目录为空: {snap_dir}")
    lines = []
    for p in files:
        file_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{file_sha}  {p.relative_to(snap_dir)}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def measure_embedding_dim(cfg: Config) -> int:
    """embedding_dim := 首次 embed 实测(不是读配置声明值)。"""
    vectors = probe_scores.embed(
        ["everos-mcp-scorer-embedding-dim-probe"],
        base_url=cfg.infinity_base, model=cfg.embed_model, timeout=30,
        post_json=http.post_json,
    )
    if not vectors or not vectors[0]:
        raise PinCollectionError("embedding_dim 探测: embed 返回空向量")
    return len(vectors[0])


# ======================================================================
# collect_pins() / _collect_pin_bundle()
# ======================================================================

def _assert_pins_known(pins: dict) -> None:
    missing = PIN_KEYS - set(pins.keys())
    if missing:
        raise PinCollectionError(f"pin bundle 缺键: {sorted(missing)}")
    unknown = [k for k in PIN_KEYS if pins[k] in (None, "unknown")]
    if unknown:
        raise PinCollectionError(f"pin bundle 含 None/unknown 值: {sorted(unknown)}")


def _collect_pin_bundle(cfg: Config) -> tuple[str, dict]:
    """返回 `(marker, pins)`。docker 相关两项(image digest + artifact fp)采集
    期间会再取一次 marker 核对是否漂移——采集本身也要对得起"原子"二字。"""
    container = cfg.infinity_container
    marker_before = get_container_marker(container)
    image_digest = get_image_digest(container)
    artifact_fp = compute_model_artifact_fp(container)
    marker_after = get_container_marker(container)
    if marker_before != marker_after:
        raise PinCollectionError(
            f"pin 采集期间容器 {container} marker 漂移"
            f"(采集前 {marker_before!r} != 采集后 {marker_after!r})——放弃本次采集"
        )

    embedding_dim = measure_embedding_dim(cfg)
    tokenizer_sha = compute_tokenizer_artifact_sha()
    window = probe_passage.run_window_probe(cfg.infinity_base, get_json=http.get_json)

    repo_root = _repo_root()
    pins = {
        "embed_model": cfg.embed_model,
        "rerank_model": cfg.rerank_model,
        "model_artifact_fp": artifact_fp,
        "tokenizer_artifact_sha": tokenizer_sha,
        "infinity_image_digest": image_digest,
        "embedding_dim": embedding_dim,
        "uv_lock_sha": _uv_lock_sha(repo_root),
        "passage_spec_sha_case": probe_passage.passage_spec_sha("prod", window.cap, "agent_case"),
        "passage_spec_sha_skill": probe_passage.passage_spec_sha("prod", window.cap, "agent_skill"),
        "cap": window.cap,
        "query_budget": window.query_budget,
        "scorer_git_sha": _git_rev_parse_head(repo_root),
    }
    _assert_pins_known(pins)
    return marker_after, pins


def collect_pins(cfg: Config) -> dict:
    """键集 = `materialize.PIN_KEYS` 精确一致;任一采集失败 raise
    `PinCollectionError`,不落 "unknown"(marker 是内部簿记,不对外暴露)。"""
    _, pins = _collect_pin_bundle(cfg)
    return pins


# ======================================================================
# collect_config_fp() / PinFileCache
# ======================================================================

def _read_pin_file(path: Path) -> str:
    if not path.is_file():
        raise PinCollectionError(f"EVEROS_PIN_FILE 不存在: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise PinCollectionError(f"EVEROS_PIN_FILE 为空: {path}")
    return text


class PinFileCache:
    """P2(R4 阻断项 #4):`everos_pin` 是**上游 everos-prod 进程**的属性,不是
    本进程的静态配置——everos-prod 重部署会换 PIN 文件内容,而 `collect_config_fp`
    此前只在 `bootstrap()` 时调用一次,结果被 `AppState.config_fp` 长期
    boot-cache 住,此后每一条 accepted 行(不管请求发生在进程启动后第几秒/
    第几天)都携带同一份陈旧 PIN——config fingerprint 因此对"upstream 已经
    redeploy 过"这件事完全失明。

    修法:PIN 必须逐请求重读,但真的每次都 `read_text()` 没必要——`os.stat()`
    是微秒级操作,只有 mtime/size 真的变化时才重新读文件内容并使缓存失效。
    静态字段(server_git_sha/agent_id/top_k/method/payload_cap/
    tool_desc_version)不受影响,继续在 `bootstrap()` 时算一次(见
    `collect_static_config_fp`)——它们不会在进程生命周期内变化,每请求重算
    没有意义,反而会让 `_git_rev_parse_head` 这类 subprocess 调用拖垮 p95。
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._cached: tuple[int, int, str] | None = None  # (mtime_ns, size, text)

    def read(self) -> str:
        """请求时刻 PIN 文件缺失/为空/不可读 -> `PinCollectionError`(调用方
        server.py 据此把该请求判为 config 采集失败,fail-closed 返回
        error_code="internal"——这是我们自己的配置层故障,不是上游 EverOS
        响应异常,不适用 upstream_fail 系列 error_code;选择 "internal" 并在
        此记录取舍)。

        P2:整条读路径(`stat()` + `read_text()`)统一 try/except——此前只
        `stat()` 的 `OSError` 被捕获,`read_text()` 抛出的 `PermissionError`
        (`OSError` 子类)/`UnicodeDecodeError` 会原样冒出未捕获异常,而不是
        统一映射成 `PinCollectionError`。任何一步失败都归一为同一种可判读
        故障信号,调用方不需要分别处理多种异常类型。"""
        try:
            st = self._path.stat()
            cached = self._cached
            if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                return cached[2]
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise PinCollectionError(f"EVEROS_PIN_FILE 不存在: {self._path}") from e
        except (OSError, UnicodeDecodeError) as e:
            raise PinCollectionError(f"EVEROS_PIN_FILE 读取失败: {self._path}: {e}") from e
        if not text.strip():
            raise PinCollectionError(f"EVEROS_PIN_FILE 为空: {self._path}")
        self._cached = (st.st_mtime_ns, st.st_size, text)
        return text


def collect_static_config_fp(cfg: Config) -> dict:
    """config fingerprint 里在进程生命周期内不变的静态部分——`bootstrap()`
    算一次即可,不含 `everos_pin`(那是每请求经 `PinFileCache` 重读的部分,
    见 `collect_config_fp` 文档)。"""
    return {
        "server_git_sha": _git_rev_parse_head(_repo_root()),
        "agent_id": cfg.agent_id,
        "top_k": TOP_K,
        "method": METHOD,
        "payload_cap": PAYLOAD_CAP,
        "tool_desc_version": TOOL_DESC_VERSION,
    }


def collect_config_fp(cfg: Config, pin_cache: "PinFileCache | None" = None) -> dict:
    """accepted 行 config fingerprint 的生产者(Task 8 消费)。`everos_pin` 是
    `cfg.pin_file` 原文两因子,只读不重算——那是 `everos_prod_instance.sh` 写的,
    但**读取时机**必须逐请求进行(见 `PinFileCache`),不能只在启动时读一次。

    未传 `pin_cache` 时(CLI/一次性脚本/既有测试的直接调用形态)直接读一次
    文件,行为与改动前完全一致;server.py 的每请求路径传入共享的
    `PinFileCache` 实例,靠 mtime/size 判断是否需要重读。"""
    pin_text = pin_cache.read() if pin_cache is not None else _read_pin_file(cfg.pin_file)
    return {**collect_static_config_fp(cfg), "everos_pin": pin_text}


# ======================================================================
# collect_lib_counts()
# ======================================================================

_ENTRY_COUNT_RE = re.compile(r"^entry_count:\s*(\d+)\s*$", re.MULTILINE)


def _read_frontmatter_entry_count(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise LibCountsError(f"{path}: 无 frontmatter(不以 --- 起始)")
    end = text.find("\n---", 3)
    if end == -1:
        raise LibCountsError(f"{path}: frontmatter 未闭合")
    frontmatter = text[3:end]
    m = _ENTRY_COUNT_RE.search(frontmatter)
    if not m:
        raise LibCountsError(f"{path}: frontmatter 缺 entry_count 字段")
    return int(m.group(1))


def collect_lib_counts(cfg: Config) -> dict:
    """`case_count` = 遍历 `.cases/agent_case-*.md` **求和** frontmatter
    `entry_count`(每日聚合文件,一文件多条,禁按文件数计);`skill_count` =
    `skills/*/SKILL.md` 计数。目录不可读/frontmatter 解析失败 ->
    `LibCountsError`(调用方让该次 lib_counts 缺席,不糊 0)。"""
    cases_dir = cfg.instance_dir / ".cases"
    if not cases_dir.is_dir():
        raise LibCountsError(f".cases 目录不可读: {cases_dir}")
    case_count = 0
    for p in sorted(cases_dir.glob("agent_case-*.md")):
        case_count += _read_frontmatter_entry_count(p)

    skills_dir = cfg.instance_dir / "skills"
    if not skills_dir.is_dir():
        raise LibCountsError(f"skills 目录不可读: {skills_dir}")
    skill_count = len(list(skills_dir.glob("*/SKILL.md")))

    return {"case_count": case_count, "skill_count": skill_count, "count_ts": time.time()}


# ======================================================================
# 磁盘卡向量缓存:键 = (passage_sha, model, artifact_fp) -> root/veccache/
# ======================================================================

def _cache_key_name(passage_sha: str, model: str, artifact_fp: str) -> str:
    raw = f"{passage_sha}|{model}|{artifact_fp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() + ".json"


def _ensure_cache_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True)
        os.chmod(path, _DIR_MODE)


def _cache_get(cache_dir: Path, passage_sha: str, model: str, artifact_fp: str):
    path = cache_dir / _cache_key_name(passage_sha, model, artifact_fp)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    vec = data.get("vector")
    return vec if isinstance(vec, list) else None


def _cache_put(cache_dir: Path, passage_sha: str, model: str, artifact_fp: str,
               vector: list[float]) -> None:
    path = cache_dir / _cache_key_name(passage_sha, model, artifact_fp)
    tmp = cache_dir / f".tmp.{os.getpid()}.{uuid.uuid4().hex}"
    tmp.write_text(json.dumps({"vector": vector}, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, _FILE_MODE)
    os.replace(tmp, path)


# ======================================================================
# ScoreWorker
# ======================================================================

class ScoreWorker:
    """独立线程消费 rid 队列 + 周期 reconciliation 扫描。

    打分调用全部发生在本类自己的后台线程里(同步 urllib 底座,不进任何事件
    循环)。`tokenizer`/`retry_backoff_base` 是测试注入口(生产不传,走各自
    默认值——真 rerank tokenizer / 0.5s 指数退避 base)。
    """

    def __init__(
        self,
        cfg: Config,
        ledger: Ledger,
        blobstore: BlobStore,
        queue_max: int = 256,
        *,
        tokenizer=None,
        retry_backoff_base: float = _DEFAULT_RETRY_BACKOFF_BASE,
        reconcile_interval: float = _DEFAULT_RECONCILE_INTERVAL,
        reconcile_batch_gap: float = _DEFAULT_RECONCILE_BATCH_GAP,
        fail_threshold: int = _DEFAULT_FAIL_THRESHOLD,
    ):
        self.cfg = cfg
        self.ledger = ledger
        self.blobstore = blobstore
        self._tokenizer = tokenizer
        self._retry_backoff_base = retry_backoff_base
        self._reconcile_interval = reconcile_interval
        self._reconcile_batch_gap = reconcile_batch_gap
        self._fail_threshold = fail_threshold

        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()

        self._veccache_dir = Path(cfg.ledger_dir) / "veccache"
        _ensure_cache_dir(self._veccache_dir)

        # 初始 pin bundle 采集失败 -> 构造即失败(fail-fast,与 collect_pins
        # "任一采集失败 raise" 同一纪律,不允许 worker 带着假 pins 起步)。
        self._marker: str | None = None
        self._pins: dict | None = None
        self._rebuild_pin_bundle()

        self._recon_stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="everos-score-worker", daemon=True)
        self._thread.start()
        self._recon_thread = threading.Thread(
            target=self._reconcile_loop, name="everos-score-reconcile", daemon=True
        )
        self._recon_thread.start()

    # ------------------------------------------------------------
    def enqueue(self, rid: str) -> bool:
        """非阻塞:队列满返回 False(accepted 已落账,reconciliation 兜底)。
        producer="realtime"。"""
        try:
            self._queue.put_nowait(_WorkItem(rid, "realtime"))
            return True
        except queue.Full:
            return False

    def enqueue_reconcile(self, rid: str) -> bool:
        """`reconcile()` 的唯一投递口(P1d):把 rid 塞进与 realtime 共用的
        同一条队列——这就是"reconciliation 与实时打分互斥、并发 1"的实现
        方式(单一消费线程,不是靠锁挡并发)。非阻塞,队列满返回 False(下一轮
        sweep 会重新发现这个孤儿,不是数据丢失)。"""
        try:
            self._queue.put_nowait(_WorkItem(rid, "reconciliation"))
            return True
        except queue.Full:
            return False

    def manual_rescore(self, rid: str) -> None:
        """人工补打入口,producer="manual"。同样经共享队列投递(不直接调
        `_score_once`),保持"打分调用只在 worker 唯一线程发生"这条不变量;
        阻塞等待该 rid 处理完成再返回(调用方期望的是同步语义——旧版直接
        同步调用,行为对调用方保持一致,只是执行体挪到了 worker 线程)。不做
        score_eligible 前置校验——人工介入的场景本就是"我知道我在干什么",
        交给 `_score_once`/writer 的健康谓词自然把关(缺 accepted 行会静默
        no-op,畸形结果会被 validator 打回 retryable_error)。"""
        done = threading.Event()
        self._queue.put(_WorkItem(rid, "manual", done))
        done.wait()

    def close(self, drain: bool = True, timeout: float = 10.0) -> None:
        self._recon_stop.set()
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            # 队列满塞不进哨兵——清一个位置腾出空间,daemon 线程反正会随进程退出。
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(_SENTINEL)
            except (queue.Empty, queue.Full):
                pass
        if drain:
            self._thread.join(timeout=timeout)
        self._recon_thread.join(timeout=timeout)

    # ------------------------------------------------------------
    def _run(self) -> None:
        """唯一调用 `_score_once` 的地方——realtime/reconciliation/manual 三类
        work item 全部经同一条队列串行处理,并发天然是 1。"""
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            try:
                self._score_once(item.rid, producer=item.producer)
            except Exception:  # noqa: BLE001 —— worker 线程绝不能被单个 rid 打死
                pass
            finally:
                if item.done_event is not None:
                    item.done_event.set()

    def _reconcile_loop(self) -> None:
        while not self._recon_stop.wait(self._reconcile_interval):
            try:
                reconcile(
                    self.cfg, self.ledger, self.enqueue_reconcile,
                    interval_between=self._reconcile_batch_gap,
                    fail_threshold=self._fail_threshold,
                )
            except Exception:  # noqa: BLE001 —— 周期扫描不能把线程带死
                pass

    # ------------------------------------------------------------
    def _rebuild_pin_bundle(self) -> None:
        marker, pins = _collect_pin_bundle(self.cfg)
        with self._lock:
            self._marker = marker
            self._pins = pins

    def _pins_snapshot(self) -> dict:
        with self._lock:
            return dict(self._pins) if self._pins is not None else {}

    def _find_accepted(self, rid: str) -> dict | None:
        rows, _ = iter_rows(self.ledger.root, "accepted")
        for row in rows:
            if row.get("kind") == "accepted" and row.get("rid") == rid:
                return row
        return None

    # ------------------------------------------------------------
    def _attempt_score(self, accepted: dict, candidates: list[dict]) -> tuple[dict, dict]:
        query = accepted.get("query")
        container = self.cfg.infinity_container

        marker_before = get_container_marker(container)
        with self._lock:
            expected_marker = self._marker
            pins_snapshot = dict(self._pins)

        if marker_before != expected_marker:
            self._rebuild_pin_bundle()
            raise MarkerDrift(
                f"容器 {container} marker 在读缓存前已漂移"
                f"(读取={marker_before!r} != 已知 pin bundle={expected_marker!r})"
            )

        artifact_fp = pins_snapshot["model_artifact_fp"]

        passages: dict[str, str] = {}
        for c in candidates:
            passages[c["passage_sha"]] = self.blobstore.get(c["passage_sha"])

        cached: dict[str, list] = {}
        miss: list[dict] = []
        for c in candidates:
            vec = _cache_get(self._veccache_dir, c["passage_sha"], self.cfg.embed_model, artifact_fp)
            if vec is not None:
                cached[c["passage_sha"]] = vec
            else:
                miss.append(c)

        batch_texts = [query] + [passages[c["passage_sha"]] for c in miss]
        vectors = probe_scores.embed(
            batch_texts, base_url=self.cfg.infinity_base, model=self.cfg.embed_model,
            post_json=http.post_json,
        )
        query_vec = vectors[0]
        for c, vec in zip(miss, vectors[1:]):
            cached[c["passage_sha"]] = vec
            _cache_put(self._veccache_dir, c["passage_sha"], self.cfg.embed_model, artifact_fp, vec)

        docs = [passages[c["passage_sha"]] for c in candidates]
        ce_scores = probe_scores.rerank(
            query, docs, base_url=self.cfg.infinity_base, model=self.cfg.rerank_model,
            tokenizer=self._tokenizer, post_json=http.post_json,
        )

        marker_after = get_container_marker(container)
        if marker_after != marker_before:
            self._rebuild_pin_bundle()
            raise MarkerDrift(
                f"容器 {container} marker 在打分调用期间漂移(前={marker_before!r} 后={marker_after!r})"
            )

        per_card = {}
        for c, ce in zip(candidates, ce_scores):
            cos = probe_scores.cosine(query_vec, cached[c["passage_sha"]])
            per_card[_card_key(c)] = {"cos": cos, "ce": ce}

        return per_card, pins_snapshot

    def _safe_lib_counts(self) -> tuple[dict | None, float | None, str | None]:
        try:
            lc = collect_lib_counts(self.cfg)
        except LibCountsError as e:
            return None, None, f"lib_counts unavailable: {e}"
        return {"case_count": lc["case_count"], "skill_count": lc["skill_count"]}, lc["count_ts"], None

    def _score_once(self, rid: str, producer: str) -> None:
        accepted = self._find_accepted(rid)
        if accepted is None:
            return  # 无 accepted 行可打(未知 rid/竞态)——非本函数职责兜底
        candidates = accepted.get("candidates") or []
        if not candidates:
            return

        last_exc: Exception | None = None
        for attempt_i in range(_MAX_TRANSIENT_RETRIES):
            try:
                per_card, pins = self._attempt_score(accepted, candidates)
            except Exception as e:  # noqa: BLE001 —— 任何打分期异常都按瞬时失败重试
                last_exc = e
                if attempt_i < _MAX_TRANSIENT_RETRIES - 1:
                    time.sleep(self._retry_backoff_base * (2 ** attempt_i))
                continue

            lib_counts, count_ts, lib_detail = self._safe_lib_counts()
            row = scored_row(
                rid, producer, "ok", per_card=per_card, pins=pins,
                score_error_detail=lib_detail, lib_counts=lib_counts, count_ts=count_ts,
            )
            self.ledger.submit_scored(row, accepted)
            return

        row = scored_row(
            rid, producer, "retryable_error", per_card={}, pins=self._pins_snapshot(),
            score_error_code="scoring_failed",
            score_error_detail=(str(last_exc)[:2000] if last_exc is not None else None),
        )
        self.ledger.submit_scored(row, accepted)


# ======================================================================
# reconcile()——纯函数,注入 enqueue primitive(P1d:不再注入打分 primitive
# 直接同步打分——reconcile() 只做 anti-join/阈值判断这类纯 ledger 读写,真正
# 需要补打的 rid 一律经 `enqueue_fn` 投递进 ScoreWorker 的单一队列/单一消费
# 线程,由此保证"reconciliation 与实时打分互斥、并发 1"。manual_rescore 是
# ScoreWorker 的一个薄方法,见上文,不必再重复一份自由函数)。
# ======================================================================

@dataclass(frozen=True)
class ReconcileReport:
    scanned: int
    orphans_found: int
    rescored: int
    permanent_failures: int


def reconcile(
    cfg: Config,
    ledger: Ledger,
    enqueue_fn: Callable[[str], bool],
    *,
    interval_between: float = _DEFAULT_RECONCILE_BATCH_GAP,
    fail_threshold: int = _DEFAULT_FAIL_THRESHOLD,
) -> ReconcileReport:
    """全扫 score_eligible accepted vs terminal_scored 的 anti-join,逐条限速
    **投递**(并发 1 由单一 worker 队列保证,不是本函数自己拿锁挡出来的;
    `interval_between` 限的是投递节奏,不是打分完成节奏——打分本身异步发生
    在 worker 线程,本函数不阻塞等结果)。同 rid 累计 retryable_error >=
    `fail_threshold` 次 -> 落 `permanent_failure`(此后不再进入待补打集合,
    这部分判定与写入仍是纯 ledger 读写,同步执行)。

    `enqueue_fn(rid) -> bool`——通常是 `ScoreWorker.enqueue_reconcile`(P1d,
    此前是直接同步执行打分的 `score_fn(rid, producer)`,已改为"只管投递",
    producer="reconciliation" 由 worker 内部固定,不再由调用方传入);测试可
    注入纯 fake 验证 anti-join/阈值逻辑,不必起真 worker。返回 False(队列满)
    时本函数不算作已补打,下一轮 sweep 会重新发现同一个孤儿。

    `cfg` 当前实现未直接使用(anti-join/阈值判断只需要 `ledger` 的三条流)——
    保留在签名里是为了跟简报冻结的 `reconcile(cfg, ledger, ...)` 接口对齐,
    也给后续"阈值/批间隔改由 cfg 驱动"留一个不必改签名的口子。
    """
    root = ledger.root
    ops_rows, _ = iter_rows(root, "ops")
    accepted_rows, _ = iter_rows(root, "accepted")
    scored_rows, _ = iter_rows(root, "scored")
    abort_rids = read_abort_rids(root)

    accepted_by_rid = {r["rid"]: r for r in accepted_rows if r.get("kind") == "accepted"}
    scored_by_rid: dict[str, list[dict]] = {}
    for r in scored_rows:
        scored_by_rid.setdefault(r.get("rid"), []).append(r)

    started_rids = sorted({r["rid"] for r in ops_rows if r.get("kind") == "started"})

    orphans: list[str] = []
    permanent = 0
    for rid in started_rids:
        eff = effective_status(ops_rows, accepted_rows, abort_rids, rid)
        accepted = accepted_by_rid.get(rid)
        if not score_eligible(eff, accepted):
            continue

        rows_for_rid = scored_by_rid.get(rid, [])
        if any(healthy(r, accepted) for r in rows_for_rid):
            continue  # 已有健康终态,不必补打

        folded = fold(rows_for_rid, accepted)
        if folded is not None and folded.get("status") == "permanent_failure":
            continue  # 已经终态失败,不再扫入待补打集合

        fail_count = sum(1 for r in rows_for_rid if r.get("status") == "retryable_error")
        if fail_count >= fail_threshold:
            permanent += 1
            row = scored_row(
                rid, "reconciliation", "permanent_failure", per_card={}, pins={},
                score_error_code="permanent_failure_threshold",
                score_error_detail=f"累计 retryable_error 达 {fail_count} 次(阈值 {fail_threshold})",
            )
            ledger.submit_scored(row, accepted)
            continue

        orphans.append(rid)

    rescored = 0
    for i, rid in enumerate(orphans):
        if enqueue_fn(rid):
            rescored += 1
        if i < len(orphans) - 1:
            time.sleep(interval_between)

    return ReconcileReport(
        scanned=len(started_rids), orphans_found=len(orphans),
        rescored=rescored, permanent_failures=permanent,
    )
