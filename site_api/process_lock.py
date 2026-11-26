"""Portable non-blocking process locks for shared local resources."""
from __future__ import annotations

import errno
import os


class LockAlreadyHeldError(RuntimeError):
    """Raised when another process currently owns the requested lock."""


class ProcessLock:
    """An advisory OS lock released automatically when its process exits.

    The lock file is intentionally retained after release. Deleting a lock file
    can race with another process opening the same path and create two inodes
    with separate locks.
    """

    def __init__(self, path: str):
        self.path = os.fspath(path)
        self._handle = None
        self._platform = None

    def acquire(self):
        if self._handle is not None:
            return self

        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        handle = open(self.path, "a+b")

        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(bytes((0,)))
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise LockAlreadyHeldError(
                            "process lock is already held: %s" % self.path
                        ) from exc
                    raise
                self._platform = "nt"
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN):
                        raise LockAlreadyHeldError(
                            "process lock is already held: %s" % self.path
                        ) from exc
                    raise
                self._platform = "posix"
        except Exception:
            handle.close()
            raise

        self._handle = handle
        return self

    def release(self):
        handle = self._handle
        if handle is None:
            return

        self._handle = None
        try:
            if self._platform == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._platform == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._platform = None
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False
