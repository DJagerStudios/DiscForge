from __future__ import annotations

import os
import shutil
import sys
import time

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None  # type: ignore[assignment]


class DcError(Exception):
    """Base class for every error meant to be shown to the user."""


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def cache_dir() -> str:
    """Where unpacked/converted games live: XDG cache on Linux/BSD, ~/Library/Caches on macOS,
    %LOCALAPPDATA% on Windows."""
    if os.environ.get("XDG_CACHE_HOME"):
        base = os.environ["XDG_CACHE_HOME"]
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.path.expanduser("~/.cache")
    d = os.path.join(base, "discforge")
    os.makedirs(d, exist_ok=True)
    return d


class DirLock:
    """Marks a cache folder as in use for as long as a job needs it (shared between readers; cleaners
    only delete folders nobody holds).  flock on Unix, a byte-range lock on Windows."""

    def __init__(self, folder: str):
        self.fd = None
        try:
            self.fd = os.open(os.path.join(folder, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            if self.fd is not None:
                os.close(self.fd)
            self.fd = None

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def in_use(folder: str) -> bool:
    """True while some job (in any DiscForge process) holds the folder."""
    path = os.path.join(folder, ".lock")
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            return True
        return False
    finally:
        os.close(fd)


def remove_dir(path: str) -> bool:
    """Delete a cache folder unless a job is using it."""
    if in_use(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def clean_cache(max_age_hours: float = 12) -> None:
    """Remove leftovers (staging folders, extracted archives, converted games) of old runs."""
    d = cache_dir()
    now = time.time()
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if name.startswith(("stage-", "extract-", "burn-", "test-", "job-", "conv-")):
            try:
                if now - os.path.getmtime(p) > max_age_hours * 3600:
                    if os.path.isdir(p):
                        remove_dir(p)
                    else:
                        os.remove(p)
            except OSError:
                pass


def mb(sectors: int, size: int = 2048) -> float:
    return sectors * size / (1024 * 1024)


def fmt_mb(sectors: int, size: int = 2048) -> str:
    return f"{mb(sectors, size):.1f} MB"
