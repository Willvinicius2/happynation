from pathlib import Path
import importlib


def test_stack_launcher_exposes_codex_cli_path_for_powershell_runtime():
    script = Path("scripts/run_aether_stack.ps1").read_text(encoding="utf-8")

    assert "AETHER_CODEX_CLI_PATH" in script
    assert "AppData\\Roaming\\npm\\codex.cmd" in script
    assert "Test-Path" in script


def test_background_launcher_restart_manages_deploy_watchdog():
    script = Path("scripts/run_aether_bg.ps1").read_text(encoding="utf-8")

    assert "$managedEntries = @($modules)" in script
    assert "scripts._watchdog_stack" in script
    assert "Get-ExistingModuleProcesses -Entries $managedEntries" in script
    assert "Aether/bot/watchdog" in script


def test_watchdog_uses_aether_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AETHER_DB_PATH", raising=False)
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))

    import scripts._watchdog_stack as watchdog

    watchdog = importlib.reload(watchdog)

    assert watchdog.DB == tmp_path.resolve() / "aether.sqlite3"


def test_watchdog_prefers_aether_db_path(monkeypatch, tmp_path):
    db = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("AETHER_DB_PATH", str(db))

    import scripts._watchdog_stack as watchdog

    watchdog = importlib.reload(watchdog)

    assert watchdog.DB == db.resolve()


def test_aether_config_prefers_project_runtime_db(monkeypatch, tmp_path):
    monkeypatch.delenv("AETHER_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("AETHER_DB_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    import src.research.aether.config as config

    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    project_runtime = tmp_path / "runtime"
    project_runtime.mkdir()
    (project_runtime / "aether.sqlite3").write_text("", encoding="utf-8")

    assert config.runtime_dir() == project_runtime.resolve()
    assert config.db_path() == project_runtime.resolve() / "aether.sqlite3"


def test_aether_config_sqlite_busy_timeout_is_configurable(monkeypatch):
    import src.research.aether.config as config

    monkeypatch.delenv("AETHER_SQLITE_BUSY_TIMEOUT_MS", raising=False)
    assert config.sqlite_busy_timeout_ms() == 90000

    monkeypatch.setenv("AETHER_SQLITE_BUSY_TIMEOUT_MS", "45000")
    assert config.sqlite_busy_timeout_ms() == 45000

    monkeypatch.setenv("AETHER_SQLITE_BUSY_TIMEOUT_MS", "bad")
    assert config.sqlite_busy_timeout_ms() == 90000


def test_aether_config_sqlite_busy_retry_is_short_and_bounded(monkeypatch):
    import src.research.aether.config as config

    monkeypatch.delenv("AETHER_SQLITE_BUSY_RETRY_SECONDS", raising=False)
    assert config.sqlite_busy_retry_seconds() == 5.0

    monkeypatch.setenv("AETHER_SQLITE_BUSY_RETRY_SECONDS", "0.2")
    assert config.sqlite_busy_retry_seconds() == 1.0

    monkeypatch.setenv("AETHER_SQLITE_BUSY_RETRY_SECONDS", "45")
    assert config.sqlite_busy_retry_seconds() == 30.0

    monkeypatch.setenv("AETHER_SQLITE_BUSY_RETRY_SECONDS", "bad")
    assert config.sqlite_busy_retry_seconds() == 5.0


def test_aether_db_init_enables_wal(tmp_path):
    import sqlite3

    from src.research.aether.db import init_db

    db = init_db(tmp_path / "aether.sqlite3")
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()
