from __future__ import annotations

import json
import os
from typing import Any, Callable

from .campaign_constants import CURRENT_CAMPAIGN_ID
from .campaigns import get_active_campaign_id
from .config import TIMEFRAMES
from .db import add_event
from .llm_context import (
    collect_exploration_context,
    recent_critic_failures,
    recent_generation_count,
)
from .llm_parsing import (
    clean_hypotheses,
    extract_json,
    scoped_id,
    slug,
)
from .llm_prompts import (
    ALLOWED_ENGINES,
    SYSTEM_PROMPT,
    USER_PROMPT,
    THEORIST_SYSTEM_PROMPT,
    ENGINEER_SYSTEM_PROMPT,
)
from .llm_runtime import call_llm_agent
from .llm_stages import two_stage_generate
from .llm_traces import record_llm_trace
from .repository import role_for_timeframe, upsert_node


# Prompts e ALLOWED_ENGINES vivem em llm_prompts.py — re-importados acima.
def _collect_exploration_context(conn, campaign_id: str):
    context, dominant, dominance_pct, family_active, family_validated, _tf_active = collect_exploration_context(conn, campaign_id)
    return context, dominant, dominance_pct, family_active, family_validated


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def create_llm_hypothesis_batch(
    conn,
    *,
    max_new: int = 3,
    force_exploration: bool = False,
    caller: Callable[..., dict[str, Any]] = call_llm_agent,
) -> dict[str, Any]:
    campaign_id = get_active_campaign_id(conn)
    if os.environ.get("AETHER_LLM_ENABLED", "1") == "0":
        reason = os.environ.get("AETHER_LLM_DISABLED_REASON") or "LLM desativado por configuracao."
        return {
            "created_hypotheses": 0,
            "created_nodes": 0,
            "provider": None,
            "model": None,
            "skipped": "llm_disabled",
            "error": None,
            "reason": reason,
        }
    if recent_generation_count(conn, campaign_id) > 0 and not force_exploration:
        return {
            "created_hypotheses": 0,
            "created_nodes": 0,
            "provider": None,
            "model": None,
            "skipped": "cooldown",
            "error": None,
        }
    # Tree focus: bloqueia novas raizes quando ha muitos ramos abertos.
    # Forca o ciclo a finalizar / expandir antes de pesquisar coisas novas.
    try:
        from .tree_focus import should_open_new_branch
        allow_new, focus_reason, focus_state = should_open_new_branch(conn, campaign_id)
        if not allow_new and not force_exploration:
            add_event(
                conn,
                type="focus_block_new_root",
                campaign_id=campaign_id,
                agent_role="hypothesis_generator",
                message=f"Hypothesis Generator bloqueado pelo tree_focus: {focus_reason}",
                payload=focus_state,
            )
            return {
                "created_hypotheses": 0,
                "created_nodes": 0,
                "provider": None,
                "model": None,
                "skipped": "tree_focus",
                "focus_reason": focus_reason,
                "focus_state": focus_state,
                "error": None,
            }
    except Exception:
        pass
    # Throughput por chamada: padrao subiu de 3 -> 5 hipoteses por call, com
    # max_tokens correspondente ampliado pra acomodar a saida. Em Opus, output
    # custa muito mais que input, entao gerar 5 ao inves de 3 melhora ~67% de
    # diversidade por unidade de tempo, sem dobrar custo.
    # Pra acelerar mais drasticamente, use um modelo menor em
    # AETHER_OPENAI_ROLE_HYPOTHESIS_GENERATOR_MODEL no scout-pass e reserve um
    # modelo mais forte apenas para refino.
    max_new = max(1, min(max_new, _env_int("AETHER_LLM_MAX_NEW", 5)))
    add_event(
        conn,
        type="llm_research_started",
        campaign_id=campaign_id,
        agent_role="hypothesis_generator",
        message="Hypothesis Generator chamou agente LLM para novas hipoteses.",
    )
    (
        context_block,
        dominant_engine,
        dominance_pct,
        family_active,
        family_validated,
        tf_active,
    ) = collect_exploration_context(conn, campaign_id)
    # Memoria semantica: similares ja testados no historico (validados/podados).
    # Vira parte do contexto para que o LLM nao repita ideias ja exploradas.
    similar_block = ""
    try:
        from . import semantic_memory as _sm

        seed_text = f"familia dominante {dominant_engine} dominance {dominance_pct:.0f}% / {context_block}"
        similar = _sm.find_similar(conn, text=seed_text, k=_env_int("AETHER_SEMANTIC_K", 6))
        rendered = _sm.render_for_prompt(similar)
        if rendered:
            similar_block = "\n\nMemoria semantica (estrategias parecidas ja testadas):\n" + rendered
    except Exception:
        similar_block = ""
    # Feedback loop com Critic: top razoes de rejeicao recentes viram
    # avisos dentro do prompt. Pesquisador deixa de gerar ideias que ja sabe
    # que serao mortas pelos mesmos motivos (cross-symbol, regime, MTF, etc).
    critic_block = ""
    try:
        failures = recent_critic_failures(conn, campaign_id, days=7, top_n=5)
        if failures:
            critic_block = "\n\nFEEDBACK DO CRITIC (ultimas rejeicoes — EVITE estes modos de falha):\n" + "\n".join(
                f"- {n}x: {reason}" for n, reason in failures
            )
    except Exception:
        critic_block = ""

    prompt = USER_PROMPT.format(
        max_new=max_new,
        exploration_context=context_block + similar_block + critic_block,
        dominance_pct=f"{dominance_pct:.0f}",
    )
    if force_exploration:
        prompt += (
            "\n\nMODO EXPLORACAO OBRIGATORIA ATIVO:\n"
            "- Esta rodada PROIBE qualquer hipotese cuja unica engine seja a familia dominante.\n"
            "- Priorize combinacoes multi-engine ou um engine ainda pouco usado na arvore.\n"
            "- Se nao houver angulo realmente novo, retorne 'hypotheses': [] em vez de repetir.\n"
        )
        add_event(
            conn,
            type="exploration_forced_round",
            campaign_id=campaign_id,
            agent_role="hypothesis_generator",
            message=f"Rodada de exploracao forcada acionada (dominante={dominant_engine} {dominance_pct:.0f}%).",
            payload={"dominant_engine": dominant_engine, "dominance_pct": dominance_pct},
        )
    two_stage = _env_flag("AETHER_HYPOTHESIS_TWO_STAGE", "0")
    theorist_response: dict[str, Any] | None = None
    # A chamada ao Codex/OpenAI pode levar dezenas de segundos. Qualquer evento
    # gravado antes dela abre uma transacao SQLite; comitar aqui evita segurar
    # write-lock enquanto o modelo gera texto.
    try:
        conn.commit()
    except Exception:
        pass
    if two_stage:
        response, theorist_response = two_stage_generate(
            context_block=context_block,
            similar_block=similar_block,
            critic_block=critic_block,
            dominance_pct=dominance_pct,
            max_new=max_new,
            caller=caller,
        )
        # Trace separado pro Theorist (transparencia + custo)
        if theorist_response is not response:
            try:
                record_llm_trace(
                    conn,
                    agent_role="hypothesis_generator",
                    action="theorist_stage",
                    response=theorist_response,
                    system=THEORIST_SYSTEM_PROMPT,
                    prompt="(theorist stage)",
                    campaign_id=campaign_id,
                )
            except Exception:
                pass
        active_system = ENGINEER_SYSTEM_PROMPT
        active_prompt = "(engineer stage — two-stage on)"
    else:
        response = caller(
            "hypothesis_generator",
            system=SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=_env_int("AETHER_HYPOTHESIS_MAX_TOKENS", 6000),
        )
        active_system = SYSTEM_PROMPT
        active_prompt = prompt
    record_llm_trace(
        conn,
        agent_role="hypothesis_generator",
        action="hypothesis_generation",
        response=response,
        system=active_system,
        prompt=active_prompt,
        campaign_id=campaign_id,
    )
    if not response.get("ok"):
        add_event(
            conn,
            type="llm_research_unavailable",
            campaign_id=campaign_id,
            agent_role="hypothesis_generator",
            message=str(response.get("error") or "Agente LLM indisponivel.")[:500],
            payload={"provider": response.get("provider"), "model": response.get("model")},
        )
        return {"created_hypotheses": 0, "created_nodes": 0, "provider": response.get("provider"), "model": response.get("model"), "error": response.get("error")}

    try:
        payload = extract_json(str(response.get("text") or ""))
        hypotheses = clean_hypotheses(payload, max_new=max_new)
    except Exception as exc:
        add_event(
            conn,
            type="llm_research_parse_error",
            campaign_id=campaign_id,
            agent_role="hypothesis_generator",
            message=f"Resposta LLM nao virou JSON de hipotese: {exc}",
            payload={"provider": response.get("provider"), "model": response.get("model")},
        )
        return {"created_hypotheses": 0, "created_nodes": 0, "provider": response.get("provider"), "model": response.get("model"), "error": str(exc)}

    created = 0
    rejected_diversity = 0
    rejected_family_cap = 0
    rejected_duplicate = 0
    rejected_existing = 0
    existing_count = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE parent_id='root' AND campaign_id=? AND id LIKE '%llmhyp:%'",
        (campaign_id,),
    ).fetchone()["n"]
    # Limiares mais agressivos: dominance bloqueia em 40% (antes 60), e o cap
    # passa a valer mesmo quando a hipotese eh multi-engine (a familia saturada
    # tambem saturava em compostas, so que ninguem barrava).
    DOMINANCE_BLOCK_PCT = float(_env_int("AETHER_DOMINANCE_BLOCK_PCT", 25 if force_exploration else 40))
    FAMILY_NODE_CAP = _env_int("AETHER_FAMILY_NODE_CAP", 8)
    FAMILY_VALIDATED_CAP = _env_int("AETHER_FAMILY_VALIDATED_CAP", 4)
    for spec in hypotheses:
        spec_engines = {e.lower() for e in spec["engines"]}
        # Bloqueio de dominancia: vale tanto pra single-engine puro quanto pra
        # multi-engine onde TODAS as engines pertencem a familia dominante (eg.
        # "bollinger + bollinger_squeeze" — formalmente diferente, mesma familia).
        if (
            dominant_engine
            and dominance_pct >= DOMINANCE_BLOCK_PCT
            and dominant_engine in spec_engines
            and len(spec_engines - {dominant_engine}) == 0
        ):
            rejected_diversity += 1
            add_event(
                conn,
                type="hypothesis_rejected_diversity",
                campaign_id=campaign_id,
                agent_role="hypothesis_generator",
                message=(
                    f"Hipotese '{spec['name']}' rejeitada: familia '{dominant_engine}' "
                    f"ja domina {dominance_pct:.0f}% da arvore."
                ),
                payload={"slug": spec["slug"], "engines": list(spec_engines), "dominance_pct": dominance_pct},
            )
            continue
        # Cap por familia: ANY engine da spec que ja esta saturada bloqueia a hipotese.
        # Antes so valia pra single-engine, entao multi-engine driblava o cap injetando
        # uma familia saturada como "ingrediente extra" e mantia a concentracao.
        saturated_engines: list[tuple[str, int, int]] = []
        for eng in spec_engines:
            active_count = family_active.get(eng, 0)
            validated_count = family_validated.get(eng, 0)
            if (FAMILY_NODE_CAP > 0 and active_count >= FAMILY_NODE_CAP) or (
                FAMILY_VALIDATED_CAP > 0 and validated_count >= FAMILY_VALIDATED_CAP
            ):
                saturated_engines.append((eng, active_count, validated_count))
        if saturated_engines:
            rejected_family_cap += 1
            engs_msg = ", ".join(
                f"{e}({a}a/{v}v)" for e, a, v in saturated_engines
            )
            add_event(
                conn,
                type="hypothesis_rejected_family_cap",
                campaign_id=campaign_id,
                agent_role="hypothesis_generator",
                message=(
                    f"Hipotese '{spec['name']}' rejeitada: familia(s) saturada(s) "
                    f"[{engs_msg}] vs cap nodes={FAMILY_NODE_CAP}, val={FAMILY_VALIDATED_CAP}."
                ),
                payload={
                    "slug": spec["slug"],
                    "engines": list(spec_engines),
                    "saturated": [e for e, _, _ in saturated_engines],
                    "active_cap": FAMILY_NODE_CAP,
                    "validated_cap": FAMILY_VALIDATED_CAP,
                },
            )
            continue
        parent_id = scoped_id(campaign_id, f"llmhyp:{spec['slug']}")
        slug_existed = (
            conn.execute("SELECT 1 FROM nodes WHERE id=?", (parent_id,)).fetchone()
            or conn.execute(
                "SELECT 1 FROM nodes WHERE parent_id='root' AND campaign_id=? AND id LIKE ?",
                (campaign_id, f"%llmhyp:{spec['slug']}%"),
            ).fetchone()
        )
        if slug_existed:
            # LLM propos hipotese com slug que ja existe (provavelmente loop de
            # sessao reusada). Conta e emite evento — antes era continue silencioso,
            # o que mascarava loops de LLM propondo a mesma hipotese repetidamente
            # e queimava tokens sem produzir nada (cada call ~$0.40).
            rejected_existing += 1
            add_event(
                conn,
                type="hypothesis_rejected_existing",
                campaign_id=campaign_id,
                agent_role="hypothesis_generator",
                message=(
                    f"Hipotese '{spec['name']}' rejeitada: slug '{spec['slug']}' ja existe "
                    f"na arvore (LLM propos duplicata, possivel loop de sessao)."
                ),
                payload={"slug": spec["slug"], "existing_id": parent_id},
            )
            continue
        # Gate de duplicata semantica: bloqueia hipoteses praticamente identicas
        # a alguma ja salva (validada/em teste/podada). Usa ST encoder quando
        # disponivel; com fallback hash ainda barra duplicatas estruturais.
        try:
            from . import semantic_memory as _sm
            candidate_text = " | ".join([
                spec["name"],
                spec["hypothesis"],
                spec["causal_mechanism"],
                " ".join(spec["engines"]),
            ])
            dup_threshold = float(os.environ.get("AETHER_DUPLICATE_THRESHOLD", "0.92"))
            is_dup, dup_match = _sm.is_duplicate_idea(
                conn, text=candidate_text, threshold=dup_threshold
            )
            if is_dup and dup_match:
                rejected_duplicate += 1
                add_event(
                    conn,
                    type="hypothesis_rejected_duplicate",
                    campaign_id=campaign_id,
                    agent_role="hypothesis_generator",
                    message=(
                        f"Hipotese '{spec['name']}' rejeitada: similar a node existente "
                        f"'{dup_match.get('node_id')}' (sim={float(dup_match.get('similarity', 0.0)):.3f})."
                    ),
                    payload={
                        "slug": spec["slug"],
                        "match_node_id": dup_match.get("node_id"),
                        "similarity": dup_match.get("similarity"),
                        "match_status": dup_match.get("status"),
                        "match_engine": dup_match.get("engine"),
                        "threshold": dup_threshold,
                    },
                )
                continue
        except Exception:
            pass
        base_score = float(spec["base_score"])
        upsert_node(
            conn,
            id=parent_id,
            campaign_id=campaign_id,
            parent_id="root",
            code=f"LLM-{existing_count + created + 1}",
            name=spec["name"],
            hypothesis=spec["hypothesis"],
            causal_mechanism=spec["causal_mechanism"],
            mutation_from_parent=f"Agente LLM {response.get('provider')} / {response.get('model')}",
            agent_role="hypothesis_generator",
            status="visited",
            score=base_score,
            priority=0.09,
            depth=1,
            metrics={
                "engines": spec["engines"],
                "probe_specs": spec["probe_specs"],
                "generator": "llm_agent",
                "provider": response.get("provider"),
                "model": response.get("model"),
                "invalidation": spec["invalidation"],
            },
            tags=["hypothesis", "llm", *(f"engine:{engine}" for engine in spec["engines"])],
            source_type="llm_agent",
            source_path=str(response.get("provider") or "llm"),
        )
        # TF-aware spawning: cada filho herda o base_score mas leva BONUS se o
        # timeframe esta sub-explorado na arvore atual. TFs com <10% de share
        # ganham +20% de prioridade; TFs >25% sofrem -10%. Ajuste pequeno mas
        # bate forte ao longo de muitas hipoteses, sem violar a ordenacao geral.
        tf_total_now = sum(tf_active.values()) or 1
        for idx, timeframe in enumerate(TIMEFRAMES, start=1):
            tf_share = tf_active.get(timeframe, 0) / tf_total_now
            if tf_share < 0.10:
                tf_factor = 1.20  # underused: empurra pra cima
            elif tf_share > 0.25:
                tf_factor = 0.90  # over-explored: leve handicap
            else:
                tf_factor = 1.00
            child_score = max(0.1, (base_score - idx * 0.02) * tf_factor)
            upsert_node(
                conn,
                id=f"{parent_id}:tf:{timeframe}",
                campaign_id=campaign_id,
                parent_id=parent_id,
                code=f"LLM-{timeframe}",
                name=f"{spec['name']} / {timeframe}",
                hypothesis=f"{spec['hypothesis']} Timeframe alvo: {timeframe}.",
                causal_mechanism=spec["causal_mechanism"],
                mutation_from_parent=f"Especializacao LLM para {timeframe}.",
                timeframe=timeframe,
                agent_role=role_for_timeframe(timeframe),
                status="candidate",
                score=child_score,
                priority=child_score,
                depth=2,
                metrics={
                    "engines": spec["engines"],
                    "probe_specs": spec["probe_specs"],
                    "generator": "llm_agent",
                    "provider": response.get("provider"),
                    "model": response.get("model"),
                    "invalidation": spec["invalidation"],
                },
                tags=["hypothesis", "llm", timeframe, *(f"engine:{engine}" for engine in spec["engines"])],
                source_type="llm_agent",
                source_path=str(response.get("provider") or "llm"),
            )
        created += 1

    add_event(
        conn,
        type="llm_research_completed",
        campaign_id=campaign_id,
        agent_role="hypothesis_generator",
        message=(
            f"Agente LLM criou {created} hipoteses; "
            f"{rejected_diversity} bloqueadas por diversidade; "
            f"{rejected_family_cap} bloqueadas por cap de familia; "
            f"{rejected_duplicate} bloqueadas por duplicata semantica; "
            f"{rejected_existing} ja existiam na arvore."
        ),
        payload={
            "provider": response.get("provider"),
            "model": response.get("model"),
            "created": created,
            "rejected_diversity": rejected_diversity,
            "rejected_family_cap": rejected_family_cap,
            "rejected_duplicate": rejected_duplicate,
            "rejected_existing": rejected_existing,
            "dominant_engine": dominant_engine,
            "dominance_pct": dominance_pct,
        },
    )
    # Anti-loop: se TODAS as hipoteses propostas pelo LLM ja existem na arvore,
    # a sessao do provedor provavelmente esta presa repetindo. Detecta sequencia de
    # 2+ ciclos com 100% existing e invalida a sessao para forcar um restart
    # de contexto. Sem isso, o LLM queima ~$0.40/call em loop infinito.
    proposed = len(hypotheses)
    if proposed > 0 and rejected_existing == proposed and created == 0:
        try:
            from .db import get_setting, set_setting
            counter = int((get_setting(conn, "llm_loop_counter", default=0) or 0)) + 1
            set_setting(conn, "llm_loop_counter", counter)
            if counter >= 2:
                from .llm_runtime import _session_store_path
                p = _session_store_path()
                if p.exists():
                    p.unlink()
                set_setting(conn, "llm_loop_counter", 0)
                add_event(
                    conn,
                    type="llm_loop_detected",
                    campaign_id=campaign_id,
                    agent_role="hypothesis_generator",
                    message=(
                        f"Loop de sessao detectado: {counter} ciclos consecutivos "
                        f"com 100% das hipoteses ja existentes. Sessao LLM invalidada "
                        f"para forcar contexto novo."
                    ),
                    payload={"consecutive_loops": counter, "proposed": proposed},
                )
        except Exception:
            pass
    elif created > 0:
        try:
            from .db import set_setting
            set_setting(conn, "llm_loop_counter", 0)
        except Exception:
            pass

    return {
        "created_hypotheses": created,
        "created_nodes": created * (len(TIMEFRAMES) + 1),
        "provider": response.get("provider"),
        "model": response.get("model"),
        "rejected_diversity": rejected_diversity,
        "rejected_family_cap": rejected_family_cap,
        "rejected_duplicate": rejected_duplicate,
        "rejected_existing": rejected_existing,
        "dominant_engine": dominant_engine,
        "dominance_pct": dominance_pct,
        "error": None,
    }
