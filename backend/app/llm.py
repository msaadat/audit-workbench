"""LLM transport for the natural-language assistant.

Talks to OpenAI-compatible chat/completions endpoints over the standard library
only (``urllib``), so there is no extra runtime dependency to bundle into the
portable-zip distribution.

Only API keys live in environment variables or a local ``.env`` file. The
provider and model are normal application settings saved through the UI:

    GROQ_API_KEY             Groq API key
    OPENROUTER_API_KEY       OpenRouter API key
    MISTRAL_API_KEY          Mistral API key
    OPENCODE_API_KEY         OpenCode Zen API key
    CEREBRAS_API_KEY         Cerebras API key
    LMSTUDIO_API_KEY         optional local dummy key
    LLM_REQUEST_TIMEOUT      optional request timeout in seconds
    AGENT_PROVIDER           optional provider override for agent runs
    AGENT_MODEL              optional model override for agent runs

This module is a thin transport: it knows how to send a chat request (with
optional tool schemas) and hand back the raw assistant message. It never sees
a workspace or a frame; callers assemble bounded model context one layer up.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import base64
import time
import urllib.error
import urllib.request

from . import assistant_settings
from . import config  # noqa: F401  # load .env before reading os.environ
from . import debug_store

DEFAULT_LMSTUDIO_API_KEY = "lm-studio"
USER_AGENT = "audit-workbench/0.1"
REQUEST_TIMEOUT = 60  # seconds
LOCAL_REQUEST_TIMEOUT = 300  # seconds
MAX_REQUEST_ATTEMPTS = 3
MAX_RETRY_DELAY = 2.0
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


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
    call_started = time.monotonic()
    try:
        settings = _settings(profile)
    except Exception as error:
        # Invalid non-secret configuration is itself useful telemetry. Use a
        # minimal settings-shaped object so the failed call is still durable.
        from types import SimpleNamespace
        fallback = SimpleNamespace(backend=None, model=None, base_url="", timeout=None)
        call_id, _ = debug_store.start_call(
            {"messages": messages, "tools": tools, "temperature": temperature},
            fallback, extra={"profile": profile},
        )
        context = debug_store.current_context()
        if call_id:
            debug_store.finish_call(
                str(context["workspace_id"]), call_id, error=str(error),
                started_monotonic=call_started,
            )
        raise

    body: dict = {
        "model": settings.model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    call_id, _ = debug_store.start_call(body, settings, extra={"profile": profile})
    trace = debug_store.current_context()
    trace_workspace_id = str(trace.get("workspace_id") or "")

    if not settings.api_key:
        provider = assistant_settings.PROVIDERS[settings.backend]
        if settings.backend == "lmstudio":
            hint = "Start LM Studio's local server"
        else:
            hint = f"Set {provider['api_key_env']}"
        error = LLMError(
            f"The assistant is not configured for {settings.backend}. {hint} "
            "in .env or the environment; choose provider/model in Assistant settings."
        )
        if call_id:
            debug_store.finish_call(
                trace_workspace_id, call_id, error=str(error), started_monotonic=call_started,
            )
        raise error

    request_bytes = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.base_url}/chat/completions",
        data=request_bytes,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **settings.extra_headers,
        },
        method="POST",
    )
    if call_id:
        def actual_request_metrics(record: dict) -> None:
            record["request_size_bytes"] = len(request_bytes)
            record["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
        debug_store.update_call(trace_workspace_id, call_id, actual_request_metrics)

    for attempt in range(MAX_REQUEST_ATTEMPTS):
        attempt_started_wall = debug_store.utcnow()
        attempt_started = time.monotonic()
        attempt_record: dict = {"number": attempt + 1, "started_at": attempt_started_wall}
        payload = None
        response_headers = None
        raw_body = b""
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout) as response:
                headers_at = time.monotonic()
                response_headers = getattr(response, "headers", {})
                raw_body = response.read()
                body_at = time.monotonic()
                attempt_record.update({
                    "http_status": getattr(response, "status", 200),
                    "response_headers": debug_store.safe_headers(response_headers),
                    "time_to_headers_ms": round((headers_at - attempt_started) * 1000, 3),
                    "body_read_ms": round((body_at - headers_at) * 1000, 3),
                    "response_size_bytes": len(raw_body),
                    "response_sha256": hashlib.sha256(raw_body).hexdigest(),
                })
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                finally:
                    parsed_at = time.monotonic()
                    attempt_record["parse_ms"] = round((parsed_at - body_at) * 1000, 3)
        except urllib.error.HTTPError as error:
            raw_error, error_payload, detail = _read_http_error(error)
            hint = _http_error_hint(settings, error)
            if hint:
                detail = f"{detail} {hint}"
            attempt_record.update({
                "http_status": error.code,
                "response_headers": debug_store.safe_headers(error.headers),
                "error": detail,
                "error_response": debug_store.sanitize(error_payload),
                "response_size_bytes": len(raw_error),
                "response_sha256": hashlib.sha256(raw_error).hexdigest(),
            })
            if error.code in RETRYABLE_HTTP_CODES and attempt + 1 < MAX_REQUEST_ATTEMPTS:
                delay = _wait_before_retry(
                    attempt, error.headers.get("Retry-After") if error.headers else None
                )
                attempt_record["retry_delay_ms"] = round(delay * 1000, 3)
                _finish_attempt(attempt_record, attempt_started)
                if call_id: debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                continue
            terminal = LLMError(f"LLM request failed ({error.code}): {detail}")
            _finish_attempt(attempt_record, attempt_started)
            if call_id:
                debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                debug_store.finish_call(trace_workspace_id, call_id, payload=error_payload,
                                        error=str(terminal), headers=error.headers,
                                        started_monotonic=call_started)
            raise terminal from error
        except urllib.error.URLError as error:
            attempt_record["error"] = f"{error.reason}"
            if attempt + 1 < MAX_REQUEST_ATTEMPTS:
                delay = _wait_before_retry(attempt)
                attempt_record["retry_delay_ms"] = round(delay * 1000, 3)
                _finish_attempt(attempt_record, attempt_started)
                if call_id: debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                continue
            terminal = LLMError(f"Could not reach the LLM endpoint: {error.reason}")
            _finish_attempt(attempt_record, attempt_started)
            if call_id:
                debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                debug_store.finish_call(trace_workspace_id, call_id, error=str(terminal), started_monotonic=call_started)
            raise terminal from error
        # ``urllib`` normally wraps socket failures in URLError, but
        # http.client.RemoteDisconnected and related connection failures can
        # escape directly when an upstream closes before sending a response.
        except (TimeoutError, ConnectionError, json.JSONDecodeError) as error:
            attempt_record["error"] = str(error)
            if isinstance(error, json.JSONDecodeError) and raw_body:
                attempt_record["error_response"] = debug_store.sanitize(
                    {"unparseable_body": raw_body.decode("utf-8", errors="replace")}
                )
            if attempt + 1 < MAX_REQUEST_ATTEMPTS:
                delay = _wait_before_retry(attempt)
                attempt_record["retry_delay_ms"] = round(delay * 1000, 3)
                _finish_attempt(attempt_record, attempt_started)
                if call_id: debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                continue
            terminal = LLMError(f"LLM request failed: {error}")
            _finish_attempt(attempt_record, attempt_started)
            if call_id:
                debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                debug_store.finish_call(trace_workspace_id, call_id, payload=payload or attempt_record.get("error_response"),
                                        error=str(terminal), headers=response_headers,
                                        started_monotonic=call_started)
            raise terminal from error

        _finish_attempt(attempt_record, attempt_started)
        if call_id: debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
        choices = payload.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            if call_id:
                debug_store.finish_call(trace_workspace_id, call_id, payload=payload,
                                        message=message, headers=response_headers,
                                        started_monotonic=call_started)
            return message
        if attempt + 1 < MAX_REQUEST_ATTEMPTS:
            delay = _wait_before_retry(attempt)
            if call_id:
                def add_delay(record: dict) -> None:
                    record["attempts"][-1]["retry_delay_ms"] = round(delay * 1000, 3)
                debug_store.update_call(trace_workspace_id, call_id, add_delay)
            continue
        terminal = LLMError(f"LLM returned no choices after {MAX_REQUEST_ATTEMPTS} attempts.")
        if call_id:
            debug_store.finish_call(trace_workspace_id, call_id, payload=payload,
                                    error=str(terminal), headers=response_headers,
                                    started_monotonic=call_started)
        raise terminal
    raise LLMError("LLM request failed after retrying.")


def _finish_attempt(attempt: dict, started: float) -> None:
    attempt["finished_at"] = debug_store.utcnow()
    attempt["duration_ms"] = max(0, round((time.monotonic() - started) * 1000, 3))


def _wait_before_retry(attempt: int, retry_after: str | None = None) -> float:
    delay = min(0.25 * (2 ** attempt), MAX_RETRY_DELAY)
    if retry_after:
        try:
            delay = min(max(delay, float(retry_after)), MAX_RETRY_DELAY)
        except ValueError:
            pass
    time.sleep(delay)
    return delay


def _read_http_error(error: urllib.error.HTTPError) -> tuple[bytes, object, str]:
    try:
        raw = error.read()
    except Exception:
        raw = b""
    text = raw.decode("utf-8", errors="replace")
    try:
        body: object = json.loads(text) if text else {}
    except json.JSONDecodeError:
        body = {"text": text}
    error_body = body.get("error", {}) if isinstance(body, dict) else {}
    message = error_body.get("message") if isinstance(error_body, dict) else None
    error_type = ((error_body.get("metadata") or {}).get("error_type")
                  if isinstance(error_body, dict) else None)
    detail = f"{message} ({error_type})" if message and error_type else str(message or text.strip() or error.reason or "unknown error")
    return raw, body, detail


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
