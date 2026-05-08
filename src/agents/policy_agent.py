from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.agents.base import AgentFinding, AgentSeverity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class PolicyAgent:
    """Comandante: traduz findings dos outros agentes em controles fisicos.

    Refinos vs versao ingenua (1 critical -> kill global imediato):
      - Anti-flapping: so dispara apos `MIN_STREAK` ciclos consecutivos com o
        mesmo finding (key=agent+title+asset). Reduz falsos positivos.
      - Granularidade: criticals com `.asset` definido viram kill por asset
        (`src/logs/kill_switch/<ASSET>.json`, mode="graceful" por padrao --
        bloqueia novas operacoes mas mantem posicao). Critical sem asset OU
        com >= GLOBAL_TRIGGER assets simultaneos vira kill GLOBAL (liquida
        MARKET).
      - Caminho de volta: kills tem `expires_at` (default 1h) e auto-cleanup
        no proximo ciclo se streak cair a zero.
    """

    name = "policy_agent"
    display_name = "Comandante"

    def __init__(
        self,
        kill_switch_path: str | Path = "KILL_SWITCH",
        kill_switch_assets_dir: str | Path = "src/logs/kill_switch",
        decisions_path: str | Path = "src/logs/agents/policy_decisions.json",
        streaks_path: str | Path = "src/logs/agents/policy_streaks.json",
    ) -> None:
        self.kill_switch_path = Path(kill_switch_path)
        self.kill_switch_assets_dir = Path(kill_switch_assets_dir)
        self.decisions_path = Path(decisions_path)
        self.streaks_path = Path(streaks_path)
        self.min_streak = max(1, int(os.environ.get("POLICY_MIN_STREAK", "2")))
        self.global_trigger = max(2, int(os.environ.get("POLICY_GLOBAL_TRIGGER_ASSETS", "3")))
        self.kill_ttl_minutes = max(5, int(os.environ.get("POLICY_KILL_TTL_MINUTES", "60")))

    # ------------------------------------------------------------------
    # Streak persistence
    # ------------------------------------------------------------------
    def _load_streaks(self) -> dict:
        if not self.streaks_path.exists():
            return {}
        try:
            return json.loads(self.streaks_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_streaks(self, streaks: dict) -> None:
        try:
            self.streaks_path.parent.mkdir(parents=True, exist_ok=True)
            self.streaks_path.write_text(json.dumps(streaks, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _key(f: AgentFinding) -> str:
        asset = (f.asset or "GLOBAL").upper()
        return f"{f.agent}|{asset}|{f.title}"

    # ------------------------------------------------------------------
    # Kill writers
    # ------------------------------------------------------------------
    def _write_asset_kill(self, asset: str, drivers: list[AgentFinding], mode: str = "graceful") -> None:
        self.kill_switch_assets_dir.mkdir(parents=True, exist_ok=True)
        path = self.kill_switch_assets_dir / f"{asset.upper()}.json"
        payload = {
            "asset": asset.upper(),
            "mode": mode,
            "reason": " | ".join(f"[{d.display_name or d.agent}] {d.title}: {d.message}" for d in drivers)[:1500],
            "drivers": [
                {"agent": d.agent, "title": d.title, "severity": d.severity.value if hasattr(d.severity, "value") else str(d.severity)}
                for d in drivers
            ],
            "created_at": _iso(_now()),
            "expires_at": _iso(_now() + timedelta(minutes=self.kill_ttl_minutes)),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_global_kill(self, drivers: list[AgentFinding]) -> None:
        reason = " | ".join(f"[{d.display_name or d.agent}] {d.title}: {d.message}" for d in drivers)[:2000]
        self.kill_switch_path.write_text(reason, encoding="utf-8")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def run(self) -> list[AgentFinding]:
        return []

    def run_with_context(self, previous_findings: list[AgentFinding]) -> list[AgentFinding]:
        out: list[AgentFinding] = []
        critical_seen = [f for f in previous_findings if f.severity == AgentSeverity.CRITICAL]
        critical_by_key: dict[str, AgentFinding] = {}
        for finding in critical_seen:
            critical_by_key.setdefault(self._key(finding), finding)
        critical = list(critical_by_key.values())

        # 1) Atualiza streaks: incrementa nos vistos agora, zera nos ausentes.
        prev_streaks = self._load_streaks()
        seen_keys = {self._key(f) for f in critical}
        new_streaks: dict[str, dict] = {}
        for key in seen_keys:
            entry = prev_streaks.get(key, {})
            new_streaks[key] = {
                "count": int(entry.get("count", 0)) + 1,
                "first_seen": entry.get("first_seen") or _iso(_now()),
                "last_seen": _iso(_now()),
            }
        # Manter findings que cairam fora visiveis com count=0 por mais 1 ciclo? -> nao, drop.
        self._save_streaks(new_streaks)

        # 2) Filtra findings que ja confirmaram min_streak (anti-flapping).
        confirmed = [f for f in critical if new_streaks.get(self._key(f), {}).get("count", 0) >= self.min_streak]

        # 3) Agrupa por asset; sem asset -> "GLOBAL".
        by_asset: dict[str, list[AgentFinding]] = {}
        for f in confirmed:
            asset = (f.asset or "GLOBAL").upper()
            by_asset.setdefault(asset, []).append(f)

        global_drivers = by_asset.pop("GLOBAL", [])
        asset_buckets = by_asset  # {ASSET: [findings...]}

        # 4) Decide acao:
        #    - Se ha drivers GLOBAIS OU >= global_trigger assets em critical: kill GLOBAL.
        #    - Caso contrario: kill por asset (graceful).
        if global_drivers or len(asset_buckets) >= self.global_trigger:
            drivers = global_drivers + [f for fs in asset_buckets.values() for f in fs]
            try:
                self._write_global_kill(drivers)
                out.append(
                    AgentFinding(
                        agent=self.name, display_name=self.display_name,
                        severity=AgentSeverity.CRITICAL,
                        title="KILL SWITCH GLOBAL acionado",
                        message=(
                            f"{len(drivers)} driver(s) critico(s) confirmado(s) "
                            f"({len(global_drivers)} system-wide + {len(asset_buckets)} assets); "
                            f"liquidacao MARKET autorizada."
                        ),
                        action=f"Resolva os criticos e remova o arquivo {self.kill_switch_path} para retomar.",
                        metadata={
                            "kind": "global",
                            "drivers": [{"agent": d.agent, "title": d.title, "asset": d.asset} for d in drivers],
                        },
                    )
                )
            except Exception as exc:
                out.append(
                    AgentFinding(
                        agent=self.name, display_name=self.display_name,
                        severity=AgentSeverity.WARNING,
                        title="Falha ao acionar KILL GLOBAL",
                        message=f"Tentativa abortada: {exc}",
                        action="Verifique permissoes da pasta do projeto.",
                    )
                )
        else:
            # Per-asset graceful kills
            for asset, drivers in asset_buckets.items():
                try:
                    self._write_asset_kill(asset, drivers, mode="graceful")
                    out.append(
                        AgentFinding(
                            agent=self.name, display_name=self.display_name,
                            severity=AgentSeverity.WARNING,
                            title=f"KILL por asset: {asset}",
                            message=(
                                f"{len(drivers)} critico(s) confirmado(s) em {asset}; "
                                f"novas operacoes bloqueadas por ate {self.kill_ttl_minutes}min "
                                f"(modo graceful: posicao nao e liquidada)."
                            ),
                            asset=asset,
                            action=f"Resolva e/ou remova src/logs/kill_switch/{asset}.json.",
                            metadata={
                                "kind": "asset",
                                "mode": "graceful",
                                "drivers": [{"agent": d.agent, "title": d.title} for d in drivers],
                            },
                        )
                    )
                except Exception as exc:
                    out.append(
                        AgentFinding(
                            agent=self.name, display_name=self.display_name,
                            severity=AgentSeverity.WARNING,
                            title=f"Falha ao acionar KILL {asset}",
                            message=f"Tentativa abortada: {exc}",
                            asset=asset,
                            action="Verifique permissoes da pasta do projeto.",
                        )
                    )

        # 5) Auto-cleanup: kills cujo asset nao esta mais em critical sao
        #    deixados pra expirar via TTL no PaperBalanceLogger -- nao removemos
        #    aqui, evita corrida com bot ainda usando o kill no ciclo atual.

        # 6) Aviso quando KILL_SWITCH global persiste sem mais criticals.
        if self.kill_switch_path.exists() and not (global_drivers or len(asset_buckets) >= self.global_trigger):
            out.append(
                AgentFinding(
                    agent=self.name, display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Sistema aguardando desbloqueio",
                    message="Os agentes nao detectam mais criticos confirmados, mas o KILL_SWITCH global ainda esta presente.",
                    action=f"Exclua {self.kill_switch_path} pra retomar operacoes.",
                )
            )

        # 7) Persiste decisoes pra auditoria
        try:
            self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
            self.decisions_path.write_text(
                json.dumps(
                    {
                        "ts": _iso(_now()),
                        "min_streak": self.min_streak,
                        "global_trigger_assets": self.global_trigger,
                        "kill_ttl_minutes": self.kill_ttl_minutes,
                        "criticals_seen": len(critical_seen),
                        "criticals_unique": len(critical),
                        "criticals_confirmed": len(confirmed),
                        "global_kill_drivers": len(global_drivers),
                        "asset_kills": list(asset_buckets.keys()),
                        "streaks": new_streaks,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        return out
