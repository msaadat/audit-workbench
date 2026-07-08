"""LLM backends for the natural-language assistant.

Talks to OpenAI-compatible chat/completions endpoints over the standard library
only (``urllib``), so there is no extra runtime dependency to bundle into the
portable-zip distribution.

The backend is configured through environment variables or a local ``.env``
file. Groq remains the default for backward compatibility; OpenRouter is also
supported, and LM Studio can be used as a local backend:

    LLM_BACKEND              optional: groq, openrouter, or lmstudio
    GROQ_API_KEY             Groq API key
    GROQ_MODEL               Groq model id
    GROQ_BASE_URL            Groq/OpenAI-compatible endpoint override
    OPENROUTER_API_KEY       OpenRouter API key
    OPENROUTER_MODEL         OpenRouter model slug
    OPENROUTER_BASE_URL      OpenRouter endpoint override
    OPENROUTER_HTTP_REFERER  optional attribution header
    OPENROUTER_APP_TITLE     optional attribution header
    LMSTUDIO_MODEL           optional LM Studio model id
    LMSTUDIO_BASE_URL        LM Studio local server URL
    LMSTUDIO_API_KEY         optional local dummy key

This module is a thin transport: it knows how to send a chat request (with
optional tool schemas) and hand back the raw assistant message. It never sees
a workspace or a frame; the metadata-only guarantee is enforced one layer up,
in :mod:`.assistant`, which decides what text is allowed into ``messages``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.request

from . import config  # noqa: F401  # load .env before reading os.environ

DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "~openai/gpt-latest"
DEFAULT_LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LMSTUDIO_API_KEY = "lm-studio"
USER_AGENT = "audit-workbench/0.1"
REQUEST_TIMEOUT = 60  # seconds


class LLMError(RuntimeError):
    """A user-facing problem talking to the LLM (not configured, API error)."""


@dataclass(frozen=True)
class LLMSettings:
    backend: str
    api_key: str
    model: str
    base_url: str
    extra_headers: dict[str, str]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _backend() -> str:
    configured = (_env("LLM_BACKEND") or _env("LLM_PROVIDER")).lower()
    if configured:
        return configured
    if _env("OPENROUTER_API_KEY") and not _env("GROQ_API_KEY"):
        return "openrouter"
    return "groq"


def _settings() -> LLMSettings:
    backend = _backend()
    if backend == "openrouter":
        title = _env("OPENROUTER_APP_TITLE") or "Audit Workbench"
        referer = _env("OPENROUTER_HTTP_REFERER") or _env("OPENROUTER_SITE_URL")
        headers = {"X-OpenRouter-Title": title}
        if referer:
            headers["HTTP-Referer"] = referer
        return LLMSettings(
            backend=backend,
            api_key=_env("OPENROUTER_API_KEY"),
            model=_env("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL,
            base_url=(_env("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL).rstrip("/"),
            extra_headers=headers,
        )
    if backend == "groq":
        return LLMSettings(
            backend=backend,
            api_key=_env("GROQ_API_KEY"),
            model=_env("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
            base_url=(_env("GROQ_BASE_URL") or DEFAULT_GROQ_BASE_URL).rstrip("/"),
            extra_headers={},
        )
    if backend == "lmstudio":
        return LLMSettings(
            backend=backend,
            api_key=_env("LMSTUDIO_API_KEY") or DEFAULT_LMSTUDIO_API_KEY,
            model=_env("LMSTUDIO_MODEL"),
            base_url=(_env("LMSTUDIO_BASE_URL") or DEFAULT_LMSTUDIO_BASE_URL).rstrip("/"),
            extra_headers={},
        )
    raise LLMError("Unsupported LLM_BACKEND. Use 'groq', 'openrouter', or 'lmstudio'.")


def is_configured() -> bool:
    try:
        return bool(_settings().api_key)
    except LLMError:
        return False


def status() -> dict:
    """What the frontend needs to decide whether to offer the assistant."""
    try:
        settings = _settings()
    except LLMError as error:
        return {
            "configured": False,
            "backend": _backend(),
            "model": "",
            "base_url": "",
            "error": str(error),
        }
    return {
        "configured": bool(settings.api_key),
        "backend": settings.backend,
        "model": settings.model,
        "base_url": settings.base_url,
    }


def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.0) -> dict:
    """One chat/completions round-trip. Returns the assistant message dict.

    The returned message may contain ``content`` and/or ``tool_calls``; the
    caller drives the tool loop. Raises :class:`LLMError` for any transport or
    API failure, with a message safe to show the user.
    """
    settings = _settings()
    if not settings.api_key:
        if settings.backend == "openrouter":
            hint = "Set OPENROUTER_API_KEY (and optionally OPENROUTER_MODEL)"
        elif settings.backend == "lmstudio":
            hint = "Start LM Studio's local server and optionally set LMSTUDIO_MODEL"
        else:
            hint = "Set GROQ_API_KEY (and optionally GROQ_MODEL)"
        raise LLMError(
            f"The assistant is not configured for {settings.backend}. {hint} "
            "in .env or the environment to enable natural-language analysis."
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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
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
    if settings.backend != "openrouter":
        if settings.backend == "lmstudio" and error.code in {400, 404, 503}:
            return (
                f"LM Studio is running at {settings.base_url}, but model "
                f"'{settings.model or '<blank>'}' was not accepted. Set LMSTUDIO_MODEL to the "
                "model identifier shown by LM Studio, or load the model in the "
                "local server."
            )
        return ""

    if error.code == 429:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        retry = f" Retry after {retry_after} seconds." if retry_after else ""
        return (
            f"OpenRouter is rate limiting model '{settings.model}'.{retry} "
            "Try again later, choose a different OPENROUTER_MODEL, or check the "
            "key's credits/rate limits at https://openrouter.ai/settings/keys."
        )
    if error.code == 402:
        return (
            "OpenRouter reports insufficient credits for this key. Add credits "
            "or use a key/model with available quota."
        )
    if error.code == 503:
        return (
            f"OpenRouter could not find an available provider for '{settings.model}'. "
            "Try again later or choose a different OPENROUTER_MODEL."
        )
    return ""
