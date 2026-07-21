"""Budgeted, attributed model calls for durable agent work."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from ... import debug_store, llm


DEFAULT_MAX_MODEL_TURNS = 40

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
    owns provider selection, concurrency, retry accounting, token charging,
    telemetry, and hash-only provenance.
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

    Persistence and event callbacks remain injected until the durable runtime
    extraction. This class owns the model-call behavior itself and has no
    scheduler or domain dependencies.
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_root: str,
        run: dict[str, Any],
        state_lock: threading.RLock,
        checkpoint: Callable[[], None],
        save: Callable[[], None],
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
        self._save = save
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
        with self._state_lock:
            usage = self.run.setdefault("usage", {})
            maximum_turns = int(
                (self.run.get("limits") or {}).get("max_model_turns")
                or DEFAULT_MAX_MODEL_TURNS
            )
            if usage.get("llm_turns", 0) >= maximum_turns:
                raise self._limit_error("model turn limit reached")
            maximum_prompt_tokens = int(
                (self.run.get("limits") or {}).get("max_estimated_prompt_tokens")
                or maximum_turns * 10_000
            )
            projected_tokens = (
                int(usage.get("estimated_prompt_tokens") or 0)
                + estimated_input_tokens
            )
            if projected_tokens > maximum_prompt_tokens:
                raise self._limit_error("estimated prompt-token limit reached")
            usage["llm_turns"] = usage.get("llm_turns", 0) + 1
            usage["estimated_prompt_tokens"] = projected_tokens
            usage["request_characters"] = (
                int(usage.get("request_characters") or 0) + request_characters
            )
            if attempt > 1:
                usage["retries"] = int(usage.get("retries") or 0) + 1
            self._save()

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

        with self._state_lock:
            usage = self.run.setdefault("usage", {})
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
            usage["prompt_tokens"] = (
                int(usage.get("prompt_tokens") or 0) + prompt_tokens
            )
            usage["completion_tokens"] = (
                int(usage.get("completion_tokens") or 0) + completion_tokens
            )
            maximum_completion_tokens = int(
                (self.run.get("limits") or {}).get("max_completion_tokens")
                or maximum_turns * 4_000
            )
            token_budget_exceeded = (
                usage["prompt_tokens"] > maximum_prompt_tokens
                or usage["completion_tokens"] > maximum_completion_tokens
            )
            usage["model_calls_by_worker"] = dict(
                usage.get("model_calls_by_worker") or {}
            )
            usage["model_calls_by_worker"][tag] = int(
                usage["model_calls_by_worker"].get(tag) or 0
            ) + 1
            worker_totals = usage.setdefault("model_usage_by_worker", {}).setdefault(
                tag,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "request_characters": 0,
                    "latency_ms": 0.0,
                    "retries": 0,
                },
            )
            worker_totals["calls"] += 1
            worker_totals["prompt_tokens"] += prompt_tokens
            worker_totals["completion_tokens"] += completion_tokens
            worker_totals["request_characters"] += request_characters
            worker_totals["latency_ms"] = round(
                float(worker_totals["latency_ms"]) + latency_ms,
                3,
            )
            worker_totals["retries"] += int(attempt > 1)
            usage.setdefault("model_call_metrics", []).append(
                {
                    "worker": tag,
                    "request_characters": request_characters,
                    "estimated_input_tokens": estimated_input_tokens,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "latency_ms": latency_ms,
                    "retry_number": attempt,
                    "context_metrics": (activity or {}).get("context_metrics"),
                }
            )
            self._save()

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
