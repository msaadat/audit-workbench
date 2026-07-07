"""LLM backend for the natural-language assistant.

Talks to any OpenAI-compatible chat/completions endpoint — Groq by default —
over the standard library only (``urllib``), so there is **no extra runtime
dependency** to bundle into the portable-zip distribution.

The backend is configured through environment variables or a local ``.env``
file, so a deployment can point it at Groq, a self-hosted model, or nothing at
all:

    GROQ_API_KEY   the API key (absence == "assistant not configured")
    GROQ_MODEL     model id (default: a current tool-capable Llama)
    GROQ_BASE_URL  override the endpoint (e.g. a local OpenAI-compatible server)

This module is a thin transport: it knows how to send a chat request (with
optional tool schemas) and hand back the raw assistant message. It never sees
a workspace or a frame — the metadata-only guarantee is enforced one layer up,
in :mod:`.assistant`, which decides what text is allowed into ``messages``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import config  # noqa: F401  # load .env before reading os.environ

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
USER_AGENT = "audit-workbench/0.1"
REQUEST_TIMEOUT = 60  # seconds


class LLMError(RuntimeError):
    """A user-facing problem talking to the LLM (not configured, API error)."""


def _api_key() -> str:
    return (os.environ.get("GROQ_API_KEY") or "").strip()


def _model() -> str:
    return (os.environ.get("GROQ_MODEL") or "").strip() or DEFAULT_MODEL


def _base_url() -> str:
    return ((os.environ.get("GROQ_BASE_URL") or "").strip() or DEFAULT_BASE_URL).rstrip("/")


def is_configured() -> bool:
    return bool(_api_key())


def status() -> dict:
    """What the frontend needs to decide whether to offer the assistant."""
    return {
        "configured": is_configured(),
        "model": _model(),
        "base_url": _base_url(),
    }


def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.0) -> dict:
    """One chat/completions round-trip. Returns the assistant *message* dict.

    The returned message may contain ``content`` and/or ``tool_calls`` — the
    caller drives the tool loop. Raises :class:`LLMError` for any transport or
    API failure, with a message safe to show the user.
    """
    key = _api_key()
    if not key:
        raise LLMError(
            "The assistant is not configured. Set GROQ_API_KEY (and optionally "
            "GROQ_MODEL) in .env or the environment to enable natural-language "
            "analysis."
        )

    body: dict = {
        "model": _model(),
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    request = urllib.request.Request(
        f"{_base_url()}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = _error_detail(error)
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
        message = body.get("error", {}).get("message")
        if message:
            return str(message)
    except json.JSONDecodeError:
        if text.strip():
            return text.strip()
    except Exception:  # pragma: no cover - best-effort error extraction
        pass
    return error.reason or "unknown error"
