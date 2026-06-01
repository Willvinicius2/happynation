from __future__ import annotations

import re


_PLACEHOLDER_HINTS = (
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
    "sua_secret",
    "api_key",
    "secret_key",
)


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'")


def looks_like_real_binance_credentials(api_key: str | None, secret_key: str | None) -> bool:
    """Return True only for plausible Binance credentials."""

    api = _clean(api_key)
    secret = _clean(secret_key)
    if not api or not secret:
        return False

    joined = f"{api} {secret}".lower()
    if any(hint in joined for hint in _PLACEHOLDER_HINTS):
        return False

    pattern = re.compile(r"^[A-Za-z0-9_-]{30,}$")
    return bool(pattern.match(api) and pattern.match(secret))
