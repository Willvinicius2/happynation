"""Lock inter-processo cross-platform sem dependencia externa.

Usa msvcrt.locking() no Windows e fcntl.flock() em POSIX.
Bloqueante por padrao, com timeout configuravel.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(lock_path: Path | str, timeout: float = 10.0, poll: float = 0.05):
    """Adquire lock exclusivo sobre um arquivo. Cria se nao existir.

    Bloqueia ate `timeout` segundos. Levanta TimeoutError se nao conseguir.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    deadline = time.time() + timeout
    acquired = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    # Lock 1 byte; LK_NBLCK = nao-bloqueante
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                if time.time() >= deadline:
                    raise TimeoutError(f"Timeout adquirindo lock {lock_path}")
                time.sleep(poll)
        yield
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
