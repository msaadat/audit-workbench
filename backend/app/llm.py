"""LLM transport for the natural-language assistant.

Talks to OpenAI-compatible chat/completions endpoints over the standard library
only (``urllib``), so there is no extra runtime dependency to bundle into the
portable-zip distribution.

Only API keys live in environment variables or a local ``.env`` file. The
provider and model are normal application settings saved through the UI:

    GROQ_API_KEY             Groq API key
    OPENROUTER_API_KEY       OpenRouter API key
    MISTRAL_API_KEY          Mistral API key
    LMSTUDIO_API_KEY         optional local dummy key
    LLM_REQUEST_TIMEOUT      optional request timeout in seconds
    AGENT_PROVIDER           optional provider override for agent runs
    AGENT_MODEL              optional model override for agent runs

This module is a thin transport: it knows how to send a chat request (with
optional tool schemas) and hand back the raw assistant message. It never sees
a workspace or a frame; the metadata-only guarantee is enforced one layer up,
in :mod:`.assistant`, which decides what text is allowed into ``messages``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import base64
import urllib.error
import urllib.request

from . import assistant_settings
from . import config  # noqa: F401  # load .env before reading os.environ

DEFAULT_LMSTUDIO_API_KEY = "lm-studio"
USER_AGENT = "audit-workbench/0.1"
REQUEST_TIMEOUT = 60  # seconds
LOCAL_REQUEST_TIMEOUT = 300  # seconds


class LLMError(RuntimeError):
    """A user-facing problem talking to the LLM (not configured, API error)."""


@dataclass(frozen=True)
class LLMSettings:
    backend: str
    api_key: str
    model: str
    base_url: str
    extra_headers: dict[str, str]
    timeout: int


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _request_timeout(default: int) -> int:
    configured = _env("LLM_REQUEST_TIMEOUT")
    if not configured:
        return default
    try:
        timeout = int(configured)
    except ValueError as error:
        raise LLMError("LLM_REQUEST_TIMEOUT must be a positive integer.") from error
    if timeout <= 0:
        raise LLMError("LLM_REQUEST_TIMEOUT must be a positive integer.")
    return timeout


def _settings(profile: str = "assistant") -> LLMSettings:
    saved = assistant_settings.load()
    backend = saved["provider"]
    model = saved["model"]
    if profile in ("agent", "vision"):
        # Agent runs may need a stronger model than the chat assistant.
        # AGENT_PROVIDER/AGENT_BACKEND and AGENT_MODEL override when set;
        # otherwise the agent uses the assistant's configured backend/model.
        prefix = "AGENT_VISION" if profile == "vision" else "AGENT"
        override_backend = (_env(f"{prefix}_PROVIDER") or _env(f"{prefix}_BACKEND")).lower()
        override_model = _env(f"{prefix}_MODEL")
        if override_backend:
            if override_backend not in assistant_settings.PROVIDERS:
                supported = ", ".join(assistant_settings.PROVIDERS)
                raise LLMError(
                    f"Unsupported {prefix}_PROVIDER '{override_backend}'. "
                    f"Use one of: {supported}."
                )
            backend = override_backend
            provider_meta = assistant_settings.PROVIDERS[backend]
            model = override_model or str(
                (provider_meta.get("vision_model") or provider_meta["default_model"])
                if profile == "vision" else provider_meta["default_model"]
            )
        elif override_model:
            model = override_model
        elif profile == "vision":
            model = str(assistant_settings.PROVIDERS[backend].get("vision_model") or model)
    provider = assistant_settings.PROVIDERS[backend]
    api_key_env = str(provider["api_key_env"])
    api_key = _env(api_key_env)
    if backend == "lmstudio" and not api_key:
        api_key = DEFAULT_LMSTUDIO_API_KEY
    return LLMSettings(
        backend=backend,
        api_key=api_key,
        model=model,
        base_url=str(provider["base_url"]).rstrip("/"),
        extra_headers={},
        timeout=_request_timeout(
            LOCAL_REQUEST_TIMEOUT if provider["local"] else REQUEST_TIMEOUT
        ),
    )


def is_configured() -> bool:
    try:
        return bool(_settings().api_key)
    except (LLMError, assistant_settings.SettingsError):
        return False


def status() -> dict:
    """What the frontend needs to decide whether to offer the assistant."""
    try:
        settings = _settings()
    except (LLMError, assistant_settings.SettingsError) as error:
        return {
            "configured": False,
            "vision_configured": False,
            "backend": assistant_settings.DEFAULT_PROVIDER,
            "provider": assistant_settings.DEFAULT_PROVIDER,
            "model": "",
            "base_url": "",
            "providers": assistant_settings.provider_options(),
            "error": str(error),
        }
    return {
        "configured": bool(settings.api_key),
        "backend": settings.backend,
        "provider": settings.backend,
        "model": settings.model,
        "base_url": settings.base_url,
        "providers": assistant_settings.provider_options(),
    }


def update_settings(payload: dict) -> dict:
    assistant_settings.save(payload)
    return status()


def agent_status() -> dict:
    """Like :func:`status`, but for the agent profile (env overrides applied)."""
    try:
        settings = _settings("agent")
    except (LLMError, assistant_settings.SettingsError) as error:
        return {
            "configured": False,
            "vision_configured": False,
            "backend": "",
            "provider": "",
            "model": "",
            "base_url": "",
            "error": str(error),
        }
    result = {
        "configured": bool(settings.api_key),
        "backend": settings.backend,
        "provider": settings.backend,
        "model": settings.model,
        "base_url": settings.base_url,
    }
    try:
        vision = _settings("vision")
        result["vision_configured"] = bool(
            vision.api_key and assistant_settings.PROVIDERS[vision.backend].get("vision")
        )
        result["vision_provider"] = vision.backend
        result["vision_model"] = vision.model
    except (LLMError, assistant_settings.SettingsError):
        result["vision_configured"] = False
    return result


def image_part(content: bytes, mime: str) -> dict:
    """Build an OpenAI-compatible inline image content part."""
    if not str(mime or "").startswith("image/"):
        raise LLMError("Vision content must use an image MIME type.")
    encoded = base64.b64encode(content).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.0, profile: str = "assistant") -> dict:
    """One chat/completions round-trip. Returns the assistant message dict.

    The returned message may contain ``content`` and/or ``tool_calls``; the
    caller drives the tool loop. Raises :class:`LLMError` for any transport or
    API failure, with a message safe to show the user. ``profile`` selects the
    backend/model pair: 'assistant' (saved settings), 'agent' (agent overrides),
    or 'vision' (vision-capable profile overrides).
    """
    settings = _settings(profile)
    if not settings.api_key:
        provider = assistant_settings.PROVIDERS[settings.backend]
        if settings.backend == "lmstudio":
            hint = "Start LM Studio's local server"
        else:
            hint = f"Set {provider['api_key_env']}"
        raise LLMError(
            f"The assistant is not configured for {settings.backend}. {hint} "
            "in .env or the environment; choose provider/model in Assistant settings."
        )

    body: dict = {
        "model": settings.model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    request = urllib.request.Request(
        f"{settings.base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **settings.extra_headers,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = _error_detail(error)
        hint = _http_error_hint(settings, error)
        if hint:
            detail = f"{detail} {hint}"
        raise LLMError(f"LLM request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise LLMError(f"Could not reach the LLM endpoint: {error.reason}") from error
    except (TimeoutError, json.JSONDecodeError) as error:
        raise LLMError(f"LLM request failed: {error}") from error

    choices = payload.get("choices") or []
    if not choices:
        raise LLMError("LLM returned no choices.")
    return choices[0].get("message") or {}


def _error_detail(error: urllib.error.HTTPError) -> str:
    try:
        text = error.read().decode("utf-8")
        body = json.loads(text)
        error_body = body.get("error", {})
        message = error_body.get("message")
        error_type = (error_body.get("metadata") or {}).get("error_type")
        if message:
            if error_type:
                return f"{message} ({error_type})"
            return str(message)
    except json.JSONDecodeError:
        if text.strip():
            return text.strip()
    except Exception:  # pragma: no cover - best-effort error extraction
        pass
    return error.reason or "unknown error"


def _http_error_hint(settings: LLMSettings, error: urllib.error.HTTPError) -> str:
    if settings.backend == "lmstudio" and error.code in {400, 404, 503}:
        return (
            f"LM Studio is running at {settings.base_url}, but model "
            f"'{settings.model or '<blank>'}' was not accepted. Choose the "
            "model identifier shown by LM Studio in Assistant settings, or "
            "load the model in the local server."
        )

    if settings.backend != "openrouter":
        return ""

    if error.code == 429:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        retry = f" Retry after {retry_after} seconds." if retry_after else ""
        return (
            f"OpenRouter is rate limiting model '{settings.model}'.{retry} "
            "Try again later, choose a different model in Assistant settings, "
            "or check the key's credits/rate limits at "
            "https://openrouter.ai/settings/keys."
        )
    if error.code == 402:
        return (
            "OpenRouter reports insufficient credits for this key. Add credits "
            "or use a key/model with available quota."
        )
    if error.code == 503:
        return (
            f"OpenRouter could not find an available provider for '{settings.model}'. "
            "Try again later or choose a different model in Assistant settings."
        )
    return ""
