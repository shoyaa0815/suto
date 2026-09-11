"""Single-host advisory locks. Never unlink a lock file while it may be held."""
import fcntl
import os
from pathlib import Path


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self, *, blocking: bool = False) -> bool:
        if self.fd is not None:
            return True
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"database is in use: {self.path}")
        return self

    def __exit__(self, *_):
        self.release()
