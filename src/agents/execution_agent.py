from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.base import AgentFinding, AgentSeverity
from src.agents.paper_state import load_paper_state


ERROR_KEYWORDS = ("Traceback", "Exception", "ERROR")
LOG_TAIL_BYTES = 1 * 1024 * 1024
DEFAULT_THREAD_LOG = Path("src/logs/thread_debug.log")
DEFAULT_BOT_RUNTIME_LOG = Path("src/logs/bot_runtime.log")
WATCHED_MODULES = (
    "src.main",
    "src.agent_runner_loop",
    "src.research.aether.server",
    "src.research.aether.auto_validator",
    "src.research.aether.critic_agent",
    "src.research.aether.hyper_agent",
    "src.research.aether.auto_deploy",
)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_tail(path: Path, size_bytes: int = LOG_TAIL_BYTES) -> str:
    try:
        with path.open("rb") as f:
            size = path.stat().st_size
            if size > size_bytes:
                f.seek(size - size_bytes)
            return f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _default_process_snapshot() -> list[dict[str, Any]]:
    try:
        if sys.platform == "win32":
            command = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine "
                "| ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                encoding="utf-8",
                errors="ignore",
                timeout=8,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                parsed = [parsed]
            return [
                {
                    "pid": int(item.get("ProcessId") or 0),
                    "parent_pid": int(item.get("ParentProcessId") or 0),
                    "executable": str(item.get("ExecutablePath") or ""),
                    "command": str(item.get("CommandLine") or ""),
                }
                for item in parsed
                if isinstance(item, dict)
            ]

        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return []
        snapshot: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            raw = line.strip()
            if not raw:
                continue
            pid_raw, _, rest = raw.partition(" ")
            ppid_raw, _, command = rest.strip().partition(" ")
            try:
                pid = int(pid_raw)
                parent_pid = int(ppid_raw)
            except ValueError:
                continue
            if "python" in command.lower():
                snapshot.append({"pid": pid, "parent_pid": parent_pid, "command": command})
        return snapshot
    except Exception:
        return []


class ExecutionAgent:
    name = "execution_agent"
    display_name = "Maquinista"

    def __init__(
        self,
        state_path: Path | str = "src/logs/paper_balance/state.json",
        thread_log_path: Path | str = DEFAULT_THREAD_LOG,
        runtime_log_path: Path | str = DEFAULT_BOT_RUNTIME_LOG,
        stale_minutes_warning: float = 10.0,
        stale_minutes_critical: float = 30.0,
        position_age_warning_hours: float = 24.0,
        position_age_critical_hours: float = 72.0,
        error_threshold_warning: int = 5,
        error_threshold_critical: int = 25,
        process_snapshot: list[dict[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.thread_log_path = Path(thread_log_path)
        self.runtime_log_path = Path(runtime_log_path)
        self.stale_minutes_warning = stale_minutes_warning
        self.stale_minutes_critical = stale_minutes_critical
        self.position_age_warning_hours = position_age_warning_hours
        self.position_age_critical_hours = position_age_critical_hours
        self.error_threshold_warning = error_threshold_warning
        self.error_threshold_critical = error_threshold_critical
        self.process_snapshot = process_snapshot
        self._now = now

    def _current_time(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def run(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        findings.extend(self._check_paper_state())
        findings.extend(self._check_runtime_log_freshness())
        findings.extend(self._check_thread_log_errors())
        findings.extend(self._check_duplicate_processes())

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Loop saudavel",
                    message="Threads ativas, posicoes dentro do tempo esperado e sem erros recentes.",
                )
            )
        return findings

    def _check_duplicate_processes(self) -> list[AgentFinding]:
        snapshot = self.process_snapshot if self.process_snapshot is not None else _default_process_snapshot()
        if not snapshot:
            return []

        by_module: dict[str, list[dict[str, Any]]] = {module: [] for module in WATCHED_MODULES}
        for item in snapshot:
            command = str(item.get("command") or item.get("CommandLine") or "")
            try:
                pid = int(item.get("pid") or item.get("ProcessId") or 0)
            except (TypeError, ValueError):
                pid = 0
            if not command or not pid:
                continue
            for module in WATCHED_MODULES:
                pattern = rf"-m\s+{re.escape(module)}(\s|$)"
                if re.search(pattern, command):
                    by_module[module].append(item)

        duplicates: dict[str, list[int]] = {}
        for module, items in by_module.items():
            pids = {
                int(item.get("pid") or item.get("ProcessId") or 0)
                for item in items
                if int(item.get("pid") or item.get("ProcessId") or 0)
            }
            parent_pids = {
                int(item.get("parent_pid") or item.get("ParentProcessId") or 0)
                for item in items
                if int(item.get("parent_pid") or item.get("ParentProcessId") or 0)
            }
            # No Windows, o python.exe da venv pode aparecer como processo pai
            # launcher e criar um python.exe filho com a mesma command line.
            leaf_pids = sorted(pids - parent_pids) or sorted(pids)
            if len(leaf_pids) > 1:
                duplicates[module] = leaf_pids
        if not duplicates:
            return []

        has_bot_duplicate = "src.main" in duplicates
        severity = AgentSeverity.CRITICAL if has_bot_duplicate else AgentSeverity.WARNING
        modules = ", ".join(f"{module}={sorted(set(pids))}" for module, pids in sorted(duplicates.items()))
        action = (
            "Reiniciar bot e stack com os scripts gerenciados (-Restart) para manter uma instancia por modulo."
            if has_bot_duplicate
            else "Reiniciar stack/loop com -Restart para remover processos duplicados."
        )
        return [
            AgentFinding(
                agent=self.name,
                display_name=self.display_name,
                severity=severity,
                title="Processos duplicados no runtime",
                message=f"Modulos duplicados detectados: {modules}.",
                action=action,
                metadata={"duplicates": {module: sorted(set(pids)) for module, pids in duplicates.items()}},
            )
        ]

    def _check_paper_state(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        if not self.state_path.exists():
            return findings
        state = load_paper_state(self.state_path)
        if not state:
            return findings

        now = self._current_time()
        position_warning = self.position_age_warning_hours * 3600
        position_critical = self.position_age_critical_hours * 3600

        for asset, raw in sorted(state.items()):
            position = raw.get("position")
            if not position:
                continue
            entry_time = _parse_iso(position.get("time"))
            if entry_time is None:
                continue
            age_seconds = (now - entry_time).total_seconds()
            if age_seconds <= 0:
                continue
            if age_seconds >= position_critical:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.CRITICAL,
                        title="Posicao aberta ha tempo demais",
                        message=f"{asset} aberta ha {age_seconds / 3600:.1f}h.",
                        asset=asset,
                        action="Inspecionar manualmente; saida de stop/trailing pode ter falhado.",
                        metadata={"asset": asset, "age_hours": age_seconds / 3600},
                    )
                )
            elif age_seconds >= position_warning:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Posicao aberta ha mais de 24h",
                        message=f"{asset} aberta ha {age_seconds / 3600:.1f}h.",
                        asset=asset,
                        action="Verificar se a estrategia previa essa duracao de trade.",
                        metadata={"asset": asset, "age_hours": age_seconds / 3600},
                    )
                )
        return findings

    def _check_runtime_log_freshness(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        if not self.runtime_log_path.exists() or not self.runtime_log_path.is_file():
            return findings

        try:
            mtime = datetime.fromtimestamp(
                self.runtime_log_path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            return findings

        age_minutes = (self._current_time() - mtime).total_seconds() / 60
        if age_minutes >= self.stale_minutes_critical:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Loop possivelmente travado",
                    message=f"{self.runtime_log_path.name} sem atualizacao ha {age_minutes:.1f} min.",
                    action="Verificar se o processo principal ainda esta rodando.",
                    metadata={"path": str(self.runtime_log_path), "age_minutes": age_minutes},
                )
            )
        elif age_minutes >= self.stale_minutes_warning:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Runtime log envelhecendo",
                    message=f"{self.runtime_log_path.name} sem atualizacao ha {age_minutes:.1f} min.",
                    action="Acompanhar; pode indicar lentidao no loop.",
                    metadata={"path": str(self.runtime_log_path), "age_minutes": age_minutes},
                )
            )
        return findings

    def _check_thread_log_errors(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        if not self.thread_log_path.exists() or not self.thread_log_path.is_file():
            return findings

        content = _read_tail(self.thread_log_path)
        if not content:
            return findings

        error_count = sum(content.count(keyword) for keyword in ERROR_KEYWORDS)
        if error_count >= self.error_threshold_critical:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Excecoes repetidas em threads",
                    message=f"{error_count} ocorrencias recentes em {self.thread_log_path.name}.",
                    action="Pausar exposicao e investigar a causa raiz das excecoes.",
                    metadata={"path": str(self.thread_log_path), "error_count": error_count},
                )
            )
        elif error_count >= self.error_threshold_warning:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Excecoes recentes em threads",
                    message=f"{error_count} ocorrencias recentes em {self.thread_log_path.name}.",
                    action="Inspecionar log antes de aumentar exposicao.",
                    metadata={"path": str(self.thread_log_path), "error_count": error_count},
                )
            )
        return findings
