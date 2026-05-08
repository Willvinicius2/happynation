from __future__ import annotations

import re
from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity


SUSPICIOUS_PATTERNS = (
    re.compile(r"BINANCE_(API|SECRET)_KEY\s*=\s*['\"]?[A-Za-z0-9]{30,}", re.IGNORECASE),
    re.compile(r"sk_live_[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{9,10}:[A-Za-z0-9_-]{35,}\b"),
)

SCAN_GLOBS = (
    "*.py",
    "*.json",
    "*.yml",
    "*.yaml",
    "*.toml",
    "*.md",
)

SCAN_EXCLUDE_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
}

SCAN_EXCLUDE_RELATIVE = (
    Path("src/logs"),
    Path("docs"),
    Path("tests"),
)


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


class SecurityAgent:
    name = "security_agent"
    display_name = "Cofre"

    def __init__(
        self,
        repo_root: Path | str = ".",
        env_path: Path | str = ".env",
        gitignore_path: Path | str = ".gitignore",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.env_path = Path(env_path)
        self.gitignore_path = Path(gitignore_path)

    def run(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        findings.extend(self._check_gitignore())
        findings.extend(self._check_env_confirmation())
        findings.extend(self._scan_for_leaks())

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Cofre sem alertas",
                    message=".env protegido e nenhum segredo detectado em arquivos versionaveis.",
                )
            )
        return findings

    def _check_gitignore(self) -> list[AgentFinding]:
        if not self.gitignore_path.exists():
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title=".gitignore ausente",
                    message=f"Nao encontrei {self.gitignore_path}.",
                    action="Criar .gitignore protegendo .env e chaves antes de commitar.",
                )
            ]

        content = self.gitignore_path.read_text(encoding="utf-8", errors="ignore")
        ignored_lines = {line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")}
        if not any(entry == ".env" or entry == "*.env" for entry in ignored_lines):
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title=".env nao esta no .gitignore",
                    message=".env pode ser commitado por engano e expor chaves.",
                    action="Adicionar a linha '.env' no .gitignore.",
                )
            ]
        return []

    def _check_env_confirmation(self) -> list[AgentFinding]:
        env = _parse_env(self.env_path)
        if not env:
            return []
        paper = env.get("PAPER_TRADING")
        confirm = env.get("REAL_TRADING_CONFIRM")
        findings: list[AgentFinding] = []
        if paper is not None and not _is_truthy(paper) and not _is_truthy(confirm):
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Modo real sem confirmacao dupla",
                    message="PAPER_TRADING=false sem REAL_TRADING_CONFIRM=true.",
                    action="Exigir confirmacao dupla antes de operar com dinheiro real.",
                )
            )
        api_key = env.get("BINANCE_API_KEY", "")
        if api_key and len(api_key) >= 20 and not _is_truthy(paper):
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="API key em modo real",
                    message="API key configurada com PAPER_TRADING=false; verifique restricao de IP.",
                    action="Confirmar que a API key Binance tem restricao de IP e nao tem permissao de saque.",
                )
            )
        return findings

    def _scan_for_leaks(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        seen_files: set[Path] = set()
        repo_resolved = self.repo_root.resolve()
        excluded_resolved = [
            (repo_resolved / rel).resolve() for rel in SCAN_EXCLUDE_RELATIVE
        ]
        for pattern in SCAN_GLOBS:
            for path in self.repo_root.rglob(pattern):
                if not path.is_file():
                    continue
                if any(part in SCAN_EXCLUDE_DIR_NAMES for part in path.parts):
                    continue
                resolved = path.resolve()
                if any(
                    excl == resolved or excl in resolved.parents
                    for excl in excluded_resolved
                ):
                    continue
                if path in seen_files:
                    continue
                seen_files.add(path)
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for regex in SUSPICIOUS_PATTERNS:
                    match = regex.search(text)
                    if not match:
                        continue
                    findings.append(
                        AgentFinding(
                            agent=self.name,
                            display_name=self.display_name,
                            severity=AgentSeverity.CRITICAL,
                            title="Possivel chave em arquivo versionavel",
                            message=f"{path} tem padrao suspeito de chave.",
                            action="Remover a chave do arquivo e rotacionar imediatamente.",
                            metadata={"path": str(path), "pattern": regex.pattern},
                        )
                    )
                    break
        return findings
