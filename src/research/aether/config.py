from __future__ import annotations

import os
from pathlib import Path


AETHER_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = AETHER_ROOT.parents[2]


def _default_legacy_root() -> Path:
    candidates = [
        WORKSPACE_ROOT / "teste",
        WORKSPACE_ROOT / "Testador de Estrategias" / "teste",
        WORKSPACE_ROOT.parent / "Testador de Estrategias" / "teste",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


_legacy_override = os.environ.get("AETHER_LEGACY_ROOT")
LEGACY_ROOT = Path(_legacy_override).resolve() if _legacy_override else _default_legacy_root().resolve()


def runtime_dir() -> Path:
    override = os.environ.get("AETHER_RUNTIME_DIR")
    if override:
        return Path(override).resolve()
    project_runtime = WORKSPACE_ROOT / "runtime"
    if (project_runtime / "aether.sqlite3").exists():
        return project_runtime.resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local).resolve() / "Aether" / "runtime"
    return AETHER_ROOT / ".runtime"


def db_path() -> Path:
    override = os.environ.get("AETHER_DB_PATH")
    if override:
        return Path(override).resolve()
    return runtime_dir() / "aether.sqlite3"


def checkpoint_dir() -> Path:
    return runtime_dir() / "checkpoints"


def artifact_dir() -> Path:
    return AETHER_ROOT / "artifacts"


def static_dir() -> Path:
    return AETHER_ROOT / "static"


DEFAULT_WORKERS = max(1, os.cpu_count() or 1)
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
