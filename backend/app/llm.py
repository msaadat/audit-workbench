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
    LLM_RATE_LIMIT_COOLDOWN  optional minimum cooldown after a HTTP 429,
                             in seconds (defaults to 60)
    AGENT_PROVIDER           optional provider override for agent runs
    AGENT_MODEL              optional model override for agent runs

This module is a thin transport: it knows how to send a chat request (with
optional tool schemas) and hand back the raw assistant message. It never sees
a workspace or a frame; callers assemble bounded model context one layer up.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
import base64
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from . import assistant_settings
from . import config  # noqa: F401  # load .env before reading os.environ
from . import debug_store

DEFAULT_LMSTUDIO_API_KEY = "lm-studio"
USER_AGENT = "audit-workbench/0.1"
REQUEST_TIMEOUT = 60  # seconds
LOCAL_REQUEST_TIMEOUT = 300  # seconds
# The output ceiling asked of every provider. Sent to raise a floor, not to
# impose one: left unset, the limit is whatever the routed provider happens to
# default to, and for one model in use those defaults span 32,768 to 1,179,648
# — so an answer's room depends on routing luck rather than on anything this
# code decided. It is deliberately far above any legitimate output measured
# here (the largest across 446 calls was 7,293 tokens, and the largest matrix
# 6,389), because a ceiling that can truncate real work costs an entire run,
# while a degenerate call that runs to this limit costs a few cents. Loops and
# runaways are caught by the empty-completion check in the model gateway, which
# can tell them from a long answer; this number cannot, and is not asked to.
MAX_OUTPUT_TOKENS = 131_072
MAX_REQUEST_ATTEMPTS = 3
MAX_RETRY_DELAY = 2.0
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0  # seconds
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
# Symbolic equivalents of the codes above, as used by providers that report an
# upstream failure inside an HTTP 200 body rather than as an HTTP status. These
# name the condition, so they are trusted without reading the message.
RETRYABLE_PROVIDER_CODES = frozenset(
    {
        "internal_server_error",
        "overloaded",
        "overloaded_error",
        "rate_limit_exceeded",
        "resource_exhausted",
        "server_error",
        "service_unavailable",
        "timeout",
        "unavailable",
    }
)
# An in-band numeric code is ambiguous — an aggregator returns 502 both for a
# saturated upstream and for a request the upstream rejected outright — so the
# message has to corroborate it. These are the phrases that describe something
# that may clear on its own; a schema or validation complaint will not match.
TRANSIENT_PROVIDER_MARKERS = (
    "capacity",
    "connection reset",
    "internal server error",
    "overload",
    "please retry",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "request limit reached",
    "resource exhausted",
    "resourceexhausted",
    "temporarily",
    "timed out",
    "timeout",
    "too many requests",
    "try again",
    "unavailable",
)


# A rate limit is shared by the provider account, not by an individual
# workflow unit. Keep one process-wide gate here so every LLM entry point
# (assistant, report generation, and agent workflows) observes it before
# opening a connection.
_rate_limit_gate_lock = threading.Lock()
_rate_limit_not_before = 0.0


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
    profile_name: str = "assistant"
    capabilities: tuple[str, ...] = ()
    profile_hash: str = ""
    configuration_source: str = "assistant_settings"


@dataclass(frozen=True)
class ResolvedModelProfile:
    """Non-secret, hash-identified model selection used by durable runs."""

    name: str
    provider: str
    model: str
    capabilities: tuple[str, ...]
    configuration_source: str
    configured: bool
    base_url: str
    profile_hash: str
    unavailability_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "configuration_source": self.configuration_source,
            "configured": self.configured,
            "base_url": self.base_url,
            "profile_hash": self.profile_hash,
            "unavailability_reason": self.unavailability_reason,
        }


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


def _max_output_tokens() -> int:
    configured = _env("LLM_MAX_OUTPUT_TOKENS")
    if not configured:
        return MAX_OUTPUT_TOKENS
    try:
        limit = int(configured)
    except ValueError as error:
        raise LLMError("LLM_MAX_OUTPUT_TOKENS must be a positive integer.") from error
    if limit <= 0:
        raise LLMError("LLM_MAX_OUTPUT_TOKENS must be a positive integer.")
    return limit


def _rate_limit_cooldown() -> float:
    """Return the configured minimum pause after a provider rate limit."""
    configured = _env("LLM_RATE_LIMIT_COOLDOWN")
    if not configured:
        return DEFAULT_RATE_LIMIT_COOLDOWN
    try:
        cooldown = float(configured)
    except ValueError as error:
        raise LLMError("LLM_RATE_LIMIT_COOLDOWN must be a non-negative number.") from error
    if cooldown < 0:
        raise LLMError("LLM_RATE_LIMIT_COOLDOWN must be a non-negative number.")
    return cooldown


def _rate_limit_delay(retry_after: str | None = None) -> float:
    """Use the configured minute by default, respecting a longer provider hint."""
    delay = _rate_limit_cooldown()
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    return delay


def _pause_all_llm_requests(retry_after: str | None = None) -> float:
    """Extend the shared 429 pause and return the delay that was installed."""
    global _rate_limit_not_before
    delay = _rate_limit_delay(retry_after)
    with _rate_limit_gate_lock:
        _rate_limit_not_before = max(
            _rate_limit_not_before,
            time.monotonic() + delay,
        )
    return delay


def _wait_for_rate_limit_cooldown() -> float:
    """Block this request until the process-wide 429 pause has elapsed."""
    waited = 0.0
    while True:
        with _rate_limit_gate_lock:
            remaining = _rate_limit_not_before - time.monotonic()
        if remaining <= 0:
            return waited
        time.sleep(remaining)
        waited += remaining


def _reset_rate_limit_cooldown_for_tests() -> None:
    """Clear process-wide transport state between isolated test cases."""
    global _rate_limit_not_before
    with _rate_limit_gate_lock:
        _rate_limit_not_before = 0.0


def _profile_hash(
    name: str,
    provider: str,
    model: str,
    capabilities: tuple[str, ...],
    configuration_source: str,
) -> str:
    material = {
        "name": name,
        "provider": provider,
        "model": model,
        "capabilities": list(capabilities),
        "configuration_source": configuration_source,
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _declared_capabilities(name: str) -> tuple[str, ...] | None:
    raw = _env(f"{name.upper()}_CAPABILITIES")
    if not raw:
        return None
    capabilities = tuple(
        sorted({item.strip().lower() for item in raw.split(",") if item.strip()})
    )
    unknown = [item for item in capabilities if item not in {"vision"}]
    if unknown:
        raise LLMError(f"Unknown {name.upper()} capability '{unknown[0]}'.")
    return capabilities


def _model_capabilities(provider: str, model: str) -> tuple[str, ...] | None:
    catalog = assistant_settings.PROVIDERS[provider].get("model_capabilities") or {}
    if model not in catalog:
        return None
    return tuple(sorted(str(item) for item in catalog[model]))


def resolve_model_profile(profile: str = "agent") -> ResolvedModelProfile:
    """Resolve one model-level profile without probing a provider."""

    if profile not in {"agent", "vision"}:
        raise LLMError("Model profile must be 'agent' or 'vision'.")
    saved = assistant_settings.load()
    provider = saved["provider"]
    model = saved["model"]
    source = "assistant_settings"
    explicit_capabilities: tuple[str, ...] | None = None

    prefix = "AGENT_VISION" if profile == "vision" else "AGENT"
    override_provider = (_env(f"{prefix}_PROVIDER") or _env(f"{prefix}_BACKEND")).lower()
    override_model = _env(f"{prefix}_MODEL")
    declared = _declared_capabilities(prefix)
    if override_provider:
        if override_provider not in assistant_settings.PROVIDERS:
            supported = ", ".join(assistant_settings.PROVIDERS)
            raise LLMError(
                f"Unsupported {prefix}_PROVIDER '{override_provider}'. "
                f"Use one of: {supported}."
            )
        provider = override_provider
        provider_meta = assistant_settings.PROVIDERS[provider]
        model = override_model or str(
            (provider_meta.get("vision_model") or provider_meta["default_model"])
            if profile == "vision"
            else provider_meta["default_model"]
        )
        source = "environment"
        explicit_capabilities = declared
    elif override_model:
        model = override_model
        source = "environment"
        explicit_capabilities = declared
    elif profile == "vision":
        persisted = assistant_settings.load_agent_vision_profile()
        if persisted is not None:
            provider = str(persisted["provider"])
            model = str(persisted["model"])
            explicit_capabilities = tuple(persisted["capabilities"])
            source = "settings.agent_vision"
        else:
            model = str(
                assistant_settings.PROVIDERS[provider].get("vision_model") or model
            )
            source = "model_catalog"

    catalog_capabilities = _model_capabilities(provider, model)
    capabilities = (
        explicit_capabilities
        if explicit_capabilities is not None
        else catalog_capabilities or ()
    )
    capabilities = tuple(sorted(set(capabilities)))
    provider_meta = assistant_settings.PROVIDERS[provider]
    api_key = _env(str(provider_meta["api_key_env"]))
    if provider == "lmstudio" and not api_key:
        api_key = DEFAULT_LMSTUDIO_API_KEY
    configured = bool(api_key)
    reason = None
    if not configured:
        reason = (
            "Start LM Studio's local server."
            if provider == "lmstudio"
            else f"Set {provider_meta['api_key_env']}."
        )
    elif profile == "vision" and "vision" not in capabilities:
        reason = (
            f"Model '{model}' is not declared vision-capable. Choose a known "
            "vision model or explicitly declare the vision capability."
        )
    return ResolvedModelProfile(
        name=profile,
        provider=provider,
        model=model,
        capabilities=capabilities,
        configuration_source=source,
        configured=configured,
        base_url=str(provider_meta["base_url"]).rstrip("/"),
        profile_hash=_profile_hash(
            profile, provider, model, capabilities, source
        ),
        unavailability_reason=reason,
    )


def model_profile_snapshot() -> dict[str, dict]:
    """Snapshot both durable agent profiles for a newly created run."""

    text = resolve_model_profile("agent")
    vision = resolve_model_profile("vision")
    return {"text": text.to_dict(), "vision": vision.to_dict()}


def _settings(profile: str | dict = "assistant") -> LLMSettings:
    if isinstance(profile, dict):
        provider_name = str(profile.get("provider") or "")
        model_name = str(profile.get("model") or "")
        if provider_name not in assistant_settings.PROVIDERS or not model_name:
            raise LLMError("Persisted model profile snapshot is invalid.")
        provider_meta = assistant_settings.PROVIDERS[provider_name]
        api_key = _env(str(provider_meta["api_key_env"]))
        if provider_name == "lmstudio" and not api_key:
            api_key = DEFAULT_LMSTUDIO_API_KEY
        return LLMSettings(
            backend=provider_name,
            api_key=api_key,
            model=model_name,
            base_url=str(provider_meta["base_url"]).rstrip("/"),
            extra_headers={},
            timeout=_request_timeout(
                LOCAL_REQUEST_TIMEOUT if provider_meta["local"] else REQUEST_TIMEOUT
            ),
            profile_name=str(profile.get("name") or "agent"),
            capabilities=tuple(
                sorted(str(item) for item in profile.get("capabilities") or [])
            ),
            profile_hash=str(profile.get("profile_hash") or ""),
            configuration_source=str(
                profile.get("configuration_source") or "run_snapshot"
            ),
        )
    saved = assistant_settings.load()
    backend = saved["provider"]
    model = saved["model"]
    if profile in ("agent", "vision"):
        resolved = resolve_model_profile(profile)
        backend = resolved.provider
        model = resolved.model
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
        profile_name=str(profile),
        capabilities=(
            resolve_model_profile(profile).capabilities
            if profile in {"agent", "vision"}
            else ()
        ),
        profile_hash=(
            resolve_model_profile(profile).profile_hash
            if profile in {"agent", "vision"}
            else ""
        ),
        configuration_source=(
            resolve_model_profile(profile).configuration_source
            if profile in {"agent", "vision"}
            else "assistant_settings"
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
            "vision_unavailability_reason": str(error),
            "backend": "",
            "provider": "",
            "model": "",
            "base_url": "",
            "error": str(error),
        }
    text_profile = resolve_model_profile("agent")
    result = {
        "configured": bool(settings.api_key),
        "backend": settings.backend,
        "provider": settings.backend,
        "model": settings.model,
        "base_url": settings.base_url,
        "text_profile": text_profile.to_dict(),
    }
    try:
        vision_profile = resolve_model_profile("vision")
        result["vision_configured"] = bool(
            vision_profile.configured and "vision" in vision_profile.capabilities
        )
        result["vision_provider"] = vision_profile.provider
        result["vision_model"] = vision_profile.model
        result["vision_profile"] = vision_profile.to_dict()
        result["vision_unavailability_reason"] = (
            None
            if result["vision_configured"]
            else vision_profile.unavailability_reason
        )
    except (LLMError, assistant_settings.SettingsError) as error:
        result["vision_configured"] = False
        result["vision_unavailability_reason"] = str(error)
    return result


def image_part(content: bytes, mime: str) -> dict:
    """Build an OpenAI-compatible inline image content part."""
    if not str(mime or "").startswith("image/"):
        raise LLMError("Vision content must use an image MIME type.")
    encoded = base64.b64encode(content).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.0,
    profile: str | dict = "assistant",
    tool_choice: str | dict | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> dict:
    """One chat/completions round-trip. Returns the assistant message dict.

    The returned message may contain ``content`` and/or ``tool_calls``; the
    caller drives the tool loop. Raises :class:`LLMError` for any transport or
    API failure, with a message safe to show the user. ``profile`` selects the
    backend/model pair: 'assistant' (saved settings), 'agent' (agent overrides),
    or 'vision' (vision-capable profile overrides).

    Passing ``on_delta`` requests a streamed response and calls it with each
    text fragment as it arrives. The return value is unchanged, so a caller that
    only wants progress reporting needs no other adjustment. Streaming is
    text-only: a request carrying ``tools`` ignores ``on_delta``, because a
    partial tool call is not something a reader can be shown.
    """
    streaming = on_delta is not None and not tools
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
            fallback, extra={"profile": profile if isinstance(profile, str) else profile.get("name")},
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
        "max_tokens": _max_output_tokens(),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    if streaming:
        body["stream"] = True
        # Ask for usage on the terminal chunk. Providers that ignore this simply
        # report no usage, which the budget ledger already tolerates.
        body["stream_options"] = {"include_usage": True}
    call_id, _ = debug_store.start_call(
        body, settings, extra={"profile": settings.profile_name}
    )
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
        cooldown_wait = _wait_for_rate_limit_cooldown()
        attempt_started_wall = debug_store.utcnow()
        attempt_started = time.monotonic()
        attempt_record: dict = {"number": attempt + 1, "started_at": attempt_started_wall}
        if cooldown_wait:
            attempt_record["rate_limit_wait_ms"] = round(cooldown_wait * 1000, 3)
        payload = None
        response_headers = None
        raw_body = b""
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout) as response:
                headers_at = time.monotonic()
                response_headers = getattr(response, "headers", {})
                if streaming:
                    assert on_delta is not None
                    payload, raw_body = _read_stream(response, on_delta)
                else:
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
                    if not streaming:
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
            if error.code == 429:
                delay = _pause_all_llm_requests(
                    error.headers.get("Retry-After") if error.headers else None
                )
                attempt_record["rate_limit_cooldown_ms"] = round(delay * 1000, 3)
                if attempt + 1 < MAX_REQUEST_ATTEMPTS:
                    attempt_record["retry_delay_ms"] = round(delay * 1000, 3)
                    _finish_attempt(attempt_record, attempt_started)
                    if call_id:
                        debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                    continue
            elif error.code in RETRYABLE_HTTP_CODES and attempt + 1 < MAX_REQUEST_ATTEMPTS:
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

        provider_error = _response_error(payload)
        if provider_error is not None:
            error_code, detail = provider_error
            attempt_record["error"] = detail
            attempt_record["error_response"] = debug_store.sanitize(payload)
            # An aggregator such as OpenRouter reports an upstream failure in
            # the body of an HTTP 200, so the transport-level retry above never
            # sees it. A transient upstream code must exhaust the same ladder
            # here or a rate-limited provider kills the unit — and, because a
            # failed unit folds the whole run to failed, the run with it.
            if _is_rate_limit_error(error_code):
                delay = _pause_all_llm_requests(_retry_after_header(response_headers))
                attempt_record["rate_limit_cooldown_ms"] = round(delay * 1000, 3)
                if attempt + 1 < MAX_REQUEST_ATTEMPTS:
                    attempt_record["retry_delay_ms"] = round(delay * 1000, 3)
                    _finish_attempt(attempt_record, attempt_started)
                    if call_id:
                        debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                    continue
            elif (
                _retryable_provider_error(error_code, detail)
                and attempt + 1 < MAX_REQUEST_ATTEMPTS
            ):
                delay = _wait_before_retry(
                    attempt, _retry_after_header(response_headers)
                )
                attempt_record["retry_delay_ms"] = round(delay * 1000, 3)
                _finish_attempt(attempt_record, attempt_started)
                if call_id:
                    debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                continue
            terminal = LLMError(
                f"LLM request failed ({error_code}): {detail}"
                if error_code is not None
                else f"LLM request failed: {detail}"
            )
            _finish_attempt(attempt_record, attempt_started)
            if call_id:
                debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
                debug_store.finish_call(
                    trace_workspace_id,
                    call_id,
                    payload=payload,
                    error=str(terminal),
                    headers=response_headers,
                    started_monotonic=call_started,
                )
            raise terminal

        _finish_attempt(attempt_record, attempt_started)
        if call_id: debug_store.add_attempt(trace_workspace_id, call_id, attempt_record)
        choices = (payload.get("choices") or []) if isinstance(payload, dict) else []
        if choices:
            message = choices[0].get("message") or {}
            if call_id:
                debug_store.finish_call(trace_workspace_id, call_id, payload=payload,
                                        message=message, headers=response_headers,
                                        started_monotonic=call_started)
            # Usage is a sibling of `choices`, not a field of the message. The
            # budget ledger reads it off the returned message, so without this
            # it saw no usage on any call and fell back silently — metering the
            # provider's own prompt count as the local estimate and every
            # completion as zero, which left `max_completion_tokens` checked
            # against a total that could never grow. Attached on return rather
            # than before the trace so the debug record keeps the raw message.
            usage = payload.get("usage")
            # `finish_reason` is a sibling too, and it is the only thing that
            # separates a model that had nothing to say from one that was cut
            # off before it started. A caller that cannot tell them apart reads
            # a truncated completion as malformed output and asks the model to
            # correct text it never sent — which is how one RCM run spent both
            # of its attempts, and $0.09, on an empty string.
            finish_reason = choices[0].get("finish_reason")
            return {
                **message,
                **({"usage": usage} if isinstance(usage, dict) else {}),
                **({"finish_reason": str(finish_reason)} if finish_reason else {}),
            }
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


def _response_error(payload: object) -> tuple[object | None, str] | None:
    """Extract an in-band provider error from an HTTP-success response."""
    if not isinstance(payload, dict) or "error" not in payload:
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message") or "unknown provider error")
        metadata = error.get("metadata")
        error_type = (
            metadata.get("error_type") if isinstance(metadata, dict) else None
        )
        detail = f"{message} ({error_type})" if error_type else message
        return code, detail
    return None, str(error or "unknown provider error")


def _read_stream(
    response: Any, on_delta: Callable[[str], None]
) -> tuple[dict, bytes]:
    """Consume a streamed completion, reporting text as it arrives.

    Returns a payload in the same shape a non-streamed call produces, so every
    caller downstream — provider-error detection, usage accounting, choice
    extraction — is unchanged. ``raw_body`` is the reassembled transcript, kept
    for the response hash and size that telemetry records.

    A malformed chunk is skipped rather than raised: the stream is a progress
    channel, and one unparseable frame must not discard a completion that is
    otherwise arriving normally. An in-band ``error`` frame is returned as the
    payload so the existing provider-error path handles it.
    """
    content: list[str] = []
    finish_reason = None
    usage = None
    role = "assistant"
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if "error" in chunk:
            return chunk, "".join(content).encode("utf-8")
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("role"):
                role = str(delta["role"])
            piece = delta.get("content")
            if piece:
                content.append(str(piece))
                on_delta(str(piece))
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
    text = "".join(content)
    payload: dict = {
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": role, "content": text},
            }
        ]
    }
    if usage:
        payload["usage"] = usage
    return payload, text.encode("utf-8")


def _retryable_provider_error(code: object, detail: str) -> bool:
    """True when an in-band provider error is worth another attempt.

    An aggregator reports an upstream failure inside an HTTP 200 body, and its
    numeric code is not a reliable signal on its own: the same ``502`` covers
    both "the upstream is momentarily out of capacity" and "your request is
    invalid", and retrying the latter burns the budget three times for an
    identical answer. So a numeric code additionally has to read as transient,
    while a symbolic code is specific enough to trust by itself.
    """
    if isinstance(code, bool):
        return False
    text = str(code or "").strip().lower()
    if isinstance(code, int) or text.isdigit():
        numeric = code if isinstance(code, int) else int(text)
        return numeric in RETRYABLE_HTTP_CODES and _transient_provider_detail(detail)
    return bool(text) and text in RETRYABLE_PROVIDER_CODES


def _is_rate_limit_error(code: object) -> bool:
    """Recognize both HTTP-style and aggregator rate-limit error codes."""
    if isinstance(code, bool):
        return False
    text = str(code or "").strip().lower()
    return text == "429" or text in {"rate_limit_exceeded", "rate_limited"}


def _transient_provider_detail(detail: str) -> bool:
    """True when an error message describes a condition that may clear."""
    text = str(detail or "").lower()
    return any(marker in text for marker in TRANSIENT_PROVIDER_MARKERS)


def _retry_after_header(headers: object) -> str | None:
    """Read ``Retry-After`` off a successful response that carried an error."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    try:
        value = getter("Retry-After")
    except Exception:
        return None
    return str(value) if value else None


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
