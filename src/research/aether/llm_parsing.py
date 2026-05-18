"""Parsing/cleaning helpers do Hypothesis Generator.

Funcoes puras (sem efeitos colaterais, sem DB) que transformam strings/dicts.
Extraidas de `llm_hypothesis.py` para isolar logica de parsing da logica de
fluxo (que vive em llm_hypothesis.py).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .campaign_constants import CURRENT_CAMPAIGN_ID
from .llm_prompts import ALLOWED_ENGINES


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text[:54] or "llm-hypothesis"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def scoped_id(campaign_id: str, base_id: str) -> str:
    """Aplica prefixo de campanha quando nao eh a campanha atual."""
    if campaign_id == CURRENT_CAMPAIGN_ID:
        return base_id
    return f"{campaign_id}:{base_id}"


def extract_json(text: str) -> dict[str, Any]:
    """Extrai JSON de uma resposta LLM, tolerando code fences e prefixos."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            raw = match.group(0)
    return json.loads(raw)


def clean_probe_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filtra probe_specs validos: engine na ALLOWED_ENGINES + params dict."""
    out = []
    for spec in specs:
        engine = str(spec.get("engine") or "")
        if engine not in ALLOWED_ENGINES:
            continue
        params = spec.get("params")
        if not isinstance(params, dict):
            params = {}
        out.append({"engine": engine, "params": params})
    return out


def clean_hypotheses(payload: dict[str, Any], *, max_new: int) -> list[dict[str, Any]]:
    """Normaliza a lista de hipoteses retornadas pelo LLM.

    - Trunca tamanhos de string (slug=54, name=120, hypothesis=800, etc).
    - Filtra entries sem engines/probe_specs validos.
    - Limita a `max_new`.
    """
    hypotheses = payload.get("hypotheses")
    if not isinstance(hypotheses, list):
        return []
    cleaned = []
    for item in hypotheses:
        if not isinstance(item, dict):
            continue
        probe_specs = clean_probe_specs(list(item.get("probe_specs") or []))
        engines = sorted({spec["engine"] for spec in probe_specs} | {str(e) for e in item.get("engines") or [] if str(e) in ALLOWED_ENGINES})
        if not engines or not probe_specs:
            continue
        name = str(item.get("name") or "LLM hypothesis")
        cleaned.append(
            {
                "slug": slug(str(item.get("slug") or name)),
                "name": name[:120],
                "hypothesis": str(item.get("hypothesis") or name)[:800],
                "causal_mechanism": str(item.get("causal_mechanism") or "")[:800],
                "invalidation": str(item.get("invalidation") or "Scout negativo ou drawdown excessivo.")[:400],
                "engines": engines,
                "probe_specs": probe_specs,
                "base_score": max(0.1, min(0.75, float(item.get("base_score") or 0.55))),
            }
        )
        if len(cleaned) >= max_new:
            break
    return cleaned
