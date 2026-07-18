import ctypes, os, struct

class UnsafePathError(RuntimeError): ...

_SYS_OPENAT2 = 437
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_RESOLVE = _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
_libc = ctypes.CDLL(None, use_errno=True)

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
