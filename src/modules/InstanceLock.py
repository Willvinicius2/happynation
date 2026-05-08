"""Lockfile inter-processo: previne duas instancias do bot na mesma pasta.

Cross-platform (Windows + POSIX) sem dependencia externa. Usa PID + check
de processo vivo via os.kill(pid, 0) (POSIX) ou via psutil/tasklist (Windows
fallback ingenuo: assume vivo se PID existir no arquivo).
"""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path

LOCK_PATH = Path(os.getenv("BOT_LOCK_FILE", "src/logs/.bot.lock"))
_ACQUIRED = False


def _is_pid_alive(pid: int) -> bool:
    """Retorna True se o processo com esse PID estiver rodando."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True  # Conservador: assume vivo
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Existe mas nao temos permissao
        except Exception:
            return False


def acquire_or_die() -> None:
    """Tenta adquirir o lock. Se outra instancia esta viva, sai com mensagem.

    A criacao do arquivo usa O_EXCL para evitar corrida quando dois bots sobem
    no mesmo segundo: so um processo consegue criar o lock de forma atomica.
    """
    global _ACQUIRED
    if _ACQUIRED:
        return

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    for _attempt in range(3):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            _ACQUIRED = True
            atexit.register(_release)
            return
        except FileExistsError:
            pass

        try:
            existing = int(LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
        except (ValueError, OSError):
            existing = 0
        if existing == os.getpid():
            _ACQUIRED = True
            atexit.register(_release)
            return
        if existing and _is_pid_alive(existing) and existing != os.getpid():
            sys.stderr.write(
                f"\n[BOT] Outra instancia ja esta rodando (PID {existing}).\n"
                f"      Lock: {LOCK_PATH.resolve()}\n"
                f"      Se voce tem certeza que nao tem outro bot rodando, apague o lock manualmente.\n\n"
            )
            sys.exit(1)
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    sys.stderr.write(
        f"\n[BOT] Nao foi possivel adquirir lock atomico.\n"
        f"      Lock: {LOCK_PATH.resolve()}\n\n"
    )
    sys.exit(1)


def _release() -> None:
    global _ACQUIRED
    try:
        if LOCK_PATH.exists():
            try:
                pid_in_file = int(LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                pid_in_file = 0
            if pid_in_file == os.getpid():
                LOCK_PATH.unlink(missing_ok=True)
                _ACQUIRED = False
    except Exception:
        pass
