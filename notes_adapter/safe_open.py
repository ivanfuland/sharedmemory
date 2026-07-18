# M0 威胁模型边界（Fix 4）：
#   - `root` 本身是被信任的入口——它以不带 O_NOFOLLOW 的方式打开，调用方必须
#     保证传入的 root 路径本身可信（不是攻击者可控的符号链接）。本模块只保护
#     root *之下* 的相对路径解析，不重新校验 root 自身。
#   - 不防御 root 内部跨挂载点（mount-point crossing）：openat2 的 RESOLVE
#     标志未设置 RESOLVE_NO_XDEV，所以如果 root 子树里挂了另一个文件系统，
#     解析可以穿过挂载边界。M0 只做"不逃出 root 目录树的符号链接/dotdot/
#     非常规文件"防护，不是完整沙箱。
import ctypes, os, struct

class UnsafePathError(RuntimeError): ...

_SYS_OPENAT2 = 437
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_RESOLVE = _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long   # Fix 2: 未声明 restype 默认 c_int，64 位返回值（fd）可能被截断

def _openat2(dirfd: int, rel: str) -> int:
    how = struct.pack("<QQQ", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0, _RESOLVE)   # 主路径同样防 FIFO（R9 nit3；FIFO 用例覆盖主/备两路）
    fd = _libc.syscall(_SYS_OPENAT2, dirfd, rel.encode(), how, len(how))
    if fd < 0:
        raise UnsafePathError(f"openat2 errno={ctypes.get_errno()} rel={rel}")
    return fd

def _walk_fallback(dirfd: int, rel: str) -> int:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafePathError(f"dotdot in {rel}")
    fd = os.dup(dirfd)
    try:
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_NONBLOCK if i == len(parts) - 1 else os.O_DIRECTORY)   # 末段 O_NONBLOCK 防 FIFO（R8 P1-8）
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError as e:
                raise UnsafePathError(f"{rel}: {e}") from e
            os.close(fd); fd = nfd
        return fd
    except Exception:
        os.close(fd); raise

def read_within(root: str, rel: str) -> bytes:
    if "\x00" in rel:
        # Fix 1: 嵌入 NUL 字节必须在入口统一拒绝。os.open()（fallback 路径）
        # 会对含 NUL 的路径抛未包装的 ValueError，绕过 UnsafePathError 契约；
        # 提前拦截可以让 openat2 主路径和 fallback 路径行为一致。
        raise UnsafePathError(f"embedded NUL byte in {rel}")
    if ".." in [q for q in rel.split("/") if q]:
        raise UnsafePathError(f"dotdot component in {rel}")   # 分量级判定，a..b 合法；双路径一致（R7 P2-13）
    dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            fd = _openat2(dirfd, rel)
        except UnsafePathError:
            fd = _walk_fallback(dirfd, rel)
        try:
            st = os.fstat(fd)
            import stat as _s
            if not _s.S_ISREG(st.st_mode):
                raise UnsafePathError(f"not regular: {rel}")
            out = b""
            while chunk := os.read(fd, 1 << 20):
                out += chunk
            return out
        finally:
            os.close(fd)
    finally:
        os.close(dirfd)
