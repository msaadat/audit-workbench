"""Analysis-side composition of the domain-neutral capability scheduler.

Every analysis capability is bound to a native scheduler path: a per-unit
pipeline binding for the one model-backed capability (analysis definitions) and
a per-unit deterministic computation for the three that need no model. This
module owns only the analysis-shaped glue the scheduler must not know about —
which worker and executor a unit uses, the declared context scope it resolves,
the approval items an auditor sees, the scope-clarification checkpoint, the
post-commit bookkeeping, and the completion projection.

The deterministic capabilities here differ from the audit workflow's in one
respect: ``data.joins_ready`` and ``analysis.executed`` commit through
*registered* executors and persist a receipt, because both mutate durable
workspace state under a compare-and-swap guard and must be replayable after an
interruption. The scheduler still drives them through its deterministic path;
the binder owns the executor call and the receipt sidecar, exactly as the
pipeline path would.
"""

from __future__ import annotations

import uuid

from ..workspace_transactions import parent_hashes
from ..workspaces import Workspace, WorkspaceConflict
from . import joins as join_diagnostics, narration, store, workflow
from .base import BaseRunner
from .capabilities.analysis import (
    ANALYSIS_SCOPE_CHECKPOINT,
    ANALYSIS_SUMMARY_REF,
    STAGE_CHECKPOINTS,
    MAX_SCOPE_TABLES,
    TableScope,
    agent_analyses,
    pair_join,
    resolve_table_scope,
)
from .capabilities import ANALYSIS_REGISTRY
from .context import (
    ContextResolver,
    analysis_definition_scope,
    analysis_summary_scope,
)
from .executors import EXECUTORS, ExecutorReceipt
from .executors.analysis import (
    AMBIGUOUS_RELATIONSHIP,
    ANALYSIS_COVERED_ELSEWHERE,
    AUDITOR_ANALYSIS_PRESERVED,
    AnalysisDefinitionExecutorTarget,
    AnalysisExecutionExecutorTarget,
    AnalysisSummaryExecutorTarget,
    JoinExecutorTarget,
    analysis_ref,
    infer_relationship,
    join_ref,
    relationship_ref,
    run_analysis,
)
from ..analysis_results import analyses_summary_payload, analysis_result_state
from .execution_support import refresh_workspace, workflow_scope
from .runtime import (
    BoundUnitPipeline,
    CapabilityExecution,
    CapabilityExecutionRegistry,
    DeterministicUnitResult,
    FinishProjection,
    RunRuntime,
    UnitPipeline,
    UnitPipelineConflict,
    UnitPipelineRequest,
    UnitSidecarStore,
    WorkflowRunner,
    first_unit_error,
    fold_terminal_status,
    unsettled_capabilities,
)
from .workers import WORKERS
from .workers.analysis import NOTHING_NEW_TO_ANALYSE


class DeterministicCommitConflict(WorkspaceConflict):
    """A deterministic commit was reconciled to a conflict rather than repeated."""

    def __init__(self, reason: str, revision: int):
        super().__init__(revision, revision)
        self.args = (reason,)


class AnalysisWorkflowExecution(BaseRunner):
    """Per-unit analysis execution bindings and projections for the scheduler."""

    stage_titles = {
        "relationships": "Table relationships",
        "joins": "Materialized joins",
        "analysis_definitions": "Analysis definitions",
        "analysis_execution": "Analysis results",
        "analysis_summary": "Analysis summary",
    }

    def __init__(
        self,
        workspace: Workspace,
        run: dict,
        handle,
        *,
        runtime: RunRuntime | None = None,
        context_resolver: ContextResolver | None = None,
    ):
        super().__init__(workspace, run, handle, runtime=runtime)
        self.context_resolver = context_resolver or ContextResolver()
        self.sidecars = UnitSidecarStore(workspace, run["id"])
        self.unit_pipeline: UnitPipeline | None = None
        self.scheduler: WorkflowRunner | None = None

    # ------------------------------------------------------------- scheduling
    def _refresh_dynamic_limits(self) -> None:
        """Size the model budget from the frames actually in scope.

        Two analysis capabilities are model-backed: definitions runs one turn
        per target frame, and the summary runs exactly one more for the whole
        workspace. The budget follows the resolved scope rather than a fixed
        constant, with headroom for the summary's own repair turn.
        """
        table_scope = self.scope()
        targets = max(1, len(table_scope.targets))
        calculated = 12 + 2 * targets
        self.update_limits(
            {
                "max_model_turns": calculated,
                "max_estimated_prompt_tokens": calculated * 10_000,
                "max_completion_tokens": calculated * 4_000,
            },
            grow_only=True,
        )

    def scope(self) -> TableScope:
        return resolve_table_scope(self.ws, workflow_scope(self.run))

    def milestone_projection(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
    ) -> dict | None:
        """Summarize a meaningful analysis boundary from bounded result data."""
        self.ws = subject
        requested = set((run.get("workflow") or {}).get("requested_outcomes") or [])
        if capability.id == "data.joins_ready":
            if capability.id not in requested:
                return None
            scope = self.scope()
            records = self.relationship_records()
            ambiguous = sum(
                str(item.get("status") or "") == "ambiguous" for item in records
            )
            supported = sum(
                bool(item.get("join") or item.get("recommended")) for item in records
            )
            return {
                "headline": "Table relationships analyzed",
                "summary": (
                    f"Compared {len(scope.pairs())} table pair(s) across "
                    f"{len(scope.tables)} scoped table(s). "
                    f"{len(scope.joins)} join(s) are available; "
                    f"{ambiguous} pair(s) still need confirmation."
                ),
                "metrics": [
                    {"label": "Scoped tables", "value": len(scope.tables)},
                    {"label": "Pairs compared", "value": len(scope.pairs())},
                    {"label": "Supported relationships", "value": supported},
                    {"label": "Available joins", "value": len(scope.joins)},
                ],
                "artifact_refs": [
                    ref
                    for unit in stage.get("units") or []
                    for ref in unit.get("result_refs") or []
                ],
            }
        if capability.id != "analysis.executed":
            return None

        scope = self.scope()
        targets = set(scope.targets)
        payload = analyses_summary_payload(subject)
        items = [
            item
            for item in payload["items"]
            if item.get("run_id") == run.get("id")
            and str(item.get("table") or "") in targets
        ]
        counts = {
            "needs_review": sum(
                item["classification"] in {"exception", "unusual"} for item in items
            ),
            "errors": sum(item["classification"] == "execution_error" for item in items),
            "clear": sum(item["classification"] == "clear" for item in items),
            "informational": sum(
                item["classification"] == "informational" for item in items
            ),
        }
        table_word = (
            "all eligible tables"
            if not scope.explicit and scope.ambiguity is None
            else f"{len(scope.tables)} scoped table(s)"
        )
        parts = [
            f"Analyzed {table_word} with {len(items)} completed check(s).",
            f"{counts['needs_review']} need review",
            f"{counts['clear']} were clear",
        ]
        if counts["errors"]:
            parts.append(f"{counts['errors']} could not run")
        summary = " ".join([parts[0], "; ".join(parts[1:]) + "."])
        issue_items = [
            item
            for item in items
            if item["classification"] in {"exception", "unusual", "execution_error"}
        ]
        issue_items.sort(
            key=lambda item: (
                {"exception": 0, "unusual": 1, "execution_error": 2}.get(
                    item["classification"], 9
                ),
                str(item.get("title") or "").casefold(),
                str(item.get("analysis_id") or ""),
            )
        )
        return {
            "status": (
                "completed_with_issues"
                if counts["needs_review"] or counts["errors"]
                else "completed"
            ),
            "headline": "Data analysis complete",
            "summary": summary,
            "metrics": [
                {"label": "Tables", "value": len(scope.tables)},
                {"label": "Checks executed", "value": len(items)},
                {"label": "Needs review", "value": counts["needs_review"]},
                {"label": "Clear", "value": counts["clear"]},
                {"label": "Execution errors", "value": counts["errors"]},
            ],
            "highlights": [
                {
                    "severity": (
                        "error"
                        if item["classification"] == "execution_error"
                        else "warning"
                    ),
                    "label": item["title"],
                    "detail": item.get("error")
                    or item.get("verdict_text")
                    or f"{item.get('row_count', 0)} result row(s).",
                    "artifact_ref": f"analysis:{item['analysis_id']}",
                }
                for item in issue_items[:3]
            ],
            "artifact_refs": [
                f"analysis:{item['analysis_id']}" for item in items
            ],
        }

    # ------------------------------------------------------------- provenance
    def _analysis_record(self) -> dict:
        return self.run.setdefault("analysis", {"relationships": []})

    def relationship_records(self) -> list[dict]:
        """The relationship evidence this run diagnosed, in pair order."""

        return list(self._analysis_record().get("relationships") or [])

    def _record_relationships(self, record: dict) -> None:
        """Persist one pair's diagnosis on the durable run record.

        Relationship evidence is deliberately not a workspace collection: it is
        a recomputable diagnostic about tables rather than an engagement
        artifact, and adding a collection for it would change the workspace
        schema. The run record is the durable, auditable home the framework
        already provides.
        """
        pair = (record["left"], record["right"])
        relationships = [
            item
            for item in self.relationship_records()
            if (item.get("left"), item.get("right")) != pair
        ]
        relationships.append(record)
        relationships.sort(key=lambda item: (str(item.get("left")), str(item.get("right"))))
        self._analysis_record()["relationships"] = relationships
        self.save()

    def _pair_record(self, left: str, right: str) -> dict:
        """This run's evidence for a pair, diagnosing it now if it has none.

        A run that reused ``data.relationships_inferred`` (every scoped pair was
        already joined) never scheduled the diagnosis, so a later capability
        that still needs the evidence recomputes it. The computation is
        deterministic and read-only, so recomputing is a cost, never a
        divergence.
        """
        wanted = {left, right}
        for item in self.relationship_records():
            if {str(item.get("left")), str(item.get("right"))} == wanted:
                return item
        record = infer_relationship(self.ws, left, right)
        self._record_relationships(record)
        return record

    @staticmethod
    def _parent(unit: dict, prefix: str) -> str:
        return next(
            ref.split(":", 1)[1]
            for ref in unit["parent_refs"]
            if ref.startswith(f"{prefix}:")
        )

    # --------------------------------------------------- deterministic commits
    def _commit_with_receipt(
        self,
        *,
        capability: workflow.Capability,
        unit: dict,
        executor_id: str,
        proposal: dict,
        target: object,
        expected_parents: dict[str, str],
    ) -> ExecutorReceipt:
        """Run one registered executor and persist its durable receipt.

        Deterministic units still get the pipeline's durability guarantees, and
        they get them from the pipeline itself rather than from a second
        implementation: ``UnitPipeline.commit_local`` persists the locally derived
        proposal before the commit, reconciles an interrupted commit instead of
        repeating it, and writes the receipt to the same
        ``receipts/<unit_id>.json`` sidecar the model-backed path uses.
        """
        assert self.unit_pipeline is not None
        request = UnitPipelineRequest(
            capability_id=capability.id,
            unit_id=unit["id"],
            worker_id="",
            executor_id=executor_id,
            unit_input={},
            activity={"artifact_refs": list(unit.get("parent_refs") or [])},
            expected_revision=self.ws.revision,
            expected_parents=expected_parents,
            capability_definition_hash=workflow.capability_definition_hash(capability),
        )

        def record(field: str):
            def persist(reference) -> None:
                unit[field] = dict(reference)
                self.save()

            return persist

        try:
            outcome = self.unit_pipeline.commit_local(
                request,
                proposal=proposal,
                target=target,
                on_proposal_persisted=record("proposal_sidecar"),
                on_receipt_persisted=record("receipt_sidecar"),
            )
        except UnitPipelineConflict as error:
            raise DeterministicCommitConflict(str(error), self.ws.revision) from error
        assert outcome.receipt is not None
        return outcome.receipt

    # ------------------------------------------- data.relationships_inferred
    def _bind_relationships(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``data.relationships_inferred``.

        One unit diagnoses one table pair with local Polars evidence and records
        the aggregate metrics on the run. No model is consulted, no relationship
        fact is generated, and nothing is committed to the workspace — the
        materialization decision belongs to ``data.joins_ready``.
        """
        self.ws = subject
        left = unit["parent_refs"][0].split(":", 1)[1]
        right = unit["parent_refs"][1].split(":", 1)[1]
        task = self.add_task(
            "relationships", "workflow:relationships", "Table relationships"
        )
        self.task_status(task, "running")
        record = infer_relationship(self.ws, left, right)
        self._record_relationships(record)
        refs = tuple(str(item["ref"]) for item in record["candidates"])
        self.emit(
            "relationship_evidence",
            {
                "left": left,
                "right": right,
                "candidates": len(record["candidates"]),
                "strong": len(record["strong"]),
                "moderate": len(record["moderate"]),
            },
        )
        self.task_detail(
            task,
            f"{left} ↔ {right}: {len(record['strong'])} strong, "
            f"{len(record['moderate'])} moderate candidate(s).",
        )
        self.task_status(task, "completed")
        if not record["candidates"] and record["join"] is None:
            # Unrelated table pairs are normal in a broad workspace. The
            # relationship record retains the evidence locally, but there is
            # nothing useful to surface to the auditor or narrate repeatedly.
            return DeterministicUnitResult("succeeded", refs)
        return DeterministicUnitResult("succeeded", refs)

    # ------------------------------------------------------ data.joins_ready
    def _bind_join(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``data.joins_ready``.

        Permission mode asks the auditor to select among competing candidates.
        Auto mode instead materializes the highest-ranked safe candidate — a
        strong candidate first, then a moderate one — and records that choice.
        Every safe candidate has a unique right key and no material row
        multiplication; candidates below that threshold are never applied.
        """
        self.ws = subject
        left = unit["parent_refs"][0].split(":", 1)[1]
        right = unit["parent_refs"][1].split(":", 1)[1]
        existing = pair_join(self.ws, left, right)
        if existing is not None:
            return DeterministicUnitResult(
                "succeeded", (join_ref(str(existing["name"])),)
            )
        task = self.add_task("joins", "workflow:joins", "Materialized joins")
        self.task_status(task, "running")
        record = self._pair_record(left, right)
        strong = list(record["strong"])
        moderate = list(record["moderate"])
        if not strong and not moderate:
            self.task_status(task, "completed")
            return DeterministicUnitResult("skipped")

        candidate = None
        joinable = strong or moderate
        ranked = sorted(joinable, key=join_diagnostics.evidence_rank)
        if len(strong) == 1:
            candidate = strong[0]
        elif self.run.get("mode") == "permission":
            # Only permission mode stops to ask. Auto mode is an unattended
            # run: leaving a pair unjoined there does not protect anyone, it
            # just removes the frame every downstream analysis needed.
            candidate = self._approve_join(task, left, right, joinable)
        elif ranked:
            candidate = ranked[0]
            diagnostics = candidate["diagnostics"]
            self.warn(
                "Auto-selected the best-evidenced join candidate for "
                f"'{left}' and '{right}': "
                f"{candidate['left_on'][0]} → {candidate['right_on'][0]} "
                f"({candidate['strength']} evidence, "
                f"{diagnostics['match_rate']:.0%} match rate, "
                f"row multiplication {diagnostics['row_multiplication']})."
            )
            if not join_diagnostics.decisive(joinable):
                # Several keys diagnose identically — a table reaching the same
                # person dimension as requester and as approver. Rank order
                # picks one, but the choice decides what every analysis on this
                # frame measures, so the alternatives are named rather than
                # silently discarded.
                alternatives = ", ".join(
                    f"{item['left_on'][0]} → {item['right_on'][0]}"
                    for item in ranked[1:]
                    if join_diagnostics.evidence_rank(item)
                    == join_diagnostics.evidence_rank(candidate)
                )
                self.warn(
                    f"'{left}' and '{right}' are related by more than one key with "
                    f"identical evidence; {alternatives} would each give a "
                    "different frame and were not materialized. Review the applied "
                    "relationship before relying on analyses built on it."
                )
        if candidate is None:
            reported = ranked
            self.warn(
                f"{len(reported)} join candidate(s) for '{left}' and '{right}' need "
                "confirmation; none was applied."
            )
            self.task_status(task, "skipped")
            return DeterministicUnitResult(
                "awaiting_confirmation",
                tuple(str(item["ref"]) for item in reported),
                AMBIGUOUS_RELATIONSHIP,
            )

        expected = parent_hashes(self.ws, list(unit["parent_refs"]))
        target = JoinExecutorTarget(self.ws, self.run["id"], left, right)
        try:
            receipt = self._commit_with_receipt(
                capability=capability,
                unit=unit,
                executor_id="analysis.join",
                proposal={
                    "join": {
                        key: candidate[key]
                        for key in ("left", "right", "how", "left_on", "right_on")
                    }
                },
                target=target,
                expected_parents=expected,
            )
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        self.ws = target.workspace
        name = str(receipt.output["name"])
        self._record_relationships({**record, "join": name})
        self.record_artifact(
            "join", name, relationship_ref(candidate), "created", task
        )
        self.task_status(task, "completed")
        return DeterministicUnitResult("succeeded", tuple(receipt.artifact_refs))

    def _approve_join(
        self,
        task: dict,
        left: str,
        right: str,
        candidates: list[dict],
    ) -> dict | None:
        """Ask the auditor which diagnosed relationship to materialize."""

        accepted = self.request_approval(
            "joins",
            task,
            [
                self.proposal_item(
                    f"{item['left']}.{item['left_on'][0]} → "
                    f"{item['right']}.{item['right_on'][0]}",
                    f"{item['strength']} evidence: "
                    f"{item['diagnostics']['match_rate']:.0%} match rate, "
                    f"row multiplication {item['diagnostics']['row_multiplication']}.",
                    {
                        key: item[key]
                        for key in ("left", "right", "how", "left_on", "right_on")
                    },
                    {"diagnostics": dict(item["diagnostics"])},
                )
                for item in candidates
            ],
        )
        if not accepted:
            return None
        spec = dict(accepted[0]["spec"])
        return next(
            (
                item
                for item in candidates
                if item["left"] == spec.get("left")
                and item["right"] == spec.get("right")
                and list(item["left_on"]) == list(spec.get("left_on") or [])
                and list(item["right_on"]) == list(spec.get("right_on") or [])
            ),
            None,
        )

    # -------------------------------------------- analysis.definitions_ready
    def _bind_definitions(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Bind one frame's analysis-definition unit to the shared pipeline.

        The domain-neutral scheduler owns manifest/proposal/receipt persistence,
        proposal reuse, approval, and readiness reevaluation. This binding
        supplies only the frame-scoped declared context, the definition commit
        target, per-analysis approval items, and the post-commit bookkeeping.
        """
        self.ws = subject
        parent_ref = str(unit["parent_refs"][0])
        target_frame = parent_ref.split(":", 1)[1]
        table_scope = self.scope()
        existing = [
            item
            for item in agent_analyses(self.ws, table_scope)
            if str(item.get("table") or "") == target_frame
        ]
        if existing and workflow_scope(self.run).get("generation_mode") != "force":
            # The frame already carries workflow-authored definitions. Units are
            # durable once materialized, so a stage re-run must not spend a
            # provider turn re-deriving what already exists; only an explicit
            # regeneration does that.
            return DeterministicUnitResult(
                "succeeded",
                tuple(analysis_ref(str(item["id"])) for item in existing),
            )
        expected = parent_hashes(self.ws, [parent_ref])
        target = AnalysisDefinitionExecutorTarget(
            self.ws,
            self.run["id"],
            target_frame,
            parent_ref,
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        task = self.add_task(
            "analysis_definitions", "workflow:analysis_definitions", "Analysis definitions"
        )

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                analysis_definition_scope(
                    self.ws,
                    target_frame,
                    related=[
                        name for name in table_scope.targets if name != target_frame
                    ],
                    relationships=self.relationship_records(),
                ),
            )

        def approval_provider(proposal):
            proposed = [
                self.proposal_item(
                    str(item.get("title")),
                    f"Rerunnable {item.get('kind')} analysis for {target_frame}.",
                    dict(item),
                )
                for item in proposal.get("analyses") or []
            ]
            accepted = self.request_approval("analysis_definitions", task, proposed)
            analyses = [dict(item["spec"]) for item in accepted]
            return {"analyses": analyses} if analyses else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is None:
                return
            output = outcome.receipt.output
            for item in output.get("analyses") or []:
                self.record_artifact(
                    "analysis",
                    str(item["id"]),
                    str(item.get("semantic_id") or ""),
                    str(item.get("action") or "created"),
                    task,
                )
            for preserved in output.get("preserved") or []:
                self.warn(f"Preserved auditor-owned analysis '{preserved}'.")
            for covered in output.get("covered") or []:
                self.warn(
                    f"Analysis '{covered}' already computes this from the same "
                    "columns on a related frame; not duplicated here."
                )
            self.task_status(task, "completed")

        def conflict_handler(_stage, _unit, error) -> tuple[str, str] | None:
            if str(error) == ANALYSIS_COVERED_ELSEWHERE:
                # Nothing to write because everything proposed is already saved
                # against a frame built from the same tables. That is the
                # de-duplication working, not an outcome needing an auditor.
                return (
                    "skipped",
                    "Every proposed analysis is already covered by a related frame.",
                )
            if str(error) != AUDITOR_ANALYSIS_PRESERVED:
                return None
            return (
                "awaiting_confirmation",
                "Auditor-owned analysis definitions were preserved.",
            )

        def failure_handler(_stage, _unit, error) -> tuple[str, str] | None:
            # The worker had its repair turn and still found nothing this frame
            # computes that its join family does not already hold. A joined
            # frame that adds no analysis of its own is a real answer about the
            # data, so the unit settles instead of failing the run.
            if NOTHING_NEW_TO_ANALYSE not in str(error):
                return None
            return (
                "skipped",
                "Every analysis this frame supports is already saved against a "
                "frame built from the same tables.",
            )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="analysis.definitions",
                executor_id="analysis.definitions",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": [parent_ref],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "analysis_definitions"
                    if self.run["mode"] == "permission"
                    else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            # Definitions fan out one unit per scoped frame, so this capability's
            # readiness only holds once every unit has committed.
            readiness_provider=None,
            on_committed=on_committed,
            conflict_handler=conflict_handler,
            failure_handler=failure_handler,
        )

    # --------------------------------------------------- analysis.summarized
    def _bind_summary(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind the one analysis-summary unit to the shared pipeline.

        Unlike every other unit in this graph the memo is workspace-wide, not
        frame-scoped: it is the one place the results are read together, which
        is the only vantage from which the engagement-level statements in it
        can be made at all.
        """
        self.ws = subject
        expected = parent_hashes(self.ws, [ANALYSIS_SUMMARY_REF])
        target = AnalysisSummaryExecutorTarget(self.ws, self.run["id"])
        task = self.add_task(
            "analysis_summary", "workflow:analysis_summary", "Analysis summary"
        )

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                analysis_summary_scope(self.ws),
            )

        def approval_provider(proposal):
            accepted = self.request_approval(
                "analysis_summary",
                task,
                [
                    self.proposal_item(
                        "Analysis summary",
                        "The EDA summary written from the recorded results.",
                        dict(proposal),
                    )
                ],
            )
            return dict(accepted[0]["spec"]) if accepted else None

        def on_committed(_stage, _unit, outcome) -> None:
            self.ws = target.workspace
            if outcome.receipt is None:
                return
            output = outcome.receipt.output
            self.record_artifact(
                "analysis_summary", "current", "analysis_summary:current",
                str(output.get("action") or "summarized"), task,
            )
            self.task_detail(
                task,
                f"{output.get('characters', 0)} characters, "
                f"{output.get('cited', 0)} result(s) cited.",
            )
            self.task_status(task, "completed")
            self.emit(
                "workspace_changed",
                {"kind": "analysis_summary", "id": "current", "action": "summarized"},
            )

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="analysis.summary",
                executor_id="analysis.summary",
                unit_input={
                    "kind": unit.get("kind"),
                    "input_sha1": unit.get("input_sha1"),
                    "parent_refs": list(unit.get("parent_refs") or []),
                },
                activity={
                    "artifact_refs": [ANALYSIS_SUMMARY_REF],
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents=expected,
                capability_definition_hash=workflow.capability_definition_hash(capability),
                approval_kind=(
                    "analysis_summary" if self.run["mode"] == "permission" else None
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=unit.get("receipt_sidecar"),
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=target,
            approval_provider=(
                approval_provider if self.run["mode"] == "permission" else None
            ),
            readiness_provider=None,
            on_committed=on_committed,
        )

    # ----------------------------------------------------- analysis.executed
    def _bind_execution(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> DeterministicUnitResult:
        """Deterministic execution for ``analysis.executed``.

        The saved spec is recomputed locally through the same service the
        Analysis tab uses — analytics through the registry, Polars through the
        guarded sandbox — and only the bounded result contract is committed. A
        spec that errors is not a run failure: it is a definition the auditor
        needs to look at, so the unit settles as ``awaiting_confirmation`` with
        the error durably recorded on the analysis.
        """
        self.ws = subject
        analysis_id = self._parent(unit, "analysis")
        analysis = next(
            (item for item in self.ws.analyses if str(item.get("id")) == analysis_id),
            None,
        )
        if analysis is None:
            return DeterministicUnitResult(
                "failed", error=f"Analysis '{analysis_id}' no longer exists."
            )
        task = self.add_task(
            "analysis_execution", "workflow:analysis_execution", "Analysis results"
        )
        self.task_status(task, "running")
        executed = run_analysis(self.ws, analysis, run_id=self.run["id"])
        result = executed.result
        expected = parent_hashes(self.ws, [analysis_ref(analysis_id)])
        target = AnalysisExecutionExecutorTarget(self.ws, self.run["id"], analysis_id)
        try:
            receipt = self._commit_with_receipt(
                capability=capability,
                unit=unit,
                executor_id="analysis.execution",
                # The flagged rows are part of the proposal, not a side effect
                # of it: persisting them with the result is what lets a resumed
                # commit restore the same evidence without re-running Polars.
                proposal={"result": result, "evidence": executed.evidence},
                target=target,
                expected_parents=expected,
            )
        except WorkspaceConflict as error:
            return DeterministicUnitResult("conflict", error=str(error))
        self.ws = target.workspace
        self.emit(
            "workspace_changed",
            {"kind": "analysis", "id": analysis_id, "action": "executed"},
        )
        refs = tuple(receipt.artifact_refs)
        title = analysis.get("title") or analysis_id
        if result["status"] != "ok":
            self.warn(f"Analysis '{title}' did not run: {result['error']}")
            self.task_detail(task, f"{title}: {result['error']}")
            self.task_status(task, "completed")
            return DeterministicUnitResult(
                "awaiting_confirmation", refs, str(result["error"])
            )
        self.task_detail(task, f"{title}: {result['row_count']} row(s).")
        self.task_status(task, "completed")
        return DeterministicUnitResult("succeeded", refs)

    # ------------------------------------------------------------ checkpoint
    def _scope_checkpoint(self) -> None:
        """Settle an ambiguous table scope before any capability fans out.

        The scope is ambiguous only when the request named nothing and the
        workspace holds more tables than the bounded fallback analyses. In
        permission mode that is a question for the auditor; in auto mode the
        bounded deterministic selection stands and is reported as a warning, so
        the run never silently analyses an arbitrary subset.

        The checkpoint is declared on two capabilities but settles the scope once
        per run, so a workflow that reuses its first outcome still asks before
        the frames it will spend model turns on are chosen.
        """
        record = self._analysis_record()
        if record.get("scope_settled"):
            return
        table_scope = self.scope()
        if table_scope.ambiguity is None:
            return
        record["scope_settled"] = True
        if self.run.get("mode") != "permission":
            self.warn(table_scope.ambiguity)
            self.save()
            return
        interaction = next(
            (
                item
                for item in self.run.get("interactions") or []
                if item.get("type") == "analysis_scope"
                and item.get("status") == "pending"
            ),
            None,
        )
        eligible = sorted(str(item.get("name")) for item in self.ws.tables)
        if interaction is None:
            interaction = {
                "id": f"int_{uuid.uuid4().hex[:12]}",
                "action_id": "workflow:analysis_scope",
                "type": "analysis_scope",
                "prompt": (
                    "Which tables should the analysis cover? "
                    f"Choose up to {MAX_SCOPE_TABLES}."
                ),
                "options": eligible,
                "payload": {
                    "eligible_tables": eligible,
                    "default_tables": list(table_scope.tables),
                    "max_tables": MAX_SCOPE_TABLES,
                    "reason": table_scope.ambiguity,
                },
                "policy_reason": (
                    "The requested scope is ambiguous and materially changes the "
                    "analysis."
                ),
                "status": "pending",
                "response": None,
                "actor": None,
                "created_at": store.utcnow(),
                "resolved_at": None,
            }
            self.run.setdefault("interactions", []).append(interaction)
            self.run["workflow"]["pending_checkpoint"] = interaction["id"]
            self.save()
            self.emit("checkpoint_request", {"interaction": interaction})
        response = self.runtime.wait_for_interaction(interaction)
        chosen = [
            str(value).strip()
            for value in (response.get("tables") or response.get("options") or [])
            if str(value).strip() in set(eligible)
        ]
        if not chosen:
            text = str(response.get("text") or "")
            chosen = [name for name in eligible if name in text]
        if chosen:
            scope = dict((self.run["workflow"].get("scope") or {}))
            scope["tables"] = chosen[:MAX_SCOPE_TABLES]
            self.run["workflow"]["scope"] = scope
        else:
            self.warn(table_scope.ambiguity)
        self.run["workflow"]["pending_checkpoint"] = None
        self.runtime.resolve_interaction(interaction, response)
        self.emit(
            "checkpoint_resolved",
            {"interaction_id": interaction["id"], "count": len(chosen)},
        )

    # ---------------------------------------------------------------- finish
    def _finish_projection(
        self,
        subject: Workspace,
        _workflow_state: dict,
        stages: tuple[dict, ...],
    ) -> FinishProjection:
        """Close the run on real analysis outcomes, not on unit bookkeeping."""

        self.ws = subject
        table_scope = self.scope()
        units = [unit for stage in stages for unit in stage.get("units") or []]
        failed = sum(unit.get("status") in {"failed", "conflict"} for unit in units)
        open_units = sum(
            unit.get("status")
            in {"blocked", "awaiting_input", "awaiting_confirmation"}
            for unit in units
        )
        analyses = agent_analyses(subject, table_scope)
        executed = [
            item for item in analyses if analysis_result_state(subject, item) == "current"
        ]
        next_outcomes = list(unsettled_capabilities(stages))
        self.run["workflow"]["workspace_revision"] = subject.revision
        terminal = fold_terminal_status(stages)
        if failed:
            self.run["error"] = first_unit_error(
                stages, "One or more analysis units failed."
            )
        summary = narration.summary_markdown(
            "Data analysis",
            [
                ("Tables analysed", list(table_scope.tables)),
                ("Joins available", len(table_scope.joins)),
                ("Analysis definitions", len(analyses)),
                ("Analyses executed", len(executed)),
                ("Failed or conflicting units", failed),
                ("Open workflow units", open_units),
            ],
        )
        return FinishProjection(
            next_outcomes=tuple(dict.fromkeys(next_outcomes)),
            summary_markdown=summary,
            terminal_status=terminal,
        )


_PARTIAL_DEPENDENCIES = {
    "data.joins_ready": {"data.relationships_inferred"},
    "analysis.definitions_ready": {"data.joins_ready"},
    "analysis.executed": {"analysis.definitions_ready"},
    # One procedure that would not execute must not withhold the memo. A
    # summary written over the results that did land is the useful artifact,
    # and the procedure that failed is itself reported in "further work".
    "analysis.summarized": {"analysis.executed"},
}


def build_analysis_workflow_runner(
    workspace: Workspace,
    run: dict,
    handle,
    *,
    runtime: RunRuntime | None = None,
    context_resolver: ContextResolver | None = None,
) -> WorkflowRunner:
    """Compose the domain-neutral scheduler with the analysis bindings."""

    adapter = AnalysisWorkflowExecution(
        workspace,
        run,
        handle,
        runtime=runtime,
        context_resolver=context_resolver,
    )
    unit_pipeline = UnitPipeline(
        runtime=adapter.runtime,
        gateway=adapter.model_gateway,
        workers=WORKERS,
        executors=EXECUTORS,
        sidecars=adapter.sidecars,
    )
    adapter.unit_pipeline = unit_pipeline
    _PIPELINE_BINDERS = {
        "analysis.definitions_ready": (
            adapter._bind_definitions,
            {"worker": "analysis.definitions", "executor": "analysis.definitions"},
        ),
        "analysis.summarized": (
            adapter._bind_summary,
            {"worker": "analysis.summary", "executor": "analysis.summary"},
        ),
    }
    _DETERMINISTIC_BINDERS = {
        "data.relationships_inferred": (
            adapter._bind_relationships,
            {"deterministic": "analysis.relationships"},
        ),
        "data.joins_ready": (
            adapter._bind_join,
            {"deterministic": "analysis.join"},
        ),
        "analysis.executed": (
            adapter._bind_execution,
            {"deterministic": "analysis.execution"},
        ),
    }
    executions = CapabilityExecutionRegistry()
    for capability in ANALYSIS_REGISTRY.all():
        pipeline_binding = _PIPELINE_BINDERS.get(capability.id)
        if pipeline_binding is not None:
            binder, identity = pipeline_binding
            executions.register(
                CapabilityExecution(
                    capability_id=capability.id,
                    implementation_hash=workflow.canonical_sha256(
                        {"capability": capability.id, **identity}
                    ),
                    pipeline_binder=binder,
                )
            )
            continue
        binder, identity = _DETERMINISTIC_BINDERS[capability.id]
        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash=workflow.canonical_sha256(
                    {"capability": capability.id, **identity}
                ),
                deterministic_executor=binder,
            )
        )

    checkpoint_handlers = {
        ANALYSIS_SCOPE_CHECKPOINT: adapter._scope_checkpoint,
    }

    def before_stage(
        subject: Workspace,
        capability: workflow.Capability,
        _stage: dict,
    ) -> None:
        adapter.ws = subject
        checkpoint = STAGE_CHECKPOINTS.get(capability.id)
        if checkpoint is not None:
            checkpoint_handlers[checkpoint]()

    def dependency_policy(
        capability_id: str,
        dependency_id: str,
        _dependency_status: str,
    ) -> bool:
        # Every edge in this graph is partial. A pair with no safe join, an
        # ambiguous relationship left for confirmation, or one frame whose
        # definitions could not be produced must not stop the analysis the other
        # frames still support: each later capability re-expands against what
        # the earlier one actually committed.
        return dependency_id in _PARTIAL_DEPENDENCIES.get(capability_id, set())

    scheduler = WorkflowRunner(
        subject=workspace,
        run=run,
        runtime=adapter.runtime,
        registry=ANALYSIS_REGISTRY,
        executions=executions,
        unit_pipeline=unit_pipeline,
        refresh_subject=lambda: refresh_workspace(adapter),
        refresh_limits=lambda _subject: adapter._refresh_dynamic_limits(),
        dependency_policy=dependency_policy,
        before_stage=before_stage,
        milestone_projector=adapter.milestone_projection,
        finish_evaluator=adapter._finish_projection,
    )
    adapter.scheduler = scheduler
    scheduler.execution_adapter = adapter
    return scheduler


__all__ = [
    "AnalysisWorkflowExecution",
    "build_analysis_workflow_runner",
]
