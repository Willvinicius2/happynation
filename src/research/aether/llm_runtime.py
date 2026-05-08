from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time as _time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import runtime_dir


# Circuit breaker por provider: depois de N falhas consecutivas dispara
# uma janela de cooldown durante a qual o provider eh pulado direto. Evita
# travar a fila com timeouts repetidos quando a API esta down.
_CB_LOCK = threading.Lock()
_CB_STATE: dict[str, dict[str, float]] = {}


def _cb_threshold() -> int:
    try:
        return max(1, int(os.environ.get("AETHER_LLM_CB_THRESHOLD", "5")))
    except (TypeError, ValueError):
        return 5


def _cb_cooldown() -> float:
    try:
        return max(10.0, float(os.environ.get("AETHER_LLM_CB_COOLDOWN", "120")))
    except (TypeError, ValueError):
        return 120.0


def _cb_is_open(provider_name: str) -> tuple[bool, float]:
    now = _time.time()
    with _CB_LOCK:
        st = _CB_STATE.get(provider_name)
        if not st:
            return False, 0.0
        until = float(st.get("open_until") or 0.0)
        if until > now:
            return True, until - now
        return False, 0.0


def _cb_record_failure(provider_name: str) -> None:
    now = _time.time()
    with _CB_LOCK:
        st = _CB_STATE.setdefault(provider_name, {"fails": 0.0, "open_until": 0.0})
        st["fails"] = float(st.get("fails") or 0.0) + 1
        if st["fails"] >= _cb_threshold():
            st["open_until"] = now + _cb_cooldown()
            st["fails"] = 0.0


def _cb_record_success(provider_name: str) -> None:
    with _CB_LOCK:
        _CB_STATE[provider_name] = {"fails": 0.0, "open_until": 0.0}


def _provider_cb_key(provider: "LLMProvider") -> str:
    return f"{provider.name}:{provider.transport}"


ROLE_ASSIGNMENTS = {
    "chief_orchestrator": "claude",
    "hypothesis_generator": "claude",
    "archivist_agent": "claude",
    "validation_agent": "claude",
    "portfolio_agent": "claude",
    "manifest_agent": "claude",
    "infra_data_agent": "claude",
}

ROLE_FALLBACKS = {
    "chief_orchestrator": ("codex",),
    "hypothesis_generator": ("codex",),
    "archivist_agent": ("codex",),
    "validation_agent": ("codex",),
    "portfolio_agent": ("codex",),
    "manifest_agent": ("codex",),
    "infra_data_agent": ("codex",),
}

ROLE_TIERS: dict[str, dict[str, str]] = {
    "chief_orchestrator":   {"model": "claude-haiku-4-5-20251001",  "effort": "low"},
    "hypothesis_generator": {"model": os.environ.get("AETHER_HYPOTHESIS_MODEL", "claude-sonnet-4-6"), "effort": os.environ.get("AETHER_HYPOTHESIS_EFFORT", "high")},
    "archivist_agent":      {"model": "claude-haiku-4-5-20251001",  "effort": "low"},
    "validation_agent":     {"model": os.environ.get("AETHER_VALIDATION_MODEL", "claude-sonnet-4-6"), "effort": "low"},
    "portfolio_agent":      {"model": os.environ.get("AETHER_PORTFOLIO_MODEL",  "claude-sonnet-4-6"), "effort": "low"},
    "manifest_agent":       {"model": "claude-haiku-4-5-20251001",  "effort": "low"},
    "infra_data_agent":     {"model": "claude-haiku-4-5-20251001",  "effort": "low"},
}

CODEX_ROLE_TIERS: dict[str, dict[str, str | None]] = {
    "chief_orchestrator":   {"model": "gpt-5.4-mini", "effort": "medium"},
    "hypothesis_generator": {"model": "gpt-5.4-mini", "effort": "medium"},
    "archivist_agent":      {"model": "gpt-5.4-mini", "effort": "low"},
    "validation_agent":     {"model": "gpt-5.4-mini", "effort": "medium"},
    "portfolio_agent":      {"model": "gpt-5.4-mini", "effort": "medium"},
    "manifest_agent":       {"model": "gpt-5.4-mini", "effort": "low"},
    "infra_data_agent":     {"model": "gpt-5.4-mini", "effort": "low"},
}


def _role_tier_override(role: str) -> dict[str, str] | None:
    if not _env_enabled("AETHER_ECONOMY_MODE", "1"):
        return None
    tier = ROLE_TIERS.get(role)
    if not tier:
        return None
    role_upper = role.upper()
    model = os.environ.get(f"AETHER_ROLE_{role_upper}_MODEL") or tier["model"]
    effort = os.environ.get(f"AETHER_ROLE_{role_upper}_EFFORT") or tier["effort"]
    return {"model": model, "effort": effort}


def _codex_role_tier_override(role: str) -> dict[str, str | None] | None:
    if not _env_enabled("AETHER_ECONOMY_MODE", "1"):
        return None
    tier = CODEX_ROLE_TIERS.get(role)
    if not tier:
        return None
    role_upper = role.upper()
    model = os.environ.get(f"AETHER_CODEX_ROLE_{role_upper}_MODEL") or str(tier["model"])
    effort = os.environ.get(f"AETHER_CODEX_ROLE_{role_upper}_EFFORT") or tier.get("effort")
    return {"model": model, "effort": effort}


@dataclass(frozen=True)
class LLMProvider:
    name: str
    label: str
    model: str
    effort: str | None
    key_env: str
    configured: bool
    transport: str
    cli_path: str | None = None


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


_ROLE_MAX_TOKENS_DEFAULTS: dict[str, int] = {
    # Hypothesis generator precisa de espaco pra raciocinar antes de cuspir o
    # JSON. Cap antigo (700) cortava a teoria — subimos pra 4000 mesmo em
    # economy mode pra suportar o prompt expandido (Tier A).
    "hypothesis_generator": 4000,
}


def _budgeted_max_tokens(role: str, requested: int) -> int:
    if not _env_enabled("AETHER_ECONOMY_MODE", "1"):
        return requested
    role_key = f"AETHER_ROLE_{role.upper()}_MAX_TOKENS"
    # Ordem: env-var role-especifico > default por role (se houver) > env-var global > 700.
    if role in _ROLE_MAX_TOKENS_DEFAULTS:
        cap = _env_int(role_key, _ROLE_MAX_TOKENS_DEFAULTS[role])
    else:
        cap = _env_int(role_key, _env_int("AETHER_LLM_MAX_TOKENS", 700))
    return max(128, min(requested, cap))


def _session_store_path() -> Path:
    return runtime_dir() / "llm_sessions.json"


def _load_session_store() -> dict[str, Any]:
    path = _session_store_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_session_store(store: dict[str, Any]) -> None:
    path = _session_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _session_key(role: str | None, provider: LLMProvider, system: str) -> str:
    cwd = os.environ.get("AETHER_CLAUDE_WORKDIR") or os.getcwd()
    payload = {
        "role": role or "unknown",
        "provider": provider.name,
        "model": provider.model,
        "effort": provider.effort,
        "workdir": str(Path(cwd).resolve()),
        "system_hash": hashlib.sha1(system.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _get_reusable_session(role: str | None, provider: LLMProvider, system: str) -> tuple[str, dict[str, Any] | None]:
    if not _env_enabled("AETHER_CLAUDE_SESSION_REUSE", "1"):
        return "", None
    store = _load_session_store()
    key = _session_key(role, provider, system)
    entry = store.get(key)
    if not isinstance(entry, dict):
        return key, None
    max_calls = max(1, _env_int("AETHER_CLAUDE_SESSION_MAX_CALLS", 20))
    if int(entry.get("calls") or 0) >= max_calls:
        return key, None
    session_id = str(entry.get("session_id") or "")
    if not session_id:
        return key, None
    return key, entry


def _remember_session(key: str, session_id: str | None, *, reused: bool) -> None:
    if not key or not session_id or not _env_enabled("AETHER_CLAUDE_SESSION_REUSE", "1"):
        return
    store = _load_session_store()
    prior = store.get(key) if isinstance(store.get(key), dict) else {}
    calls = int(prior.get("calls") or 0) + 1 if reused and prior else 1
    store[key] = {"session_id": session_id, "calls": calls}
    _save_session_store(store)


def _forget_session(key: str) -> None:
    if not key:
        return
    store = _load_session_store()
    if key in store:
        store.pop(key, None)
        _save_session_store(store)


def _provider_for_name(name: str) -> LLMProvider:
    if name == "claude":
        key = _env("AETHER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        transport = os.environ.get("AETHER_CLAUDE_TRANSPORT", "claude_code").strip().lower()
        cli_path = shutil.which("claude")
        configured = bool(cli_path) if transport in {"claude_code", "cli"} else bool(key)
        if transport == "auto":
            configured = bool(cli_path or key)
            transport = "claude_code" if cli_path else "api"
        return LLMProvider(
            name="claude",
            label="Claude",
            model=os.environ.get("AETHER_CLAUDE_MODEL", "claude-opus-4-7"),
            effort=os.environ.get("AETHER_CLAUDE_EFFORT", "xhigh"),
            key_env="AETHER_ANTHROPIC_API_KEY|ANTHROPIC_API_KEY",
            configured=configured,
            transport=transport,
            cli_path=cli_path,
        )
    transport = os.environ.get("AETHER_CODEX_TRANSPORT", "auto").strip().lower()
    cli_path = shutil.which("codex")
    api_enabled = _env_enabled("AETHER_OPENAI_API_FALLBACK_ENABLED")
    key = _env("AETHER_OPENAI_API_KEY", "OPENAI_API_KEY") if api_enabled else None
    if transport == "auto":
        transport = "codex_cli" if cli_path else "api"
    configured = bool(cli_path) if transport in {"codex_cli", "cli"} else bool(key)
    if transport == "api" and not api_enabled:
        configured = False
    return LLMProvider(
        name="codex",
        label="Codex",
        model=os.environ.get("AETHER_CODEX_MODEL", "gpt-5.4-mini"),
        effort=None,
        key_env="AETHER_OPENAI_API_FALLBACK_ENABLED=1 + AETHER_OPENAI_API_KEY|OPENAI_API_KEY",
        configured=configured,
        transport=transport,
        cli_path=cli_path,
    )


def llm_status() -> dict[str, Any]:
    provider_names = set(ROLE_ASSIGNMENTS.values())
    for fallbacks in ROLE_FALLBACKS.values():
        provider_names.update(fallbacks)
    providers = {name: _provider_for_name(name) for name in sorted(provider_names)}
    roles: dict[str, Any] = {}
    for role, provider_name in ROLE_ASSIGNMENTS.items():
        primary = role_provider(role)
        fallback_providers = []
        for fallback_name in ROLE_FALLBACKS.get(role, ()):
            fallback = _provider_for_name(fallback_name)
            tier = _codex_role_tier_override(role) if fallback.name == "codex" else _role_tier_override(role)
            if tier:
                fallback = dataclasses.replace(fallback, model=tier["model"], effort=tier["effort"])
            fallback_providers.append(fallback)
        roles[role] = {
            "provider": provider_name,
            "fallbacks": [
                {
                    "provider": fallback.name,
                    "configured": fallback.configured,
                    "model": fallback.model,
                    "effort": fallback.effort,
                    "transport": fallback.transport,
                }
                for fallback in fallback_providers
            ],
            "configured": primary.configured or any(fallback.configured for fallback in fallback_providers),
            "model": primary.model,
            "effort": primary.effort,
            "transport": primary.transport,
            "economy_mode": _env_enabled("AETHER_ECONOMY_MODE", "1"),
        }

    return {
        "enabled": os.environ.get("AETHER_LLM_ENABLED", "1") != "0",
        "economy_mode": _env_enabled("AETHER_ECONOMY_MODE", "1"),
        "providers": {
            name: {
                "label": provider.label,
                "model": provider.model,
                "effort": provider.effort,
                "configured": provider.configured,
                "key_env": provider.key_env,
                "transport": provider.transport,
                "cli_path": provider.cli_path,
            }
            for name, provider in providers.items()
        },
        "roles": roles,
    }


def role_provider(role: str) -> LLMProvider:
    base = _provider_for_name(ROLE_ASSIGNMENTS.get(role, "claude"))
    tier = _role_tier_override(role) if base.name == "claude" else _codex_role_tier_override(role)
    if not tier:
        return base
    return dataclasses.replace(base, model=tier["model"], effort=tier["effort"])


def role_fallback_providers(role: str) -> list[LLMProvider]:
    if os.environ.get("AETHER_LLM_FALLBACK_ENABLED", "1") == "0":
        return []
    providers: list[LLMProvider] = []
    for name in ROLE_FALLBACKS.get(role, ()):
        provider = _provider_for_name(name)
        tier = _codex_role_tier_override(role) if provider.name == "codex" else _role_tier_override(role)
        if tier:
            provider = dataclasses.replace(provider, model=tier["model"], effort=tier["effort"])
        if provider.configured:
            providers.append(provider)
    return providers


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code} {exc.reason}: {body[:500]}") from exc
    return json.loads(raw)


def _extract_openai_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output") or []:
        for part in item.get("content") or []:
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in payload.get("content") or []:
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _short_provider_error(provider: str, error: Exception | str, limit: int = 260) -> str:
    text = str(error).strip()
    try:
        payload = json.loads(text)
        if payload.get("is_error") and payload.get("result"):
            text = str(payload["result"])
        elif payload.get("error"):
            text = str(payload["error"])
    except Exception:
        pass
    text = " ".join(text.split())
    if len(text) > limit:
        text = f"{text[:limit - 3]}..."
    return f"{provider}: {text}"


def call_openai_codex(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 45.0,
    provider: LLMProvider | None = None,
) -> str:
    if not _env_enabled("AETHER_OPENAI_API_FALLBACK_ENABLED"):
        raise RuntimeError("OpenAI API fallback desativado por padrao.")
    key = _env("AETHER_OPENAI_API_KEY", "OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OpenAI/Codex API key nao configurada.")
    provider = _provider_for_name("codex")
    payload = {
        "model": provider.model,
        "instructions": system,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }
    response = _post_json(
        "https://api.openai.com/v1/responses",
        {"Authorization": f"Bearer {key}"},
        payload,
        timeout,
    )
    return _extract_openai_text(response)


def call_openai_codex_result(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 45.0,
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    return {
        "text": call_openai_codex(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout=timeout,
            provider=provider,
        ),
        "metadata": {},
    }


def call_codex_cli_result(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 120.0,
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    cli = shutil.which("codex")
    if not cli:
        raise RuntimeError("Codex CLI nao encontrado.")
    provider = provider or _provider_for_name("codex")
    full_prompt = (
        "Siga as instrucoes do sistema e responda somente o conteudo final solicitado.\n\n"
        f"SISTEMA:\n{system}\n\n"
        f"USUARIO:\n{prompt}"
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
        output_path = handle.name
    try:
        args = [
            cli,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-m",
            provider.model,
            "-o",
            output_path,
            full_prompt,
        ]
        child_env = os.environ.copy()
        child_env.pop("AETHER_OPENAI_API_KEY", None)
        child_env.pop("OPENAI_API_KEY", None)
        completed = subprocess.run(
            args,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            cwd=os.environ.get("AETHER_CODEX_WORKDIR") or os.getcwd(),
            env=child_env,
        )
        output = Path(output_path).read_text(encoding="utf-8").strip()
    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except Exception:
            pass
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(err[:700] or f"Codex CLI saiu com codigo {completed.returncode}.")
    if not output:
        output = (completed.stdout or "").strip()
    return {
        "text": output,
        "metadata": {
            "transport": "codex_cli",
            "stdout_excerpt": (completed.stdout or "")[:1000],
            "stderr_excerpt": (completed.stderr or "")[:1000],
        },
    }


def _claude_api_payload(provider: "LLMProvider", system: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    """Monta payload com prompt caching no system se ativado.

    cache_control:{type:"ephemeral"} marca o bloco do system pra reuso de cache
    entre calls dentro da janela de 5 min. Reduz custo de input em ~70% nas
    chamadas subsequentes com mesmo system. Desliga com
    AETHER_LLM_PROMPT_CACHE_DISABLE=1.
    """
    cache_disabled = _env_enabled("AETHER_LLM_PROMPT_CACHE_DISABLE", "0")
    if cache_disabled or len(system) < 1024:
        return {
            "model": provider.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
    return {
        "model": provider.model,
        "max_tokens": max_tokens,
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": prompt}],
    }


def call_claude_api(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 45.0,
    provider: LLMProvider | None = None,
) -> str:
    key = _env("AETHER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Anthropic/Claude API key nao configurada.")
    provider = provider or _provider_for_name("claude")
    payload = _claude_api_payload(provider, system, prompt, max_tokens)
    response = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        payload,
        timeout,
    )
    return _extract_anthropic_text(response)


def call_claude_code_result(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 120.0,
    provider: LLMProvider | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError("Claude Code CLI nao encontrado. Instale o Claude Code e faca login com sua assinatura Max.")
    provider = provider or _provider_for_name("claude")
    session_key, session_entry = _get_reusable_session(role, provider, system)
    reused_session = bool(session_entry)
    system_path = ""
    if not reused_session:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(system)
            system_path = handle.name
    try:
        args = [
            cli,
            "-p",
            "--output-format",
            "json",
        ]
        if reused_session:
            args.extend(["--resume", str(session_entry["session_id"])])
        else:
            args.extend(["--append-system-prompt-file", system_path])
        if provider.model:
            args.extend(["--model", provider.model])
        if provider.effort:
            args.extend(["--effort", provider.effort])
        child_env = os.environ.copy()
        child_env.pop("AETHER_ANTHROPIC_API_KEY", None)
        child_env.pop("ANTHROPIC_API_KEY", None)
        completed = subprocess.run(
            args,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            cwd=os.environ.get("AETHER_CLAUDE_WORKDIR") or os.getcwd(),
            env=child_env,
        )
    finally:
        if system_path:
            try:
                Path(system_path).unlink(missing_ok=True)
            except Exception:
                pass
    if completed.returncode != 0:
        if reused_session:
            _forget_session(session_key)
        err = (completed.stderr or completed.stdout or "").strip()
        try:
            payload = json.loads(err)
            if payload.get("is_error") and payload.get("result"):
                raise RuntimeError(str(payload["result"]))
        except json.JSONDecodeError:
            pass
        raise RuntimeError(err[:700] or f"Claude Code saiu com codigo {completed.returncode}.")
    raw = (completed.stdout or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw, "metadata": {"raw": raw[:2000]}}
    if payload.get("is_error"):
        if reused_session:
            _forget_session(session_key)
        raise RuntimeError(str(payload.get("result") or raw))
    structured = payload.get("structured_output")
    if structured is not None:
        text = json.dumps(structured, ensure_ascii=False)
    else:
        result = payload.get("result")
        text = result if isinstance(result, str) else raw
    session_id = payload.get("session_id")
    _remember_session(session_key, session_id if isinstance(session_id, str) else None, reused=reused_session)
    return {
        "text": text,
        "metadata": {
            "session_id": session_id,
            "session_reused": reused_session,
            "session_calls": (
                int(session_entry.get("calls") or 0) + 1
                if reused_session and isinstance(session_entry, dict)
                else 1
            ),
            "cost_usd": payload.get("total_cost_usd"),
            "duration_ms": payload.get("duration_ms"),
            "duration_api_ms": payload.get("duration_api_ms"),
            "usage": payload.get("usage"),
            "model_usage": payload.get("modelUsage"),
            "stop_reason": payload.get("stop_reason"),
            "terminal_reason": payload.get("terminal_reason"),
            "effort": provider.effort,
        },
    }


def call_claude_code(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 120.0,
    provider: LLMProvider | None = None,
    role: str | None = None,
) -> str:
    return call_claude_code_result(
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout=timeout,
        provider=provider,
        role=role,
    )["text"]


def call_claude_api_result(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 45.0,
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    key = _env("AETHER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Anthropic/Claude API key nao configurada.")
    provider = provider or _provider_for_name("claude")
    payload = _claude_api_payload(provider, system, prompt, max_tokens)
    response = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        payload,
        timeout,
    )
    return {
        "text": _extract_anthropic_text(response),
        "metadata": {
            "id": response.get("id"),
            "stop_reason": response.get("stop_reason"),
            "usage": response.get("usage"),
        },
    }


def call_claude(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 45.0,
    provider: LLMProvider | None = None,
    role: str | None = None,
) -> str:
    provider = provider or _provider_for_name("claude")
    if provider.transport in {"claude_code", "cli"}:
        return call_claude_code(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout=max(timeout, 120.0),
            provider=provider,
            role=role,
        )
    return call_claude_api(system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout, provider=provider)


def call_claude_result(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 1800,
    timeout: float = 45.0,
    provider: LLMProvider | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    provider = provider or _provider_for_name("claude")
    if provider.transport in {"claude_code", "cli"}:
        return call_claude_code_result(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout=max(timeout, 120.0),
            provider=provider,
            role=role,
        )
    return call_claude_api_result(system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout, provider=provider)


def call_llm_agent(role: str, *, system: str, prompt: str, max_tokens: int = 1800, timeout: float = 45.0) -> dict[str, Any]:
    if os.environ.get("AETHER_LLM_ENABLED", "1") == "0":
        return {"ok": False, "provider": None, "model": None, "text": "", "error": "LLM desativado por AETHER_LLM_ENABLED=0."}
    # Roteia via Context Broker quando AETHER_CONTEXT_BROKER=1.
    # Broker mantem sessao Claude Code unica e injeta snapshot 1x por TTL,
    # economizando tokens ao consolidar todos os papeis em um chat.
    if os.environ.get("AETHER_CONTEXT_BROKER", "0").strip() in {"1", "true", "yes", "on"}:
        try:
            from .context_broker import dispatch as _broker_dispatch, is_enabled as _broker_enabled
            if _broker_enabled():
                return _broker_dispatch(role, system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout)
        except Exception:
            pass  # falha do broker -> fluxo normal
    max_tokens = _budgeted_max_tokens(role, max_tokens)
    provider = role_provider(role)
    providers = [provider, *role_fallback_providers(role)]
    errors: list[str] = []

    for index, current in enumerate(providers):
        if not current.configured:
            if current.name == "claude" and current.transport in {"claude_code", "cli"}:
                errors.append(_short_provider_error(
                    current.name,
                    "Claude Code CLI nao encontrado ou nao autenticado. "
                    "Instale o Claude Code e rode `claude` para fazer login com sua assinatura Max.",
                ))
            else:
                errors.append(_short_provider_error(
                    current.name,
                    f"{current.label} nao configurado. Defina {current.key_env}.",
                ))
            continue

        cb_key = _provider_cb_key(current)
        cb_open, remaining = _cb_is_open(cb_key)
        if cb_open:
            errors.append(_short_provider_error(
                current.name,
                f"circuit breaker aberto, pulando ({int(remaining)}s restantes)",
            ))
            continue

        try:
            if current.name == "claude":
                result = call_claude_result(
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    provider=current,
                    role=role,
                )
            elif current.transport in {"codex_cli", "cli"}:
                result = call_codex_cli_result(
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    timeout=max(timeout, 120.0),
                    provider=current,
                )
            else:
                result = call_openai_codex_result(
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    provider=current,
                )
            _cb_record_success(cb_key)
            metadata = result.get("metadata") or {}
            if index > 0:
                metadata = {
                    **metadata,
                    "fallback": True,
                    "fallback_from": provider.name,
                    "previous_errors": errors,
                }
            return {
                "ok": True,
                "provider": current.name,
                "model": current.model,
                "effort": current.effort,
                "transport": current.transport,
                "text": result["text"],
                "error": None,
                "metadata": metadata,
            }
        except Exception as exc:
            _cb_record_failure(cb_key)
            errors.append(_short_provider_error(current.name, exc))

    return {
        "ok": False,
        "provider": provider.name,
        "model": provider.model,
        "effort": provider.effort,
        "transport": provider.transport,
        "text": "",
        "error": " | ".join(errors) or "Nenhum provedor LLM disponivel.",
        "metadata": {"fallback_attempted": len(providers) > 1},
    }
