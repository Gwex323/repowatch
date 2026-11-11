"""OS-owned shared/exclusive locks; process death releases ownership, never unlink locks."""

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from repowatch.application.storage import ConflictError

_held = threading.local()


class FileLock:
    def __init__(self, path: Path, *, exclusive=False, timeout=5):
        self.path, self.exclusive, self.timeout = path, exclusive, timeout
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise PermissionError("锁文件不能是链接")
        self.stream = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_lock():
                return self
            if time.monotonic() >= deadline:
                self.close()
                raise ConflictError("服务或维护操作仍在使用数据，请停止写者或稍后重试")
            time.sleep(0.025)

    def _try_lock(self):
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            self.overlapped = Overlapped()
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            result = kernel.LockFileEx(
                msvcrt.get_osfhandle(self.stream.fileno()),
                1 | (2 if self.exclusive else 0),
                0,
                1,
                0,
                ctypes.byref(self.overlapped),
            )
            if not result and ctypes.get_last_error() not in {33, 158}:
                code = ctypes.get_last_error()
                self.close()
                raise OSError(code, "无法取得维护锁")
            return bool(result)
        import fcntl

        try:
            fcntl.flock(
                self.stream.fileno(),
                (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
            return True
        except BlockingIOError:
            return False

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


@contextmanager
def data_lock(settings, name="maintenance", *, exclusive=False, timeout=5):
    path = settings.data_dir / f".{name}.lock"
    key = os.path.normcase(str(path.resolve()))
    held = getattr(_held, "locks", None)
    if held is None:
        held = _held.locks = {}
    existing = held.get(key)
    if existing is not None:
        if exclusive and not existing.exclusive:
            raise ConflictError("不能在写者内部升级为独占维护锁")
        yield
        return
    with FileLock(path, exclusive=exclusive, timeout=timeout) as lock:
        held[key] = lock
        try:
            yield
        finally:
            held.pop(key)


def guarded(method):
    """Hold the barrier across multi-transaction work, including files/remote receipts."""
    from functools import wraps

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        settings = getattr(self, "settings", None)
        if settings is None:
            owner = getattr(self, "services", None)
            settings = owner.settings if owner is not None else self.store.settings
        with data_lock(settings):
            return method(self, *args, **kwargs)

    return wrapped
