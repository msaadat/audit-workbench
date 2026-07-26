"""Persisted assistant and agent model-profile settings.

Secrets stay in ``.env`` or the process environment. This file stores only the
normal, non-secret choices the user can edit from the UI: provider, model, and
declared capabilities for custom agent-vision profiles.
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
        "model_capabilities": {
            "llama-3.3-70b-versatile": [],
            "llama-3.1-8b-instant": [],
            "meta-llama/llama-4-scout-17b-16e-instruct": ["vision"],
        },
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
        "model_capabilities": {
            "~openai/gpt-latest": ["vision"],
            "openai/gpt-4o-mini": ["vision"],
            "anthropic/claude-3.5-sonnet": ["vision"],
        },
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
        "model_capabilities": {
            "mistral-large-latest": [],
            "mistral-medium-3-5": [],
            "mistral-small-latest": [],
            "pixtral-large-latest": ["vision"],
        },
    },
    "opencode": {
        "label": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "OPENCODE_API_KEY",
        "default_model": "deepseek-v4-flash-free",
        "models": ["deepseek-v4-flash-free"],
        "local": False,
        "vision": False,
        "model_capabilities": {"deepseek-v4-flash-free": []},
    },
    "cerebras": {
        "label": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "default_model": "gpt-oss-120b",
        "models": ["gpt-oss-120b"],
        "local": False,
        "vision": False,
        "model_capabilities": {"gpt-oss-120b": []},
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
        # LM Studio accepts arbitrary local model identifiers. Capabilities are
        # therefore never inferred from this catalog and must be declared in the
        # persisted agent-vision profile or environment.
        "model_capabilities": {},
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


def _stored_document() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError(f"Could not read assistant settings: {error}") from error
    return stored if isinstance(stored, dict) else {}


def load_agent_vision_profile() -> dict | None:
    """Return the optional non-secret custom vision profile declaration."""

    stored = _stored_document()
    value = stored.get("agent_vision")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SettingsError("Agent vision profile must be an object.")
    provider = _clean_provider(value.get("provider"))
    model = str(value.get("model") or "").strip()
    if not model:
        raise SettingsError("Agent vision profile model is required.")
    raw_capabilities = value.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise SettingsError("Agent vision profile capabilities must be an array.")
    capabilities = sorted(
        {str(item or "").strip().lower() for item in raw_capabilities if str(item or "").strip()}
    )
    unknown = [item for item in capabilities if item not in {"vision"}]
    if unknown:
        raise SettingsError(f"Unknown model capability '{unknown[0]}'.")
    if "vision" not in capabilities:
        raise SettingsError(
            "A custom agent vision profile must explicitly declare the vision capability."
        )
    return {
        "provider": provider,
        "model": model,
        "capabilities": capabilities,
    }


def save(changes: dict) -> dict:
    current = load()
    provider = _clean_provider(changes.get("provider", current["provider"]))
    model = str(changes.get("model", current["model"]) or "").strip()
    if not model:
        model = _default_model(provider)
    current = {"provider": provider, "model": model}

    stored = _stored_document()
    stored["assistant"] = current
    if "vision_profile" in changes or "agent_vision" in changes:
        profile = changes.get("vision_profile", changes.get("agent_vision"))
        if profile is None:
            stored.pop("agent_vision", None)
        elif not isinstance(profile, dict):
            raise SettingsError("Agent vision profile must be an object.")
        else:
            # Validate the exact shape through the same reader contract before
            # persisting it as the authoritative custom declaration.
            provider_value = _clean_provider(profile.get("provider"))
            model_value = str(profile.get("model") or "").strip()
            capabilities_value = profile.get("capabilities")
            if not model_value:
                raise SettingsError("Agent vision profile model is required.")
            if not isinstance(capabilities_value, list):
                raise SettingsError(
                    "Agent vision profile capabilities must be an array."
                )
            capabilities = sorted(
                {
                    str(item or "").strip().lower()
                    for item in capabilities_value
                    if str(item or "").strip()
                }
            )
            if capabilities != ["vision"]:
                raise SettingsError(
                    "A custom agent vision profile must explicitly declare only "
                    "the supported vision capability."
                )
            stored["agent_vision"] = {
                "provider": provider_value,
                "model": model_value,
                "capabilities": capabilities,
            }
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(stored, indent=2),
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
                "model_capabilities": dict(meta.get("model_capabilities") or {}),
            }
        )
    return options
