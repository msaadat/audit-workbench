"""Persisted assistant provider/model settings.

Secrets stay in ``.env`` or the process environment. This file stores only the
normal, non-secret choices the user can edit from the UI: provider and model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config  # noqa: F401  # load .env before reading WORKBENCH_DATA

PROVIDERS = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "local": False,
        "vision": True,
        "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "~openai/gpt-latest",
        "models": [
            "~openai/gpt-latest",
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
        ],
        "local": False,
        "vision": True,
        "vision_model": "openai/gpt-4o-mini",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "default_model": "mistral-medium-3-5",
        "models": ["mistral-large-latest", "mistral-medium-3-5", "mistral-small-latest"],
        "local": False,
        "vision": True,
        "vision_model": "pixtral-large-latest",
    },
    "lmstudio": {
        "label": "LM Studio",
        "base_url": "http://localhost:1234/v1",
        "api_key_env": "LMSTUDIO_API_KEY",
        "default_model": "",
        "models": [""],
        "local": True,
        "vision": True,
        "vision_model": "",
    },
}

FALLBACK_PROVIDER = "mistral"


def _default_provider() -> str:
    provider = (os.environ.get("LLM_BACKEND") or FALLBACK_PROVIDER).strip().lower()
    return provider if provider in PROVIDERS else FALLBACK_PROVIDER


DEFAULT_PROVIDER = _default_provider()
DEFAULT_SETTINGS = {
    "provider": DEFAULT_PROVIDER,
    "model": PROVIDERS[DEFAULT_PROVIDER]["default_model"],
}
SETTINGS_PATH = Path(
    os.environ.get("WORKBENCH_SETTINGS", "")
    or Path(
        os.environ.get("WORKBENCH_DATA", "")
        or Path(__file__).resolve().parents[2] / "Workspaces"
    )
    / "settings.json"
)


class SettingsError(ValueError):
    """A user-facing settings problem."""


def _clean_provider(value: object) -> str:
    provider = str(value or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        supported = ", ".join(PROVIDERS)
        raise SettingsError(
            f"Unsupported assistant provider '{provider}'. Use one of: {supported}."
        )
    return provider


def _default_model(provider: str) -> str:
    return str(PROVIDERS[provider]["default_model"])


def _env_model(provider: str) -> str:
    return (os.environ.get(f"{provider.upper()}_MODEL") or "").strip()


def load() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(stored.get("assistant") or stored)
        except (OSError, json.JSONDecodeError) as error:
            raise SettingsError(f"Could not read assistant settings: {error}") from error

    provider = _clean_provider(settings.get("provider"))
    model = _env_model(provider) or str(settings.get("model") or "").strip()
    if not model:
        model = _default_model(provider)
    return {"provider": provider, "model": model}


def save(changes: dict) -> dict:
    current = load()
    provider = _clean_provider(changes.get("provider", current["provider"]))
    model = str(changes.get("model", current["model"]) or "").strip()
    if not model:
        model = _default_model(provider)
    current = {"provider": provider, "model": model}

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps({"assistant": current}, indent=2),
        encoding="utf-8",
    )
    return current


def provider_options() -> list[dict]:
    options = []
    for provider, meta in PROVIDERS.items():
        api_key = str(meta["api_key_env"])
        options.append(
            {
                "id": provider,
                "label": meta["label"],
                "base_url": meta["base_url"],
                "api_key_env": api_key,
                "api_key_configured": bool((os.environ.get(api_key) or "").strip())
                or bool(meta["local"]),
                "default_model": meta["default_model"],
                "models": meta["models"],
                "local": meta["local"],
                "vision": bool(meta.get("vision")),
                "vision_model": meta.get("vision_model") or "",
            }
        )
    return options
