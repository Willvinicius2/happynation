from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.agents.base import AgentFinding, AgentSeverity


SYSTEM_PROMPT = """Voce e um gerador de hipoteses de estrategias cripto.
Crie ideias testaveis em backtest local. Nao prometa lucro. Nao use ROI sozinho.
Responda somente JSON valido, sem comentario."""

USER_PROMPT_TEMPLATE = """Gere {max_new} hipoteses estruturais novas para serem testadas no bot HAPPYNATION.

Templates disponiveis no projeto: {templates}.
Regimes de mercado validos: bull, bear, lateral, vol_alta, vol_baixa.

Formato obrigatorio:
{{
  "hypotheses": [
    {{
      "slug": "nome-curto-sem-acentos",
      "name": "Nome da estrategia",
      "hypothesis": "Tese testavel em backtest",
      "causal_mechanism": "Por que isso funcionaria",
      "invalidation": "Quando esse ramo e cortado",
      "template": "um dos templates listados",
      "params": {{"chave": valor}},
      "risk": {{"take_profit_pct": 2.0, "stop_loss_pct": 1.5, "max_hold_candles": 96}},
      "validated_regime": "regime ideal para a estrategia",
      "base_score": 0.55
    }}
  ]
}}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text[:54] or "hypothesis"


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            raw = match.group(0)
    return json.loads(raw)


def _load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": _now_iso(), "rounds": 0, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "updated_at": _now_iso(), "rounds": 0, "entries": {}}
    if "entries" not in data:
        data["entries"] = {}
    return data


def _entries_as_dict(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = catalog.get("entries", {})
    if isinstance(entries, list):
        return {entry.get("id") or _hash(json.dumps(entry, sort_keys=True)): entry for entry in entries if isinstance(entry, dict)}
    if isinstance(entries, dict):
        return {k: v for k, v in entries.items() if isinstance(v, dict)}
    return {}


class AetherResearcherAgent:
    name = "aether_researcher_agent"
    display_name = "Caçador IA"

    def __init__(
        self,
        catalog_path: Path | str = "src/logs/research/strategy_catalog.json",
        max_new: int = 3,
        templates: tuple[str, ...] | None = None,
        write_enabled_env: str = "AETHER_RESEARCHER_WRITE_ENABLED",
        caller: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.max_new = max_new
        self.templates = templates or self._discover_templates()
        self.write_enabled_env = write_enabled_env
        self._caller = caller

    def _discover_templates(self) -> tuple[str, ...]:
        try:
            from src.research import templates as templates_module
            registry = getattr(templates_module, "TEMPLATES", None) or getattr(
                templates_module, "TEMPLATE_REGISTRY", None
            )
            if isinstance(registry, dict):
                return tuple(registry.keys())
            if isinstance(registry, (list, tuple)):
                return tuple(registry)
        except Exception:
            pass
        return ("RSI_Reversal", "TrendMomentumConfluence", "BollingerSqueeze", "MA_Crossover")

    def _resolve_caller(self) -> Callable[..., dict[str, Any]] | None:
        if self._caller is not None:
            return self._caller
        try:
            from src.research.aether.llm_runtime import call_llm_agent
            return call_llm_agent
        except Exception:
            return None

    def run(self) -> list[AgentFinding]:
        caller = self._resolve_caller()
        if caller is None:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Aether nao disponivel",
                    message="Modulo Aether (llm_runtime) nao pode ser importado.",
                    action="Verificar src/research/aether e dependencias (pyyaml).",
                )
            ]

        try:
            from src.research.aether.llm_runtime import llm_status
            status = llm_status()
        except Exception as exc:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Status do Aether falhou",
                    message=f"Nao foi possivel consultar status do Aether: {exc}",
                )
            ]

        role_status = status.get("roles", {}).get("hypothesis_generator", {})
        provider_name = role_status.get("provider") or "openai"
        provider_status = status.get("providers", {}).get(provider_name, {})
        if not role_status.get("configured") and not provider_status.get("configured"):
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="LLM do Aether nao configurado",
                    message="Configure OpenAI para habilitar a IA do Aether.",
                    action="Definir OPENAI_API_KEY ou autenticar o Codex CLI.",
                    metadata={"role": role_status, "provider": provider_status},
                )
            ]

        prompt = USER_PROMPT_TEMPLATE.format(
            max_new=self.max_new,
            templates=", ".join(self.templates),
        )
        try:
            response = caller(
                "hypothesis_generator",
                system=SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=1800,
            )
        except Exception as exc:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Chamada ao Aether falhou",
                    message=str(exc)[:300],
                    action="Inspecionar logs e verificar OPENAI_API_KEY/Codex CLI.",
                )
            ]

        if not response.get("ok"):
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Aether respondeu com erro",
                    message=str(response.get("error") or "resposta sem ok=True")[:300],
                    metadata={"provider": response.get("provider"), "model": response.get("model")},
                )
            ]

        try:
            payload = _extract_json(str(response.get("text") or ""))
            hypotheses = payload.get("hypotheses", [])
            if not isinstance(hypotheses, list):
                raise ValueError("hypotheses nao e lista")
        except Exception as exc:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.WARNING,
                    title="Resposta do Aether nao virou JSON",
                    message=str(exc)[:300],
                )
            ]

        cleaned = self._clean_hypotheses(hypotheses)
        if not cleaned:
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title="Aether nao trouxe hipoteses validas",
                    message=f"{len(hypotheses)} brutas, 0 limpas.",
                )
            ]

        write_enabled = os.environ.get(self.write_enabled_env, "").strip().lower() in {"1", "true", "yes"}
        if write_enabled:
            added = self._append_to_catalog(cleaned, response)
            return [
                AgentFinding(
                    agent=self.name,
                    display_name=self.display_name,
                    severity=AgentSeverity.INFO,
                    title=f"{added} candidata(s) Aether adicionada(s) ao catalogo",
                    message=f"Origem aether_llm. Aguardam auditoria do Juiz.",
                    action="Rodar Juiz e Caçador na proxima execucao.",
                    metadata={
                        "added": added,
                        "provider": response.get("provider"),
                        "model": response.get("model"),
                        "templates": [h["template"] for h in cleaned],
                    },
                )
            ]

        return [
            AgentFinding(
                agent=self.name,
                display_name=self.display_name,
                severity=AgentSeverity.INFO,
                title=f"{len(cleaned)} hipotese(s) Aether disponivel(is)",
                message=(
                    f"Aether gerou {len(cleaned)} candidata(s) mas {self.write_enabled_env} nao esta ligado."
                ),
                action=f"Setar {self.write_enabled_env}=1 para gravar no catalogo.",
                metadata={
                    "preview": [
                        {
                            "name": h["name"],
                            "template": h["template"],
                            "params": h["params"],
                        }
                        for h in cleaned
                    ],
                    "provider": response.get("provider"),
                    "model": response.get("model"),
                },
            )
        ]

    def _clean_hypotheses(self, raw: list[Any]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            template = item.get("template")
            if template not in self.templates:
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            risk = item.get("risk") if isinstance(item.get("risk"), dict) else {}
            slug = _slug(item.get("slug") or item.get("name") or template)
            hypothesis = str(item.get("hypothesis") or "")
            cleaned.append(
                {
                    "slug": slug,
                    "name": str(item.get("name") or template),
                    "template": template,
                    "params": params,
                    "risk": risk,
                    "hypothesis": hypothesis,
                    "causal_mechanism": str(item.get("causal_mechanism") or ""),
                    "invalidation": str(item.get("invalidation") or ""),
                    "validated_regime": str(item.get("validated_regime") or "lateral"),
                    "base_score": float(item.get("base_score") or 0.0),
                }
            )
        return cleaned

    def _append_to_catalog(
        self, cleaned: list[dict[str, Any]], response: dict[str, Any]
    ) -> int:
        catalog = _load_catalog(self.catalog_path)
        entries = _entries_as_dict(catalog)
        provider = response.get("provider")
        model = response.get("model")
        added = 0
        now = _now_iso()
        for spec in cleaned:
            entry_id = f"aether-{spec['slug']}-{_hash(json.dumps(spec['params'], sort_keys=True))}"
            if entry_id in entries:
                continue
            entries[entry_id] = {
                "id": entry_id,
                "template": spec["template"],
                "params": spec["params"],
                "risk": spec["risk"],
                "status": "pending",
                "score": spec["base_score"],
                "validated_regime": spec["validated_regime"],
                "summary": {},
                "scenarios": [],
                "discovered_at": now,
                "approved_at": None,
                "rejected_at": None,
                "rejection_reason": None,
                "origin": "aether_llm",
                "parents": [],
                "metadata": {
                    "name": spec["name"],
                    "hypothesis": spec["hypothesis"],
                    "causal_mechanism": spec["causal_mechanism"],
                    "invalidation": spec["invalidation"],
                    "provider": provider,
                    "model": model,
                },
            }
            added += 1

        if not added:
            return 0

        catalog["entries"] = entries
        catalog["updated_at"] = now
        catalog["rounds"] = int(catalog.get("rounds", 0) or 0) + 1
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return added
