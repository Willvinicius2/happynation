from __future__ import annotations

from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity


REQUIRED_KEYS = (
    "BINANCE_API_KEY",
    "BINANCE_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "PAPER_TRADING",
)

REAL_TRADING_RISK_KEYS = (
    "MAX_DAILY_LOSS_PCT",
    "MAX_ASSET_DRAWDOWN_PCT",
    "MAX_CONSECUTIVE_LOSSES",
)

PLACEHOLDER_HINTS = (
    "cole sua",
    "your_api_key",
    "your_secret",
    "your_token",
    "changeme",
    "xxxx",
    "example",
    "placeholder",
    "seu_token",
    "sua_api_key",
)

CRITICAL_REQUIRED_KEYS = ("PAPER_TRADING",)


def _parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            parsed[key] = value
    return parsed


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class ConfigAgent:
    name = "config_agent"
    display_name = "Vigia"

    def __init__(self, env_path: Path | str = ".env") -> None:
        self.env_path = Path(env_path)

    def run(self) -> list[AgentFinding]:
        if not self.env_path.exists():
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.CRITICAL,
                    title="Arquivo .env nao encontrado",
                    message=f"Nao encontrei {self.env_path}.",
                    action="Criar .env baseado no exemplo do projeto antes de rodar o bot.",
                )
            ]

        env = _parse_env(self.env_path)
        findings: list[AgentFinding] = []

        for key in REQUIRED_KEYS:
            value = env.get(key)
            if not value:
                severity = (
                    AgentSeverity.CRITICAL
                    if key in CRITICAL_REQUIRED_KEYS
                    else AgentSeverity.WARNING
                )
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=severity,
                        title="Variavel obrigatoria ausente",
                        message=f"{key} nao esta definida no .env.",
                        action=f"Definir {key} antes de iniciar o bot.",
                        metadata={"env_key": key},
                    )
                )
                continue
            lowered = value.lower()
            if any(hint in lowered for hint in PLACEHOLDER_HINTS):
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Variavel com placeholder",
                        message=f"{key} parece estar com valor de exemplo.",
                        action=f"Substituir {key} pelo valor real.",
                        metadata={"env_key": key},
                    )
                )

        paper_trading = env.get("PAPER_TRADING")
        real_confirm = env.get("REAL_TRADING_CONFIRM")
        if paper_trading is not None and not _is_truthy(paper_trading):
            if not _is_truthy(real_confirm):
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.CRITICAL,
                        title="Modo real sem confirmacao",
                        message="PAPER_TRADING=false sem REAL_TRADING_CONFIRM=true.",
                        action="Definir REAL_TRADING_CONFIRM=true so quando estiver realmente pronto para dinheiro real.",
                        metadata={
                            "paper_trading": paper_trading,
                            "real_trading_confirm": real_confirm,
                        },
                    )
                )
            for key in REAL_TRADING_RISK_KEYS:
                if not env.get(key):
                    findings.append(
                        AgentFinding(
                            agent=self.name,
                            display_name=self.display_name,
                            severity=AgentSeverity.CRITICAL,
                            title="Limite de risco ausente em modo real",
                            message=f"{key} nao definido com PAPER_TRADING=false.",
                            action=f"Definir {key} antes de operar em modo real.",
                            metadata={"env_key": key},
                        )
                    )

        for key in REAL_TRADING_RISK_KEYS:
            value = env.get(key)
            if value is None:
                continue
            try:
                parsed_value = float(value)
            except ValueError:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Limite de risco invalido",
                        message=f"{key}={value!r} nao e um numero.",
                        action=f"Corrigir {key} no .env.",
                        metadata={"env_key": key, "raw_value": value},
                    )
                )
                continue
            if parsed_value <= 0:
                findings.append(
                    AgentFinding(
                        agent=self.name,
                        display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Limite de risco zerado",
                        message=f"{key}={parsed_value} desabilita a protecao.",
                        action=f"Configurar {key} com valor maior que 0.",
                        metadata={"env_key": key, "value": parsed_value},
                    )
                )

        if not findings:
            findings.append(
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Configuracao valida",
                    message="Variaveis obrigatorias presentes e modo coerente.",
                )
            )
        return findings
