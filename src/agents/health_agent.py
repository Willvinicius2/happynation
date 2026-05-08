from __future__ import annotations

import json
from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity


CRITICAL_STATE_FILES = (
    "src/logs/paper_balance/state.json",
    "src/logs/research/strategy_catalog.json",
    "src/logs/research/active_strategies.json",
    "src/database/trailing_db.json",
)

LOG_TAIL_BYTES = 2 * 1024 * 1024

LOG_PATTERNS = (
    "src/logs/*.log",
    "src/logs/**/*.log",
)

ERROR_KEYWORDS = ("Traceback", "ERROR", "Exception")


class HealthAgent:
    name = "health_agent"
    display_name = "Medico"

    def __init__(
        self,
        repo_root: Path | str = ".",
        log_size_warning_mb: float = 50.0,
        log_size_critical_mb: float = 200.0,
        error_count_warning: int = 25,
        state_files: tuple[str, ...] = CRITICAL_STATE_FILES,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.log_size_warning_mb = log_size_warning_mb
        self.log_size_critical_mb = log_size_critical_mb
        self.error_count_warning = error_count_warning
        self.state_files = state_files

    def run(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        findings.extend(self._check_state_files())
        findings.extend(self._check_log_sizes())
        findings.extend(self._check_log_errors())

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Saude OK",
                    message="Arquivos de estado validos e logs dentro dos limites.",
                )
            )
        return findings

    def _check_state_files(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        for relative in self.state_files:
            path = self.repo_root / relative
            if not path.exists():
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.CRITICAL,
                        title="Arquivo de estado corrompido",
                        message=f"{path} nao e JSON valido: {exc}",
                        action="Restaurar backup do arquivo antes de continuar operando.",
                        metadata={"path": str(path)},
                    )
                )
        return findings

    def _check_log_sizes(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        seen: set[Path] = set()
        for pattern in LOG_PATTERNS:
            for path in self.repo_root.glob(pattern):
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb >= self.log_size_critical_mb:
                    findings.append(
                        AgentFinding(
                            agent=self.name,
                            display_name=self.display_name,
                            severity=AgentSeverity.CRITICAL,
                            title="Log gigante",
                            message=f"{path} tem {size_mb:.1f} MB.",
                            action="Rotacionar o log imediatamente.",
                            metadata={"path": str(path), "size_mb": size_mb},
                        )
                    )
                elif size_mb >= self.log_size_warning_mb:
                    findings.append(
                        AgentFinding(
                            agent=self.name,
                            display_name=self.display_name,
                            severity=AgentSeverity.WARNING,
                            title="Log acima do limite",
                            message=f"{path} tem {size_mb:.1f} MB.",
                            action="Programar rotacao deste log.",
                            metadata={"path": str(path), "size_mb": size_mb},
                        )
                    )
        return findings

    def _check_log_errors(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        thread_log = self.repo_root / "src" / "logs" / "thread_debug.log"
        if not thread_log.exists() or not thread_log.is_file():
            return findings
        try:
            with thread_log.open("rb") as f:
                size = thread_log.stat().st_size
                if size > LOG_TAIL_BYTES:
                    f.seek(size - LOG_TAIL_BYTES)
                content = f.read().decode("utf-8", errors="ignore")
        except OSError:
            return findings
        error_count = sum(content.count(keyword) for keyword in ERROR_KEYWORDS)
        if error_count >= self.error_count_warning:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Erros repetidos em thread_debug.log",
                    message=f"{error_count} ocorrencias de erro detectadas.",
                    action="Inspecionar thread_debug.log antes de aumentar exposicao.",
                    metadata={"path": str(thread_log), "error_count": error_count},
                )
            )
        return findings
