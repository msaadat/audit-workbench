"""Budgeted, attributed model calls for durable agent work."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from ... import debug_store, llm


_provider_semaphores: dict[str, threading.BoundedSemaphore] = {}
_provider_semaphores_guard = threading.Lock()


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
    ) -> str: ...


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
        self.context = threading.local()

    def complete(
        self,
        system: str,
        user: str,
        activity: dict[str, Any] | None = None,
        *,
        attempt: int = 1,
    ) -> str:
        """Execute and provenance-log one model turn."""
        self._checkpoint()
        request_characters = len(system) + len(user)
        estimated_input_tokens = max(1, request_characters // 4)

        # Charge before the provider call. Actual counts are reconciled after
        # the response, but an estimated overage never spends provider tokens.
        budget = self._reserve_model_turn(
            request_characters=request_characters,
            estimated_input_tokens=estimated_input_tokens,
            attempt=attempt,
        )

        tag = self._stage_tag(system)
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
                profile_state = llm.agent_status()
                with _provider_semaphore(profile_state):
                    message = llm.chat(
                        [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        profile="agent",
                    )
        finally:
            self._model_wait(tag, started=False, attempt=attempt)

        profile = llm.agent_status()
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
                "vision_used": False,
                "prompt_version": hashlib.sha1(
                    f"{system}\n{user}".encode("utf-8")
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
                **activity_fields,
            }
        )
        if token_budget_exceeded:
            raise self._limit_error("workflow token budget reached")
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

    def _model_wait(self, tag: str, *, started: bool, attempt: int = 1) -> None:
        self._model_wait_projection(
            tag.replace("agent:", "model."),
            self._stage_labels.get(tag, "Waiting for the model"),
            started=started,
            attempt=attempt,
        )
