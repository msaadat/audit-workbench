"""Budgeted, attributed model calls for durable agent work."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from ... import debug_store, llm


_provider_semaphores: dict[str, threading.BoundedSemaphore] = {}
_provider_semaphores_guard = threading.Lock()
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_CACHE_KEY = re.compile(r"^[0-9a-f]{64}$")
FALLBACK_IMAGE_TOKENS = 4_096


class ModelCapabilityError(RuntimeError):
    """The snapshotted model profile cannot serve the worker request."""


class PreparedMediaError(RuntimeError):
    """A prepared-media handle cannot be integrity-checked and loaded."""


class VisionRequestRejected(RuntimeError):
    """A declared vision provider rejected an image-bearing request."""


def _media_handle(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach the content-free prepared-media handle contract."""

    required = {
        "cache_key",
        "source_ref",
        "source_sha1",
        "prepared_sha256",
        "page",
        "frame",
        "variant",
        "tile_order",
        "mime",
        "width",
        "height",
        "prepared_byte_count",
        "pixel_count",
        "policy_hash",
        "prepared_set_hash",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise PreparedMediaError("Prepared-media handle fields are invalid.")
    handle = dict(value)
    if not _CACHE_KEY.fullmatch(str(handle["cache_key"])):
        raise PreparedMediaError("Prepared-media cache key is invalid.")
    match = _SHA256.fullmatch(str(handle["prepared_sha256"]))
    if match is None:
        raise PreparedMediaError("Prepared-media SHA-256 is invalid.")
    if not str(handle["mime"]).startswith("image/"):
        raise PreparedMediaError("Prepared media must use an image MIME type.")
    for name in (
        "page",
        "frame",
        "tile_order",
        "width",
        "height",
        "prepared_byte_count",
        "pixel_count",
    ):
        if isinstance(handle[name], bool) or not isinstance(handle[name], int):
            raise PreparedMediaError(f"Prepared-media {name} is invalid.")
    if min(
        handle["page"],
        handle["frame"],
        handle["width"],
        handle["height"],
        handle["prepared_byte_count"],
        handle["pixel_count"],
    ) < 1 or handle["tile_order"] < 0:
        raise PreparedMediaError("Prepared-media numeric metrics are invalid.")
    return handle


# Streaming is a progress channel, not a transcript. Fragments are coalesced so
# a long generation costs a readable trickle of events rather than one per token.
# The character rule paces a fast provider; the time rule guarantees a slow one
# still shows movement. Together they hold a typical generation to one or two
# updates a second — enough to read as live, few enough that the run's durable
# event feed stays a reasonable size.
STREAM_MIN_CHARS = 80
STREAM_MIN_SECONDS = 0.4


class _StreamCoalescer:
    """Batch provider text fragments into periodic, readable updates.

    A provider emits a token at a time. Forwarding each one would put thousands
    of events on a run's durable feed for a single call, so fragments are held
    until they are worth sending — enough characters, or enough elapsed time
    that a reader would otherwise think nothing was happening.

    One instance per provider call, and its ``call_id`` is what tells a reader
    where one call's text ends and the next begins. Without it two calls of the
    same stage — a retry, or two units of one capability — run together into a
    single unreadable block.
    """

    def __init__(self, sink: Callable[[str], None]):
        self.call_id = uuid.uuid4().hex[:12]
        self._sink = sink
        self._pending: list[str] = []
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def feed(self, piece: str) -> None:
        with self._lock:
            self._pending.append(piece)
            pending = "".join(self._pending)
            now = time.monotonic()
            if len(pending) < STREAM_MIN_CHARS and now - self._last < STREAM_MIN_SECONDS:
                return
            self._pending.clear()
            self._last = now
        self._emit(pending)

    def flush(self) -> None:
        with self._lock:
            pending = "".join(self._pending)
            self._pending.clear()
        if pending:
            self._emit(pending)

    def _emit(self, text: str) -> None:
        try:
            self._sink(text)
        except Exception:
            # A progress update must never break the call that produced it.
            pass


def _provider_semaphore(profile: Mapping[str, Any]) -> threading.BoundedSemaphore:
    """Return the process-wide gate for one provider/model profile."""
    key = f"{profile.get('provider') or profile.get('backend')}:{profile.get('model')}"
    try:
        capacity = max(
            1,
            int(os.environ.get("AGENT_PROVIDER_MAX_CONCURRENCY") or 4),
        )
    except ValueError:
        capacity = 4
    with _provider_semaphores_guard:
        return _provider_semaphores.setdefault(
            key,
            threading.BoundedSemaphore(capacity),
        )


@runtime_checkable
class ModelGateway(Protocol):
    """Execute one budgeted, attributed model turn.

    Callers own prompt construction and response parsing. The implementation
    owns provider selection, concurrency, telemetry, and hash-only provenance,
    and coordinates budget charging and retry accounting through RunRuntime.
    """

    def complete(
        self,
        system: str,
        user: str,
        activity: dict[str, Any] | None = None,
        *,
        attempt: int = 1,
        media: tuple[Mapping[str, Any], ...] | None = None,
        required_capabilities: tuple[str, ...] = (),
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        conversation: list[dict[str, Any]] | None = None,
        return_message: bool = False,
    ) -> str | dict[str, Any]: ...


class DefaultModelGateway:
    """Active model gateway shared by the current runner facade.

    Runtime callbacks own durable budgets and activity projection. This class
    owns the provider call behavior itself and has no scheduler or domain
    dependencies.
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_root: str,
        run: dict[str, Any],
        state_lock: threading.RLock,
        checkpoint: Callable[[], None],
        reserve_model_turn: Callable[..., dict[str, int]],
        record_model_usage: Callable[..., bool],
        model_wait: Callable[..., None],
        utcnow: Callable[[], str],
        append_provenance: Callable[[dict[str, Any]], None],
        template_context: Callable[[str], dict[str, Any] | None],
        stage_labels: Mapping[str, str] | None = None,
        limit_error: type[Exception] = RuntimeError,
        stream_sink: Callable[[str, str, str], None] | None = None,
    ):
        self.workspace_id = workspace_id
        self.workspace_root = workspace_root
        self.run = run
        self._state_lock = state_lock
        self._checkpoint = checkpoint
        self._reserve_model_turn = reserve_model_turn
        self._record_model_usage = record_model_usage
        self._model_wait_projection = model_wait
        self._utcnow = utcnow
        self._append_provenance = append_provenance
        self._template_context = template_context
        self._stage_labels = dict(stage_labels or {})
        self._limit_error = limit_error
        # Optional live-progress channel. Absent, calls behave exactly as before
        # and no streamed request is made.
        self._stream_sink = stream_sink
        self.context = threading.local()

    def complete(
        self,
        system: str,
        user: str,
        activity: dict[str, Any] | None = None,
        *,
        attempt: int = 1,
        media: tuple[Mapping[str, Any], ...] | None = None,
        required_capabilities: tuple[str, ...] = (),
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        conversation: list[dict[str, Any]] | None = None,
        return_message: bool = False,
    ) -> str | dict[str, Any]:
        """Execute and provenance-log one model turn."""
        self._checkpoint()
        handles = tuple(_media_handle(item) for item in (media or ()))
        required = tuple(
            sorted(
                {
                    *(
                        str(value)
                        for value in getattr(
                            self.context, "required_model_capabilities", ()
                        )
                    ),
                    *(str(value) for value in required_capabilities),
                    *(("vision",) if handles else ()),
                }
            )
        )
        snapshots = self.run.get("model_profiles") or llm.model_profile_snapshot()
        snapshot_key = "vision" if "vision" in required else "text"
        profile = dict(snapshots.get(snapshot_key) or {})
        durable_snapshot = bool(
            self.run.get("model_profiles_snapshotted")
        )
        if not durable_snapshot and snapshot_key == "text":
            legacy = llm.agent_status()
            profile.update(
                {
                    "name": "agent",
                    "provider": legacy.get("provider"),
                    "model": legacy.get("model"),
                    "configured": legacy.get("configured"),
                    "capabilities": profile.get("capabilities") or [],
                }
            )
        capabilities = set(str(value) for value in profile.get("capabilities") or [])
        missing_capabilities = sorted(set(required) - capabilities)
        if missing_capabilities:
            raise ModelCapabilityError(
                "The configured model profile does not provide required "
                f"capability '{missing_capabilities[0]}'."
            )
        if not profile.get("configured"):
            raise ModelCapabilityError(
                str(profile.get("unavailability_reason") or "The model profile is unavailable.")
            )
        # A missing or changed cache entry is a preparation failure, not a
        # provider turn. Verify all handles before reserving model budget.
        prepared_contents = [
            self._load_prepared_media(handle) for handle in handles
        ]

        if conversation is not None and handles:
            raise ValueError("A tool conversation cannot include prepared media.")
        if tool_choice is not None and not tools:
            raise ValueError("tool_choice requires tools.")
        conversation_messages = list(conversation or [{"role": "user", "content": user}])
        if not conversation_messages:
            raise ValueError("A model conversation needs at least one message.")
        request_user = (
            json.dumps(conversation_messages, default=str, ensure_ascii=False)
            if conversation is not None
            else user
        )
        request_characters = len(system) + len(request_user)
        text_token_estimate = max(1, request_characters // 4)
        image_token_estimate = len(handles) * FALLBACK_IMAGE_TOKENS
        estimated_input_tokens = text_token_estimate + image_token_estimate
        media_bytes = sum(int(item["prepared_byte_count"]) for item in handles)
        media_pixels = sum(int(item["pixel_count"]) for item in handles)

        # Charge before the provider call. Actual counts are reconciled after
        # the response, but an estimated overage never spends provider tokens.
        budget = self._reserve_model_turn(
            request_characters=request_characters,
            estimated_input_tokens=estimated_input_tokens,
            text_token_estimate=text_token_estimate,
            image_token_estimate=image_token_estimate,
            image_count=len(handles),
            prepared_bytes=media_bytes,
            prepared_pixels=media_pixels,
            attempt=attempt,
        )

        tag = self._stage_tag(system)
        stream = self._stream_coalescer(tag)
        self._model_wait(tag, started=True, attempt=attempt)
        activity_fields = dict(activity or {})
        unit_id = getattr(self.context, "unit_id", None)
        parent_refs = getattr(self.context, "parent_refs", None)
        if unit_id:
            activity_fields.setdefault("unit_id", unit_id)
        if parent_refs:
            activity_fields.setdefault("parent_refs", list(parent_refs))
        activity_fields.setdefault("retry_number", attempt)
        current_activity = dict(self.run.get("activity") or {})
        call_started = time.monotonic()
        user_parts: str | list[dict[str, Any]] = user
        if handles:
            user_parts = [{"type": "text", "text": user}]
            for handle, content in zip(handles, prepared_contents):
                user_parts.append(llm.image_part(content, str(handle["mime"])))

        try:
            with debug_store.trace_context(
                workspace_id=self.workspace_id,
                workspace_root=self.workspace_root,
                run_id=self.run["id"],
                action_id=(
                    activity_fields.get("action_id")
                    or current_activity.get("action_id")
                ),
                task_id=(
                    activity_fields.get("task_id")
                    or current_activity.get("task_id")
                ),
                chat_id=self.run.get("chat_id"),
                stage=tag,
                purpose=tag,
                document_ids=activity_fields.get("document_ids"),
                artifact_refs=activity_fields.get("artifact_refs"),
                unit_id=unit_id,
                parent_refs=parent_refs,
            ):
                with _provider_semaphore(profile):
                    try:
                        provider_messages = (
                            [{"role": "system", "content": system}, *conversation_messages]
                            if conversation is not None
                            else [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user_parts},
                            ]
                        )
                        chat_kwargs = {
                            "profile": (
                                profile
                                if durable_snapshot
                                else "vision"
                                if snapshot_key == "vision"
                                else "agent"
                            ),
                        }
                        if tools:
                            chat_kwargs["tools"] = tools
                            if tool_choice is not None:
                                chat_kwargs["tool_choice"] = tool_choice
                        # A tool-capable turn returns a structured call, not
                        # prose, so there is nothing a reader could follow.
                        if stream is not None and not tools:
                            chat_kwargs["on_delta"] = stream.feed
                        try:
                            message = llm.chat(provider_messages, **chat_kwargs)
                        finally:
                            if stream is not None:
                                stream.flush()
                    except llm.LLMError as error:
                        if handles:
                            raise VisionRequestRejected(
                                f"vision_request_rejected: {error}"
                            ) from error
                        raise
        finally:
            self._model_wait(tag, started=False, attempt=attempt)

        with self._state_lock:
            sources = list(self.run.get("model_sources") or [])
        template_versions = self._template_versions(tag)
        content = str(message.get("content") or "")
        latency_ms = round((time.monotonic() - call_started) * 1000, 3)
        provider_usage = (
            message.get("usage") if isinstance(message.get("usage"), dict) else {}
        )

        prompt_tokens = int(
            provider_usage.get("prompt_tokens")
            or provider_usage.get("input_tokens")
            or estimated_input_tokens
        )
        completion_tokens = int(
            provider_usage.get("completion_tokens")
            or provider_usage.get("output_tokens")
            or 0
        )
        token_budget_exceeded = self._record_model_usage(
            worker=tag,
            request_characters=request_characters,
            estimated_input_tokens=estimated_input_tokens,
            text_token_estimate=text_token_estimate,
            image_token_estimate=image_token_estimate,
            image_count=len(handles),
            prepared_bytes=media_bytes,
            prepared_pixels=media_pixels,
            model_profile_hash=str(profile.get("profile_hash") or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            attempt=attempt,
            context_metrics=(activity or {}).get("context_metrics"),
            budget=budget,
        )

        self._append_provenance(
            {
                "run_id": self.run["id"],
                "stage": tag,
                "task": None,
                "purpose": tag,
                "provider": profile.get("provider"),
                "model": profile.get("model"),
                "profile": profile.get("name"),
                "model_profile_hash": profile.get("profile_hash"),
                "vision_used": bool(handles),
                "prompt_version": hashlib.sha1(
                    json.dumps(
                        {
                            "system": system,
                            "user": request_user,
                            "prepared_media": [
                                {
                                    "prepared_sha256": item["prepared_sha256"],
                                    "page": item["page"],
                                    "variant": item["variant"],
                                    "tile_order": item["tile_order"],
                                }
                                for item in handles
                            ],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "template_versions": template_versions,
                "knowledge_packs": [
                    {
                        "source_ref": item["source_ref"],
                        "sha1": item.get("source_sha1"),
                    }
                    for item in sources
                    if str(item.get("source_ref", "")).startswith("pack:")
                ],
                "document_ids": activity_fields.pop(
                    "document_ids",
                    [
                        item["document_id"]
                        for item in sources
                        if not str(item.get("source_ref", "")).startswith("pack:")
                    ],
                ),
                "page_ranges": activity_fields.pop(
                    "page_ranges",
                    sorted(
                        {
                            page
                            for item in sources
                            for page in item.get("pages", [])
                        }
                    ),
                ),
                "source_hashes": activity_fields.pop(
                    "source_hashes",
                    sorted(
                        {
                            item["source_sha1"]
                            for item in sources
                            if item.get("source_sha1")
                        }
                    ),
                ),
                "response_at": self._utcnow(),
                "response_hash": hashlib.sha1(content.encode("utf-8")).hexdigest(),
                "artifact_ref": None,
                "disposition": "generated",
                "latency_ms": latency_ms,
                "input_modalities": (
                    ["text", "image"] if handles else ["text"]
                ),
                "prepared_media_hashes": [
                    item["prepared_sha256"] for item in handles
                ],
                **activity_fields,
            }
        )
        if token_budget_exceeded:
            raise self._limit_error("workflow token budget reached")
        return message if return_message else content

    def _load_prepared_media(self, handle: Mapping[str, Any]) -> bytes:
        """Dereference a prepared-media handle immediately before provider use."""

        cache_key = str(handle["cache_key"])
        path = (
            Path(self.workspace_root)
            / "Documents"
            / ".prepared"
            / f"{cache_key}.png"
        )
        try:
            content = path.read_bytes()
        except OSError as error:
            raise PreparedMediaError(
                "visual_preparation_failed: prepared media is missing"
            ) from error
        expected = _SHA256.fullmatch(str(handle["prepared_sha256"]))
        actual = hashlib.sha256(content).hexdigest()
        if expected is None or actual != expected.group(1):
            raise PreparedMediaError(
                "visual_preparation_failed: prepared media hash mismatch"
            )
        if len(content) != int(handle["prepared_byte_count"]):
            raise PreparedMediaError(
                "visual_preparation_failed: prepared media byte count mismatch"
            )
        return content

    @staticmethod
    def _stage_tag(system: str) -> str:
        return (
            system.split("]", 1)[0].lstrip("[")
            if system.startswith("[")
            else "agent"
        )

    def _template_versions(self, tag: str) -> list[dict[str, Any]]:
        context = self._template_context(tag)
        if not context:
            return []
        return [
            {
                "name": context["name"],
                "source": context["source"],
                "sha1": hashlib.sha1(
                    str(context["markdown"]).encode("utf-8")
                ).hexdigest(),
            }
        ]

    def _stream_coalescer(self, tag: str) -> _StreamCoalescer | None:
        """Bind this call's live-progress channel, if the runner supplied one."""
        if self._stream_sink is None:
            return None
        sink = self._stream_sink
        coalescer: _StreamCoalescer

        def emit(text: str) -> None:
            sink(tag, text, coalescer.call_id)

        coalescer = _StreamCoalescer(emit)
        return coalescer

    def _model_wait(self, tag: str, *, started: bool, attempt: int = 1) -> None:
        self._model_wait_projection(
            tag.replace("agent:", "model."),
            self._stage_labels.get(tag, "Waiting for the model"),
            started=started,
            attempt=attempt,
        )


__all__ = [
    "DefaultModelGateway",
    "FALLBACK_IMAGE_TOKENS",
    "ModelCapabilityError",
    "ModelGateway",
    "PreparedMediaError",
    "VisionRequestRejected",
]
