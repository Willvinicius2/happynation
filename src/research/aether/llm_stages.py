"""Two-stage hypothesis generation: Theorist -> Engineer.

Tier B opt-in (env var AETHER_HYPOTHESIS_TWO_STAGE=1). Em vez de pedir ao LLM
para gerar JSON direto, roda em duas etapas:
  1) Theorist: pensa em texto livre sobre uma TESE causal nova.
  2) Engineer: traduz a tese em probe_specs JSON usando os engines disponiveis.

Reduz overfit a parametros (Theorist nao se preocupa com numeros) e da espaco
para teses mais ricas. Custa 2x chamadas LLM em vez de 1.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .llm_prompts import (
    ENGINEER_SYSTEM_PROMPT,
    ENGINEER_USER_PROMPT,
    THEORIST_SYSTEM_PROMPT,
    THEORIST_USER_PROMPT,
    engine_catalog_for_engineer,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def two_stage_generate(
    *,
    context_block: str,
    similar_block: str,
    critic_block: str,
    dominance_pct: float,
    max_new: int,
    caller: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Roda Theorist (texto livre) -> Engineer (JSON).

    Returns:
        (engineer_response, theorist_response). Quando o Theorist falha,
        retorna o mesmo objeto duas vezes para propagar o erro adiante.
    """
    failures_text = critic_block.strip() or "(nenhuma rejeicao recente registrada)"
    theorist_prompt = THEORIST_USER_PROMPT.format(
        exploration_context=context_block + similar_block,
        critic_failures=failures_text,
    )
    theorist_resp = caller(
        "hypothesis_generator",
        system=THEORIST_SYSTEM_PROMPT,
        prompt=theorist_prompt,
        max_tokens=_env_int("AETHER_THEORIST_MAX_TOKENS", 3000),
    )
    if not theorist_resp.get("ok"):
        return theorist_resp, theorist_resp  # mesmo objeto pra propagar erro
    theory = str(theorist_resp.get("text") or "")[:6000]
    engineer_prompt = ENGINEER_USER_PROMPT.format(
        theory=theory,
        engine_catalog=engine_catalog_for_engineer(),
        dominance_pct=f"{dominance_pct:.0f}",
        context_snippet=context_block[:600],
        max_new=max_new,
    )
    engineer_resp = caller(
        "hypothesis_generator",
        system=ENGINEER_SYSTEM_PROMPT,
        prompt=engineer_prompt,
        max_tokens=_env_int("AETHER_ENGINEER_MAX_TOKENS", 2000),
    )
    return engineer_resp, theorist_resp
