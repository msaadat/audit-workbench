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

from ..text import counted, verb
from ..workspace_transactions import parent_hashes
from ..workspaces import Workspace, WorkspaceConflict
from . import joins as join_diagnostics, narration, probes, register, store, workflow
from .base import BaseRunner
from .capabilities.analysis import (
    ANALYSIS_SCOPE_CHECKPOINT,
    ANALYSIS_SUMMARY_REF,
    STAGE_CHECKPOINTS,
    MAX_SCOPE_TABLES,
    TableScope,
    agent_analyses,
    definable_targets,
    frame_ref,
    pair_join,
    resolve_table_scope,
)
from .capabilities import ANALYSIS_REGISTRY
from .context import (
    ContextResolver,
    analysis_definition_scope,
    analysis_reading_scope,
    join_utility_scope,
    analysis_summary_scope,
    promotion_scope,
)
from .executors import EXECUTORS, ExecutorReceipt
from .executors.analysis import (
    AMBIGUOUS_RELATIONSHIP,
    ANALYSIS_COVERED_ELSEWHERE,
    AUDITOR_ANALYSIS_PRESERVED,
    AnalysisDefinitionExecutorTarget,
    AnalysisExecutionExecutorTarget,
    AnalysisRegisterExecutorTarget,
    AnalysisSummaryExecutorTarget,
    PromotionExecutorTarget,
    JoinExecutorTarget,
    NO_INFORMATIVE_ANALYSIS,
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
        "join_utility": "Join utility selection",
        "joins": "Materialized joins",
        "analysis_register": "Assertion register",
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

        Three analysis capabilities are model-backed. The register spends one
        turn over the whole scope, definitions spends one per frame the register
        placed an assertion on, and the summary spends one more for the whole
        workspace. The budget still scales with the scope rather than with the
        register, because it is refreshed before every stage and the register
        does not exist when the first refresh runs — sizing it on the frames is
        the bound that holds at every point in the run.

        The register's own turn is the largest single prompt in the graph: it
        carries every frame's columns and every measured nomination at once. It
        is charged the same way as any other turn, and the token allowance below
        is per-turn headroom rather than a per-turn expectation.
        """
        table_scope = self.scope()
        targets = max(1, len(table_scope.targets))
        # Bounded turns that are not per frame: the join-utility gate, the
        # register's reading turn, the memo, and a repair turn for each, all
        # charged like any other model call.
        calculated = 16 + 2 * targets
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
                    f"Compared {counted(len(scope.pairs()), 'table pair')} across "
                    f"{counted(len(scope.tables), 'scoped table')}. "
                    f"{counted(len(scope.joins), 'join')} {verb(len(scope.joins), 'is', 'are')} available; "
                    f"{counted(ambiguous, 'pair')} still {verb(ambiguous)} confirmation."
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
            else counted(len(scope.tables), "scoped table")
        )
        parts = [
            f"Analysed {table_word} with {counted(len(items), 'completed check')}.",
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
                    or f"{counted(item.get('row_count', 0), 'result row')}.",
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

    def join_utility_decisions(self) -> dict[str, dict]:
        return {
            str(item.get("ref")): dict(item)
            for item in self._analysis_record().get("join_utility") or []
            if str(item.get("ref") or "")
        }

    def retained_hypotheses(self) -> list[dict]:
        """The gate's retained tests, each naming the tables it reads."""

        return [
            item
            for item in self.join_utility_decisions().values()
            if item.get("decision") == "retain" and item.get("requires")
        ]

    def _warn_untestable_hypotheses(self) -> None:
        """Report retained tests no materialized frame can carry."""

        lineages = [
            join_diagnostics.frame_lineage(self.ws, name)
            for name in self.ws.table_names()
        ]
        for item in self.retained_hypotheses():
            required = {str(name) for name in item.get("requires") or ()}
            if any(required <= lineage for lineage in lineages):
                continue
            self.warn(
                "No materialized frame brings together "
                f"{', '.join(sorted(required))}, so this test was prepared "
                f"nowhere: {item.get('hypothesis')}"
            )

    def _warn_unjoined_engagement(self) -> None:
        """Say plainly when an engagement reached analysis with no join at all.

        The per-pair rejections are already reported, but nine of them read as
        nine local judgments rather than as the one fact they add up to: every
        cross-table question in the engagement is now unanswerable, and the
        analysis will run over base tables as though the tables were unrelated.
        A run that did this reported ``completed``.
        """
        scope = self.scope()
        if len(scope.tables) < 2 or self.ws.joins:
            return
        rejected = [
            item
            for item in self.join_utility_decisions().values()
            if item.get("decision") == "reject"
        ]
        if not rejected:
            return
        self.warn(
            f"No join was materialized anywhere in this engagement: all "
            f"{counted(len(rejected), 'diagnosed relationship')} were judged to "
            f"support no audit test. The analysis covers "
            f"{counted(len(scope.tables), 'table')} in isolation, and every test "
            "that would compare one against another — an amount against its "
            "authorisation, a date against the document it follows — is out of "
            "reach for this run."
        )

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

    def frame_probes(self, frame: str) -> list[dict]:
        """What this frame's own data already asserts, measured once per run.

        Computed lazily and cached on the run for the same reason
        :meth:`_pair_record` recomputes relationship evidence: the sweep is
        deterministic and read-only, so computing it where it is first needed is
        a cost and never a divergence. Caching matters because a frame's
        nominations are read by its definition turn and by nothing else, while
        the sweep itself is several Polars passes over every column pair.

        This is deliberately not its own capability yet. The stage that will own
        it reads every frame at once rather than one at a time, so declaring a
        per-frame capability now would be re-homed by the next change with no
        behaviour to show for it.
        """
        record = self._analysis_record()
        cached = record.setdefault("probes", {})
        if frame in cached:
            return list(cached[frame])
        found = probes.probe_frame(self.ws, frame)
        cached[frame] = found
        self.save()
        return list(found)

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
            f"{counted(len(record['moderate']), 'moderate candidate')}.",
        )
        self.task_status(task, "completed")
        if not record["candidates"] and record["join"] is None:
            # Unrelated table pairs are normal in a broad workspace. There is
            # nothing useful to surface to the auditor or narrate repeatedly,
            # but the pair is now answered and must be recorded as such — left
            # unrecorded it reads as undiagnosed on every later run, and the
            # analysis chain waits on a diagnosis that has already happened.
            join_diagnostics.settle_pair(
                self.ws, left, right, "no candidate key was found"
            )
            return DeterministicUnitResult("succeeded", refs)
        return DeterministicUnitResult("succeeded", refs)

    # ------------------------------------------------------ data.joins_ready
    def _bind_join_utility(
        self, subject: Workspace, run: dict, capability: workflow.Capability,
        stage: dict, unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Persist one row-free LLM utility decision catalog before any join."""
        self.ws = subject
        scope = join_utility_scope(self.ws, self.relationship_records())
        catalog = next(
            (item.source for item in scope.candidates.get("join_candidates") or ()),
            {},
        )
        if not catalog.get("candidates"):
            # Local diagnosis found nothing safe enough to be worth judging.
            # There is no decision to make, and a provider turn spent asking
            # about an empty catalog would answer a question nobody asked.
            return DeterministicUnitResult("skipped")
        task = self.add_task("join_utility", "workflow:join_utility", "Join utility selection")
        self.task_status(task, "running")

        def context_provider():
            return self.context_resolver.resolve(self.ws, capability, unit, scope)

        def on_committed(_stage, _unit, outcome):
            proposal_record = self.sidecars.load_proposal(unit["id"], outcome.proposal_reference) or {}
            proposal = dict(proposal_record.get("proposal") or {})
            self._analysis_record()["join_utility"] = list(proposal.get("decisions") or [])
            self.save()
            retained = sum(item.get("decision") == "retain" for item in proposal.get("decisions") or [])
            self.task_detail(task, f"Retained {counted(retained, 'audit-useful relationship')}.")
            self.task_status(task, "completed")
            return DeterministicUnitResult("succeeded")

        def failure_handler(_stage, _unit, error) -> tuple[str, str] | None:
            # The unit's own status carries the outcome; this only closes the
            # task, which would otherwise sit running for the rest of the run
            # while every join stage below it reports being blocked by it.
            self.task_status(task, "failed", str(error))
            return None

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id, unit_id=unit["id"],
                worker_id="analysis.join_utility", executor_id=None,
                unit_input={"parent_refs": list(unit.get("parent_refs") or [])},
                activity={"artifact_refs": list(unit.get("parent_refs") or []), "task_id": task["id"]},
                expected_revision=self.ws.revision, expected_parents={},
                capability_definition_hash=workflow.capability_definition_hash(capability),
                proposal_reference=unit.get("proposal_sidecar"), receipt_reference=None,
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(capability, manifest),
            target=None, on_committed=on_committed, failure_handler=failure_handler,
        )

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
            join_diagnostics.settle_pair(
                self.ws, left, right, "no candidate key was found"
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult("skipped")

        candidate = None
        decisions = self.join_utility_decisions()
        # A technical relationship is not an audit procedure. Only the
        # proposal-only utility gate may admit one to durable materialization,
        # and it judges the strong and the moderate candidates together: a gate
        # that rejects the best-evidenced key has not thereby rejected a weaker
        # key it retained for a reason it wrote down.
        diagnosed = strong + moderate
        retained = [
            item
            for item in diagnosed
            if decisions.get(str(item.get("ref")), {}).get("decision") == "retain"
        ]
        if not retained and self._chain_hop_admitted(left, right, decisions):
            # A chain pair does not exist when the gate runs: the frame on one
            # side of it is materialized by the wave the gate authorized. What
            # the gate did judge is the relationship between the base tables
            # this hop brings together, so a hop that realizes an admitted
            # relationship on a wider frame inherits its admission.
            retained = diagnosed
        strong = [item for item in retained if item.get("strength") == "strong"]
        moderate = [item for item in retained if item.get("strength") == "moderate"]
        joinable = strong or moderate
        if not joinable:
            # An unjoined pair is a finding about the data, so it is reported
            # rather than left for a reader to infer from a frame that is not
            # there. The gate's own words are the explanation.
            #
            # The gate ruled; that ruling is the answer for this data. Recording
            # it is what stops a later run treating a relationship deliberately
            # declined as one nobody has looked at yet. Replacing either table
            # reopens it, and an auditor who wants the join regardless can still
            # build it in the join dialog.
            join_diagnostics.settle_pair(
                self.ws, left, right, "no relationship the utility gate would retain"
            )
            self._warn_gate_rejection(left, right, record, decisions)
            self.task_detail(
                task, f"'{left}' and '{right}': no relationship worth materializing."
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult("skipped")
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
        if candidate is None:
            reported = ranked
            self.warn(
                f"{counted(len(reported), 'join candidate')} for '{left}' and '{right}' need "
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
        # One pair materializes one join, so every other route to the same two
        # tables — the approver key where the requester key was applied — is
        # now unreachable. Naming them is what lets a reader tell an analysis
        # built on the intended relationship from one that merely joined.
        self._warn_gate_rejection(left, right, record, decisions, applied=candidate)
        self.task_status(task, "completed")
        return DeterministicUnitResult("succeeded", tuple(receipt.artifact_refs))

    def _chain_hop_admitted(
        self, left: str, right: str, decisions: dict[str, dict]
    ) -> bool:
        """Whether a chained pair extends a relationship the gate retained.

        Only chained pairs consult this. A pair of base tables was in the gate's
        catalog under its own refs, so silence about it is a rejection, not a
        gap — reading it as an admission would let a chain of one hop bypass the
        gate entirely.
        """

        left_lineage = join_diagnostics.frame_lineage(self.ws, left)
        right_lineage = join_diagnostics.frame_lineage(self.ws, right)
        if len(left_lineage) < 2 and len(right_lineage) < 2:
            return False
        admitted = {
            frozenset((str(item.get("left")), str(item.get("right"))))
            for record in self.relationship_records()
            for item in record.get("candidates") or []
            if decisions.get(str(item.get("ref")), {}).get("decision") == "retain"
        }
        return any(
            frozenset((base, other)) in admitted
            for base in left_lineage
            for other in right_lineage
        )

    def _warn_gate_rejection(
        self,
        left: str,
        right: str,
        record: dict,
        decisions: dict[str, dict],
        *,
        applied: dict | None = None,
    ) -> None:
        """Report the diagnosed relationships the utility gate did not admit."""

        rejected = [
            item
            for item in list(record["strong"]) + list(record["moderate"])
            if item is not applied
            and decisions.get(str(item.get("ref")), {}).get("decision") != "retain"
        ]
        if not rejected:
            return
        described = "; ".join(
            f"{item['left_on'][0]} → {item['right_on'][0]}"
            + (
                f" ({reason})"
                if (reason := str(
                    decisions.get(str(item.get("ref")), {}).get("rationale") or ""
                ).strip())
                else ""
            )
            for item in rejected
        )
        if applied is not None:
            self.warn(
                f"'{left}' and '{right}' are related by more than one key; only "
                f"{applied['left_on'][0]} → {applied['right_on'][0]} was "
                f"materialized. Not applied: {described}. Each would give a "
                "different frame, so review the applied relationship before "
                "relying on analyses built on it."
            )
            return
        self.warn(
            f"No join was materialized for '{left}' and '{right}': "
            f"{counted(len(rejected), 'diagnosed relationship')} {verb(len(rejected), 'was', 'were')} judged to support "
            f"no audit test. {described}"
        )

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

    # ------------------------------------------------ analysis.register_ready
    def register_floor(self) -> tuple[register.Nomination, ...]:
        """Every measured nomination in scope, deduplicated and ranked once.

        Cached on the run for the same reason ``frame_probes`` is: the sweep is
        deterministic and read-only, so computing it where it is needed is a
        cost and never a divergence — but it is several Polars passes per frame
        and the register reads every frame.
        """
        record = self._analysis_record()
        cached = record.get("register_floor")
        if cached is None:
            table_scope = self.scope()
            swept = {
                frame: self.frame_probes(frame)
                for frame in definable_targets(self.ws, table_scope)
            }
            floor = register.build_floor(self.ws, swept)
            record["register_floor"] = [
                {
                    "ref": item.ref,
                    "frame": item.frame,
                    "root": item.root,
                    "test": item.test,
                    "params": dict(item.params),
                    "family": item.family,
                    "signal": item.signal,
                    "tested": item.tested,
                    "flagged": item.flagged,
                    "reading": item.reading,
                    "semantic_id": item.semantic_id,
                    "also_on": list(item.also_on),
                    "unreferenced": item.unreferenced,
                }
                for item in floor
            ]
            self.save()
            return floor
        return tuple(
            register.Nomination(
                ref=str(item.get("ref") or ""),
                frame=str(item.get("frame") or ""),
                root=str(item.get("root") or ""),
                test=str(item.get("test") or ""),
                params=dict(item.get("params") or {}),
                family=str(item.get("family") or ""),
                signal=str(item.get("signal") or ""),
                tested=int(item.get("tested") or 0),
                flagged=int(item.get("flagged") or 0),
                reading=str(item.get("reading") or ""),
                semantic_id=str(item.get("semantic_id") or ""),
                also_on=tuple(str(name) for name in item.get("also_on") or ()),
                unreferenced=bool(item.get("unreferenced")),
            )
            for item in cached
        )

    def assertion_register(self) -> register.Register:
        """The register this run settled, or the deterministic floor if none.

        A run whose reading turn never completed is not a run without a
        register. It is a run whose register is exactly what was measured, and
        that is a complete answer — every nomination, kept under the name its
        own measurement derives.
        """
        stored = self._analysis_record().get("register")
        if stored is not None:
            return register.from_payload(stored)
        return register.default_register(self.register_floor())

    def _store_register(self, settled: register.Register) -> None:
        self._analysis_record()["register"] = settled.payload()
        self.save()

    def _bind_register(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """Dispatch this capability's two unit kinds to their own boundaries."""
        self.ws = subject
        if str(unit.get("kind") or "") == "analysis_register":
            return self._commit_register(capability, unit)
        return self._read_the_map(capability, unit)

    def _read_the_map(
        self, capability: workflow.Capability, unit: dict
    ) -> BoundUnitPipeline | DeterministicUnitResult:
        """One turn over every frame, settling the register. Commits nothing."""
        floor = self.register_floor()
        table_scope = self.scope()
        frames = definable_targets(self.ws, table_scope)
        if not frames:
            return DeterministicUnitResult("skipped")
        task = self.add_task(
            "analysis_register", "workflow:analysis_register", "Assertion register"
        )
        self.task_status(task, "running")

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                analysis_reading_scope(
                    self.ws,
                    frames,
                    nominations=[
                        {
                            "ref": item.ref,
                            "frame": item.frame,
                            "family": item.family,
                            "test": item.test,
                            "params": dict(item.params),
                            "tested": item.tested,
                            "flagged": item.flagged,
                            "reading": item.reading,
                            # Said as a field rather than left inside the prose,
                            # because the prose did not survive contact. Where a
                            # reference resolves in no imported key the lookup
                            # in ``params`` is one arbitrary member of a set that
                            # all failed identically — and two runs read that
                            # spec, saw a buyer identifier tested against an
                            # invoice number, correctly called it a domain
                            # mismatch, and declined a 96-row finding for it.
                            # The spec cannot be made to name a better master:
                            # name affinity scores every candidate the same.
                            # So the arbitrariness is disclosed instead.
                            **(
                                {
                                    "lookup_is_arbitrary": True,
                                    "note": (
                                        "Every imported master was checked and "
                                        "none matched. The lookup named in "
                                        "params is one of them, chosen "
                                        "arbitrarily — judge the reconciliation "
                                        "failing everywhere, not the master."
                                    ),
                                }
                                if item.unreferenced
                                else {}
                            ),
                            **(
                                {"also_measured_on": list(item.also_on)}
                                if item.also_on
                                else {}
                            ),
                        }
                        for item in floor
                    ],
                    relationships=self.relationship_records(),
                    hypotheses=self.retained_hypotheses(),
                    value_domains=[
                        domain
                        for frame in frames
                        for domain in probes.value_domains(self.ws, frame)
                    ],
                ),
            )

        def settle(proposal):
            # The pipeline calls this with the validated proposal before it
            # becomes durable. For this unit the "acceptance" is the merge: the
            # decisions the turn made, applied over a floor that already stands.
            settled = register.merge(floor, proposal)
            self._store_register(settled)
            return dict(proposal)

        def on_committed(_stage, _unit, _outcome):
            settled = self.assertion_register()
            for item in settled.declined:
                # A subtraction from a measured set is the only irreversible
                # thing this turn does, so it is reported rather than left in
                # the register for a reader to go looking for.
                self.warn(
                    f"Declined a measured nomination — {item.title}: {item.reason}"
                )
            for item in settled.unanswerable:
                self.warn(f"This data cannot answer: {item.question} — {item.why}")
            self.task_detail(
                task,
                f"{counted(len(settled.kept), 'assertion')} kept, "
                f"{len(settled.authored)} added, {len(settled.declined)} declined.",
            )
            self.task_status(task, "completed")
            return DeterministicUnitResult("succeeded")

        def failure_handler(_stage, _unit, error) -> tuple[str, str] | None:
            # The floor is the point. A reading turn that could not be used
            # settles here and the commit unit below writes what was measured,
            # so the run loses the turn's judgment and none of its evidence.
            self.warn(
                "The assertion register was not read by the model "
                f"({error}); the measured nominations stand as written."
            )
            self.task_detail(
                task, f"Unread; {counted(len(floor), 'measured nomination')} stand."
            )
            self.task_status(task, "completed")
            return ("skipped", "The register stands on its measured floor.")

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="analysis.reading",
                executor_id=None,
                unit_input={"parent_refs": list(unit.get("parent_refs") or [])},
                activity={
                    "artifact_refs": list(unit.get("parent_refs") or []),
                    "task_id": task["id"],
                },
                expected_revision=self.ws.revision,
                expected_parents={},
                capability_definition_hash=workflow.capability_definition_hash(
                    capability
                ),
                proposal_reference=unit.get("proposal_sidecar"),
                receipt_reference=None,
            ),
            context_provider=context_provider,
            context_identity_provider=lambda manifest: self.context_resolver.execution_identity(
                capability, manifest
            ),
            target=None,
            approval_provider=settle,
            on_committed=on_committed,
            failure_handler=failure_handler,
        )

    def _commit_register(
        self, capability: workflow.Capability, unit: dict
    ) -> DeterministicUnitResult:
        """Write every kept register entry, across every frame, in one commit.

        No model runs here. Each entry is a spec the sweep already executed, so
        what would have been a definition turn per frame — nineteen of them on
        the baseline run, half of whose output was the model retyping specs it
        had been handed — is a transaction.
        """
        settled = self.assertion_register()
        definitions = [item.definition() for item in settled.kept]
        if not definitions:
            return DeterministicUnitResult("skipped")
        task = self.add_task(
            "analysis_register", "workflow:analysis_register", "Assertion register"
        )
        self.task_status(task, "running")
        frames = tuple(
            dict.fromkeys(str(item["table"]) for item in definitions)
        )
        # Guarded on the frames actually written to rather than on the unit's
        # parent refs. The unit is named for the whole scope and carries the
        # register reference; what a concurrent write could invalidate is a
        # frame one of these specs reads.
        expected = parent_hashes(
            self.ws, [frame_ref(self.ws, name) for name in frames]
        )
        target = AnalysisRegisterExecutorTarget(
            self.ws,
            self.run["id"],
            frames,
            allow_auditor_overwrite=self.run["mode"] == "permission",
        )
        try:
            receipt = self._commit_with_receipt(
                capability=capability,
                unit=unit,
                executor_id="analysis.register",
                proposal={
                    "analyses": definitions,
                    "declined": [
                        f"{item.title}: {item.reason}" for item in settled.declined
                    ],
                },
                target=target,
                expected_parents=expected,
            )
        except WorkspaceConflict as error:
            self.task_status(task, "failed", str(error))
            return DeterministicUnitResult("conflict", error=str(error))
        self.ws = target.workspace
        output = receipt.output
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
        for reason in output.get("dropped") or []:
            self.warn(f"Register entry not saved — {reason}")
        written = len(output.get("analyses") or [])
        self.task_detail(
            task,
            f"{counted(written, 'measured procedure')} saved across "
            f"{counted(len(frames), 'frame')}.",
        )
        self.task_status(task, "completed")
        return DeterministicUnitResult("succeeded", tuple(receipt.artifact_refs))

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
        settled = self.assertion_register()
        assertions = settled.assertions_for(target_frame)
        # Analyses the register wrote are not evidence that this unit has run.
        # Every frame carrying a measured nomination is populated before this
        # stage begins now, so a guard that asked only "does this frame have an
        # analysis" would answer yes for every frame and no definition turn
        # would ever be taken.
        from_register = {
            item.nomination.semantic_id for item in settled.kept
        }
        existing = [
            item
            for item in agent_analyses(self.ws, table_scope)
            if str(item.get("table") or "") == target_frame
            and str(item.get("semantic_id") or "") not in from_register
        ]
        if existing and workflow_scope(self.run).get("generation_mode") != "force":
            # The frame already carries definitions this unit wrote. Units are
            # durable once materialized, so a stage re-run must not spend a
            # provider turn re-deriving what already exists; only an explicit
            # regeneration does that.
            return DeterministicUnitResult(
                "succeeded",
                tuple(analysis_ref(str(item["id"])) for item in existing),
            )
        hypotheses = [
            {
                "ref": item.ref,
                "hypothesis": item.assertion,
                "why": item.why,
                "columns": list(item.columns),
                "requires": [],
            }
            for item in assertions
        ]
        # The register decided what this run tests, and it placed nothing here.
        # Every nomination this frame measured is already saved — committed by
        # the register, not proposed by a turn — so a definition turn here would
        # have nothing to write that is not either already durable or already
        # declined with a reason.
        #
        # This replaces the old rule, which skipped a joined frame carrying no
        # retained hypothesis and no breaching nomination of its own. That rule
        # was guessing at the same question from one frame's vantage; the
        # register answers it from the whole map's.
        if not assertions:
            # A joined frame nothing was admitted to test, and nothing its own
            # columns dispute.
            #
            # The first half is a routing rule: every hypothesis this frame's
            # lineage carries is already prepared on the narrowest frame that
            # can test it, so a turn here would re-derive that work against a
            # wider population and then be dropped as a repeat. That reasoning
            # holds for tests the gate imagined, and it was deciding alone —
            # which meant a frame lost its turn before the sweep had been
            # allowed to look at it. Six of eighteen frames went that way in one
            # run, in three milliseconds each, among them a three-way frame
            # carrying an invoice's payment status beside its requisition's
            self.task_detail(
                self.add_task(
                    "analysis_definitions",
                    "workflow:analysis_definitions",
                    "Analysis definitions",
                ),
                f"The register places no assertion on '{target_frame}' that its "
                "own measurements have not already saved.",
            )
            return DeterministicUnitResult("skipped")
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
                    hypotheses=hypotheses,
                    # Deliberately no probe findings. Every nomination this
                    # frame measured is already a saved analysis, committed by
                    # the register before this turn was bound, and it appears
                    # here as one of ``current_analyses``. Sending it again as a
                    # nomination would ask the model to re-propose a spec that
                    # already exists, which the identity check then drops — a
                    # slot spent to produce a duplicate.
                    value_domains=probes.value_domains(self.ws, target_frame),
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
            for reason in output.get("dropped") or []:
                # A proposal that failed local execution or established
                # nothing is not evidence against its siblings, so it is
                # dropped rather than failing the whole frame — but a dropped
                # proposal is still a gap, and the auditor cannot see a
                # proposal that was never saved unless it is said here.
                self.warn(f"'{target_frame}': dropped a proposed analysis — {reason}")
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
            if NOTHING_NEW_TO_ANALYSE in str(error):
                return (
                    "skipped",
                    "Every analysis this frame supports is already saved against "
                    "a frame built from the same tables.",
                )
            # The same shape of answer, reached by running the proposals rather
            # than by comparing them: every one of them flagged nearly its whole
            # population, which establishes nothing about any row in it.
            if NO_INFORMATIVE_ANALYSIS in str(error):
                return (
                    "skipped",
                    "No proposed analysis for this frame separated any of its "
                    "rows from the rest.",
                )
            return None

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
                f"{counted(output.get('cited', 0), 'result')} cited.",
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

    # ---------------------------------------------------- analysis.promoted
    def _bind_promotion(
        self,
        subject: Workspace,
        run: dict,
        capability: workflow.Capability,
        stage: dict,
        unit: dict,
    ) -> BoundUnitPipeline:
        """Bind one saved procedure's fitting decision to the shared pipeline.

        Guarded on the analysis rather than on the RCM row, because the row is
        chosen by the turn and is not known until the proposal exists. The
        disposition and the test it becomes commit together under that guard,
        so an interrupted unit is either wholly applied or wholly absent.
        """
        self.ws = subject
        analysis_id = str((unit.get("payload") or {}).get("analysis_id") or "")
        if not analysis_id:
            analysis_id = unit["parent_refs"][0].split(":", 1)[1]
        parent_ref = f"analysis:{analysis_id}"
        expected = parent_hashes(self.ws, [parent_ref])
        target = PromotionExecutorTarget(self.ws, self.run["id"], analysis_id)
        task = self.add_task(
            "analysis_promotion",
            "workflow:analysis_promotion",
            "Analyses placed in the matrix",
        )

        def context_provider():
            return self.context_resolver.resolve(
                self.ws,
                capability,
                unit,
                promotion_scope(self.ws, analysis_id),
            )

        def approval_provider(proposal):
            # Permission mode gates the writing of audit artifacts. A promotion
            # writes a Data Test against a control and is gated like any other
            # generated test; a decline writes only the run's own record that it
            # considered the procedure and set it aside. Asking an auditor to
            # approve each of those would put a confirmation in front of every
            # exploratory procedure to record that nothing was produced from it.
            if not proposal.get("promote"):
                return dict(proposal)
            summary = f"Test for {proposal.get('rcm_id')} from this analysis."
            accepted = self.request_approval(
                "analysis_promotion",
                task,
                [
                    self.proposal_item(
                        str(proposal.get("title") or "Analysis placement"),
                        summary,
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
            state = str(output.get("state") or "")
            test_id = str(output.get("test_id") or "")
            if test_id:
                self.record_artifact("datatest", test_id, "", "created", task)
                self.task_detail(
                    task,
                    f"{analysis_id} became Data Test {test_id}.",
                )
                self.emit(
                    "workspace_changed",
                    {"kind": "datatest", "id": test_id, "action": "created"},
                )
            else:
                # A decline is an answer, and an answer nobody can read is not
                # one. It reaches the run record here rather than only the
                # analysis, so the stage reports what it set aside and why.
                self.task_detail(task, f"{analysis_id} set aside — {state}.")
            self.task_status(task, "completed")

        return BoundUnitPipeline(
            request=UnitPipelineRequest(
                capability_id=capability.id,
                unit_id=unit["id"],
                worker_id="analysis.promotion",
                executor_id="analysis.promotion",
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
                capability_definition_hash=workflow.capability_definition_hash(
                    capability
                ),
                approval_kind=(
                    "analysis_promotion"
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
        self.task_detail(task, f"{title}: {counted(result['row_count'], 'row')}.")
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
    # Relationship diagnosis is local and independent by pair, so one failed
    # diagnostic need not stop the gate from judging the rest.
    "data.join_utility_ready": {"data.relationships_inferred"},
    # A safe auto-selected join, a skipped unrelatable pair, or an
    # auditor-held join choice must not withhold analysis of the frames that
    # remain usable. Later analysis capabilities re-expand from the joins that
    # actually committed. Note what is deliberately absent: ``data.joins_ready``
    # is not partial in ``data.join_utility_ready``, because a join
    # materialized after the gate failed is a join nothing admitted.
    # The register's own two units are partial in each other's stage: a
    # reading turn that could not be used must not withhold the floor it was
    # given, which is the whole safety argument for spending one turn on the
    # whole engagement.
    "analysis.register_ready": {"data.joins_ready"},
    "analysis.definitions_ready": {"analysis.register_ready"},
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
        "analysis.register_ready": (
            adapter._bind_register,
            {"worker": "analysis.reading", "executor": "analysis.register"},
        ),
        "data.join_utility_ready": (
            adapter._bind_join_utility,
            {"worker": "analysis.join_utility", "executor": None},
        ),
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
        if capability.id == "analysis.definitions_ready":
            # Said once for the stage, before any frame is bound: a test the
            # gate admitted and no frame can carry is a coverage gap, and the
            # frames that were skipped are not where a reader would look for it.
            adapter._warn_untestable_hypotheses()
            adapter._warn_unjoined_engagement()

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
