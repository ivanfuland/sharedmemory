import os, threading, time, pytest
from notes_adapter import safe_open
from notes_adapter.safe_open import read_within, UnsafePathError


# Step 4 (brief): 强制 fallback 全套重跑。
# 设 SAFE_OPEN_FORCE_FALLBACK=1 时，monkeypatch safe_open._openat2 让它总是抛
# UnsafePathError（模拟 openat2 ENOSYS/不可用），逼 read_within 的每一次调用都走
# _walk_fallback。read_within 内部通过模块全局名 `_openat2` 调用，所以 patch
# 模块属性对所有测试用例（包括下面新增的 FIFO / 中间目录换链竞态）同样生效。
FORCE_FALLBACK = os.environ.get("SAFE_OPEN_FORCE_FALLBACK") == "1"


@pytest.fixture(autouse=True)
def _maybe_force_fallback(monkeypatch):
    if FORCE_FALLBACK:
        def _always_unavailable(dirfd, rel):
            raise UnsafePathError(f"forced-fallback (simulated ENOSYS) rel={rel}")
        monkeypatch.setattr(safe_open, "_openat2", _always_unavailable)
    yield


def _mk(tmp_path):
    (tmp_path / "d").mkdir(); (tmp_path / "d" / "a.md").write_text("ok")
    (tmp_path.parent / "secret.md").write_text("SECRET")
    return tmp_path


def test_regular_ok(tmp_path):
    root = _mk(tmp_path); assert read_within(str(root), "d/a.md") == b"ok"


def test_symlink_file_rejected(tmp_path):
    root = _mk(tmp_path); os.symlink(root.parent / "secret.md", root / "d" / "s.md")
    with pytest.raises(UnsafePathError): read_within(str(root), "d/s.md")


def test_symlink_dir_rejected(tmp_path):
    root = _mk(tmp_path); os.symlink(root.parent, root / "out")
    with pytest.raises(UnsafePathError): read_within(str(root), "out/secret.md")


def test_dotdot_rejected(tmp_path):
    root = _mk(tmp_path)
    with pytest.raises(UnsafePathError): read_within(str(root), "../secret.md")


def test_dotdot_lookalike_component_allowed(tmp_path):
    # a..b 是合法文件名分量，不含独立的 ".." 分量 —— 不应被 dotdot 守卫误伤
    # （R7 P2-13：分量级判定，"a..b" != ".."）。
    root = _mk(tmp_path)
    (root / "d" / "a..b").write_text("weird-but-legal")
    assert read_within(str(root), "d/a..b") == b"weird-but-legal"


def test_fifo_rejected_no_hang(tmp_path):
    # FIFO 必须被拒绝而不是挂起等待 writer。open() 本身靠 O_NONBLOCK 立即返回
    # （不等 writer 连上），真正的拒绝来自 read_within 里的 S_ISREG 检查——
    # fstat 到 FIFO 就在读取任何字节之前抛 UnsafePathError。用一个有超时的
    # watchdog 线程兜底：如果实现回归成会阻塞的版本，测试应该快速失败而不是
    # 挂起整个 CI。
    root = _mk(tmp_path)
    fifo_path = root / "d" / "fifo"
    os.mkfifo(fifo_path)

    result = {}

    def _attempt():
        try:
            result["value"] = read_within(str(root), "d/fifo")
        except BaseException as e:  # noqa: BLE001 - capture any exception for the main thread to inspect
            result["error"] = e

    t = threading.Thread(target=_attempt, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "read_within blocked on FIFO instead of rejecting it"
    assert "value" not in result, "read_within must never return FIFO bytes"
    assert isinstance(result.get("error"), UnsafePathError), (
        f"expected UnsafePathError, got {result.get('error')!r}"
    )


def test_concurrent_swap_never_leaks(tmp_path):
    root = _mk(tmp_path); target = root / "d" / "swap.md"; stop = threading.Event()
    def flip():
        while not stop.is_set():
            try:
                target.unlink(missing_ok=True); target.write_text("ok")
                target.unlink(missing_ok=True); os.symlink(root.parent / "secret.md", target)
            except OSError: pass
    t = threading.Thread(target=flip); t.start()
    try:
        for _ in range(300):
            try: assert b"SECRET" not in read_within(str(root), "d/swap.md")
            except (UnsafePathError, FileNotFoundError): pass
    finally: stop.set(); t.join()


def test_middle_dir_swap_race_never_leaks(tmp_path):
    # Step 4 (brief) 要求的中间目录换链竞态测试：把路径中间那一段目录 "d/" 本身
    # 在"真目录"和"指向 root.parent 的 symlink"之间反复切换，同时主线程不断
    # 用同一个相对路径 "d/swap.md" 读取。任何时刻 TOCTOU 让某个分量解析穿越
    # 到 root 之外，都必须被 openat2 的 RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS
    # （或 fallback 的逐分量 O_NOFOLLOW + dir_fd）挡住。为了让"泄漏"真正可
    # 观测（而不只是结构上被拒绝但恰好目标不存在），root.parent 下预先放一个
    # 同名的 swap.md，内容是 SECRET —— 如果 "d" 的 symlink 解析在某个竞态
    # 窗口里没被挡住，read_within(root, "d/swap.md") 就会真的读到它，断言会
    # 抓到。
    root = _mk(tmp_path)
    d_path = root / "d"
    (d_path / "swap.md").write_text("safe-content")
    (root.parent / "swap.md").write_text("SECRET-via-dir-swap")
    stop = threading.Event()

    def flip_dir():
        while not stop.is_set():
            try:
                # d/ -> 真目录，内含无害的 swap.md
                if d_path.is_symlink():
                    d_path.unlink()
                if not d_path.exists():
                    d_path.mkdir()
                    (d_path / "swap.md").write_text("safe-content")
            except OSError:
                pass
            try:
                # d/ -> 指向 root.parent 的 symlink。root.parent/swap.md 此时
                # "看起来"就是 d/swap.md，内容是 SECRET-via-dir-swap —— 这是
                # 本测试真正要挡住的泄漏路径。
                import shutil
                if d_path.is_dir() and not d_path.is_symlink():
                    shutil.rmtree(d_path)
                if not d_path.exists():
                    os.symlink(root.parent, d_path)
            except OSError:
                pass
        # 收尾恢复成安全状态，避免影响其他测试/清理
        try:
            if d_path.is_symlink():
                d_path.unlink()
            if not d_path.exists():
                d_path.mkdir()
        except OSError:
            pass

    t = threading.Thread(target=flip_dir)
    t.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                data = read_within(str(root), "d/swap.md")
                assert b"SECRET" not in data, "middle-dir symlink swap leaked out-of-root content"
            except (UnsafePathError, FileNotFoundError, NotADirectoryError):
                pass
    finally:
        stop.set(); t.join()
