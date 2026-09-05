"""Focused tests for the registered finding worker and executor (P7H.2).

The worker owns the prompt, the bundle-to-message transformation, and the
response contract; every reference a finding carries — RCM row, planned test,
execution result, and immutable evidence anchor — is derived by the executor
from the current observation, not from the proposal.
"""

from __future__ import annotations

import json

import pytest

from app import data_tests, doc_tests, rcm_execution, workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.capabilities import _shared
from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.reporting import (
    FINDING_EXECUTOR,
    FindingExecutorTarget,
    finding_ref,
    finding_semantic_id,
)
from app.agent.workers import WORKERS, WorkerRequest, WorkerRunError
from app.agent.workers import reporting as reporting_worker
from app.workspace_transactions import parent_hashes


class _Gateway:
    """The finding worker answers in Markdown, as the planning memorandum does.

    A scripted response is returned verbatim, so a test says what the model
    answered. The tool-call wrapping below is kept for workers that do submit
    through a forced call; the finding worker passes neither `tools` nor
    `tool_choice`, and a test asserts that.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(
        self,
        system,
        user,
        activity=None,
        *,
        attempt=1,
        tools=None,
        tool_choice=None,
        return_message=False,
        **kwargs,
    ):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "activity": activity,
                "attempt": attempt,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        response = self.responses.pop(0)
        if not return_message:
            return response
        name = (tool_choice or {}).get("function", {}).get("name", "")
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": response},
                }
            ],
        }


# The shipped finding template, reduced to the part the worker contract needs:
# the section headings the narrative must answer, and one guidance comment that
# must not survive into the narrative.
TEMPLATE = """# Finding

## Condition

<!-- section: What was found, quantified, in the past tense. -->

## Criteria

## Root Cause

## Risk

## Recommendation
"""

NARRATIVE = """## Condition

Of the 1,284 payments released in the period, 37 were duplicates.

## Criteria

Invoice identifiers are required to be unique.

## Root Cause

Lack of a duplicate check at invoice entry.

## Risk

Financial loss through duplicate payment.

## Recommendation

Enforce a uniqueness constraint on the invoice identifier.
"""


EXCEPTION_ROWS = {
    "execution_ref": "datatest:DAT-1",
    "result_sha1": "sha1-result",
    "semantic_valid": True,
    "exception_count": 1,
    "columns": ["INVOICE_ID", "INVOICE_DATE", "PAYMENT_DATE"],
    "rows": [["INV2024008", "2024-12-20", "2024-11-29"]],
    "rows_supplied": 1,
    "rows_withheld": 0,
    "truncated": False,
    "steps": [{"step_id": "STEP-1", "label": "Verification timing"}],
}


def _bundle(
    template: str = TEMPLATE,
    exception_rows: dict | None = EXCEPTION_ROWS,
    instruction: str | None = None,
):
    values = [
        (
            "observation",
            "observation:OBS-1",
            ContextRepresentation("current_artifact"),
            {
                "id": "OBS-1",
                "summary": "A duplicate invoice identifier was processed.",
                "outcome": "exception",
            },
        ),
        (
            "rcm_row",
            "rcm:RCM-1",
            ContextRepresentation("current_artifact"),
            {"id": "RCM-1", "risk": "Duplicate payments"},
        ),
        (
            "planned_test",
            "planned_test:PT-1",
            ContextRepresentation("current_artifact"),
            {"id": "PT-1", "title": "Test duplicates"},
        ),
        (
            "execution_result",
            "datatest:DAT-1",
            ContextRepresentation("current_artifact"),
            {
                "execution_ref": "datatest:DAT-1",
                "immutable_execution_result": {"exception_count": 1},
                "evidence_anchor": {"source_kind": "datatest"},
            },
        ),
        (
            "finding_template",
            "template:finding",
            ContextRepresentation("artifact_template"),
            template,
        ),
    ]
    if instruction is not None:
        values.append(
            (
                "instruction",
                "instruction:abcdef123456",
                ContextRepresentation("auditor_instruction"),
                instruction,
            )
        )
    if exception_rows is not None:
        values.append(
            (
                "exception_rows",
                "datatest:DAT-1:exceptions",
                ContextRepresentation("datatest_exception_rows"),
                exception_rows,
            )
        )
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=representation,
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        context=bundle if bundle is not None else _bundle(),
        unit_input={"input_sha1": "finding-input"},
        activity={"artifact_refs": ["observation:OBS-1"]},
    )


def _draft(**overrides):
    value = {
        "title": "Duplicate invoice processing",
        "severity": "medium",
        "narrative": NARRATIVE,
        "cause_pending": False,
    }
    value.update(overrides)
    return value


def _markdown(**overrides) -> str:
    """One finding as the worker asks for it: title, severity, then sections."""
    value = _draft(**overrides)
    return (
        f"# {value['title']}\n\n"
        f"**Severity:** {value['severity']}\n\n"
        f"{value['narrative']}"
    )


def _without_section(narrative: str, heading: str) -> str:
    """The narrative with one section's body emptied, its heading intact."""
    start = narrative.index(f"## {heading}")
    end = narrative.find("\n## ", start)
    return narrative[:start] + f"## {heading}\n\n" + narrative[end + 1:]


def _deferred_cause(narrative: str) -> str:
    """The narrative with its cause formally deferred rather than asserted."""
    return _without_section(narrative, "Root Cause").replace(
        "## Root Cause\n\n",
        "## Root Cause\n\n_Root cause pending auditor follow-up._\n\n",
        1,
    )


def test_finding_worker_uses_only_the_supplied_observation_and_execution():
    gateway = _Gateway([_markdown()])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["finding"]["severity"] == "medium"
    assert gateway.calls[0]["system"] == reporting_worker.FINDING_SYSTEM
    assert "duplicate invoice identifier" in gateway.calls[0]["user"].casefold()
    assert (
        gateway.calls[0]["activity"]["context_metrics"]["worker_kind"]
        == "finding_draft"
    )


def test_finding_worker_repairs_an_empty_template_section_by_name():
    gateway = _Gateway(
        [
            _markdown(narrative=_without_section(NARRATIVE, "Criteria")),
            _markdown(),
        ]
    )

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert "narrative section 'Criteria' is empty" in gateway.calls[1]["user"]


def test_finding_worker_accepts_an_open_root_cause_only_when_it_is_deferred():
    # The deferral is written into the narrative rather than asserted beside it,
    # so a draft cannot claim a pending cause while the section reads as blank.
    gateway = _Gateway([_markdown(narrative=_deferred_cause(NARRATIVE))])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["finding"]["cause_pending"] is True
    assert result.repaired is False


def test_finding_worker_repairs_an_undeferred_empty_root_cause():
    silent = _markdown(narrative=_without_section(NARRATIVE, "Root Cause"))
    gateway = _Gateway([silent, _markdown()])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    assert result.proposal["finding"]["cause_pending"] is False
    assert "Root cause pending auditor follow-up" in gateway.calls[1]["user"]


def test_a_stated_root_cause_is_not_read_as_a_deferral():
    gateway = _Gateway([_markdown()])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["finding"]["cause_pending"] is False


def test_finding_worker_is_given_the_exception_rows_and_told_to_identify_them():
    gateway = _Gateway([_markdown()])

    WORKERS.execute(_request(), gateway)

    user = gateway.calls[0]["user"]
    # The identifier reaches the prompt, so the draft can name the record.
    assert "INV2024008" in user
    assert '"rows_withheld": 0' in user.replace(" ", " ")
    assert "identify the records that failed" in gateway.calls[0]["system"].casefold()


def test_finding_worker_runs_without_an_exception_table():
    # A Document Test unit resolves no exception-row item at all. That is the
    # normal shape for those findings, not a contract violation.
    request = WorkerRequest(
        worker_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        context=_bundle(exception_rows=None),
        unit_input={"input_sha1": "finding-input"},
        activity={"artifact_refs": ["observation:OBS-1"]},
    )
    gateway = _Gateway([_markdown()])

    result = WORKERS.execute(request, gateway)

    assert result.repaired is False
    assert '"EXCEPTION ROWS": null' in gateway.calls[0]["user"]


def test_finding_worker_takes_its_required_sections_from_the_supplied_template():
    # A firm that renames a section moves the contract with it; nothing in the
    # worker enumerates the shipped headings.
    request = WorkerRequest(
        worker_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id="finding:OBS-1",
        context=_bundle(template="## Observation\n\n## Impact\n"),
        unit_input={"input_sha1": "finding-input"},
        activity={"artifact_refs": ["observation:OBS-1"]},
    )
    narrative = "## Observation\n\nA duplicate was released.\n\n## Impact\n\nLoss.\n"
    gateway = _Gateway([_markdown(narrative=narrative)])

    result = WORKERS.execute(request, gateway)

    assert result.repaired is False
    assert "Observation" in gateway.calls[0]["user"]


def test_finding_worker_rejects_an_unsupported_severity():
    invalid = _markdown(severity="catastrophic")
    gateway = _Gateway([invalid, invalid])

    with pytest.raises(WorkerRunError, match="carrying exactly one of"):
        WORKERS.execute(_request(), gateway)


def _observed_workspace(workspace_with_data):
    """Roll up one executed data test into an eligible exception observation."""
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate invoices may be paid",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    test = data_tests.create(
        ws,
        {
            "title": "Duplicate invoices",
            "objective": "Identify repeated invoice identifiers.",
            "engine": "analytics",
            "table_refs": ["transactions"],
            "rcm_id": row["id"],
            "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
        },
    )
    data_tests.run(ws, test["id"])
    rcm_execution.rollup(ws)
    observation = ws.observations[0]
    return ws, ws.observations[0]


def _finding_request(ws, observation, draft=None):
    return ExecutorRequest(
        executor_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id=f"finding:{observation['id']}",
        proposal={"finding": draft or _draft()},
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"observation:{observation['id']}"]),
        activity={"artifact_refs": [f"observation:{observation['id']}"]},
    )


def test_finding_executor_derives_every_reference_from_the_observation(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    # References in the proposal are ignored; the observation is authoritative.
    request = _finding_request(
        ws, observation, _draft(rcm_refs=["RCM-SMUGGLED"], auditor_confirmed=True)
    )
    target = FindingExecutorTarget(ws, "run-finding", observation["id"])

    receipt = EXECUTORS.execute(request, target)

    committed = target.workspace.findings[0]
    assert committed["rcm_refs"] == [observation["rcm_id"]]
    assert committed["test_refs"] == [observation["test_id"]]
    assert committed["execution_refs"] == [observation["execution_ref"]]
    assert committed["evidence_refs"]
    assert committed["auditor_confirmed"] is False
    assert committed["semantic_id"] == finding_semantic_id(observation["id"])
    assert receipt.artifact_refs == (finding_ref(committed["id"]),)
    assert receipt.postcondition_hashes == parent_hashes(
        target.workspace, [finding_ref(committed["id"])]
    )


def test_finding_executor_refuses_a_draft_that_fails_support_validation(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    request = _finding_request(
        ws, observation, _draft(narrative=_without_section(NARRATIVE, "Recommendation"))
    )
    target = FindingExecutorTarget(ws, "run-unsupported", observation["id"])

    with pytest.raises(workspaces.WorkspaceError, match="support validation"):
        FINDING_EXECUTOR.implementation(request, target)

    assert workspaces.load_workspace(ws.id).findings == []


def test_finding_executor_reconciles_an_interrupted_commit_idempotently(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    request = _finding_request(ws, observation)
    target = FindingExecutorTarget(ws, "run-reconcile", observation["id"])

    # The commit does not change the observation, so an absent finding is what
    # proves it never landed.
    assert FINDING_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    FINDING_EXECUTOR.implementation(request, target)
    recovered = FINDING_EXECUTOR.reconciler(request, target)

    assert recovered.disposition == "already_applied"
    assert recovered.result.output["id"] == target.workspace.findings[0]["id"]
    # The observation-derived id keeps a repeated commit from drafting twice.
    assert len(target.workspace.findings) == 1




def test_the_finding_is_answered_as_markdown_not_as_a_tool_call():
    """A finding is multi-line Markdown, so it is not carried inside JSON.

    A forced submission call bought shape and cost structure: the configured
    model never emitted a newline inside a tool call's arguments — 35 of 35
    across every worker that used one — so each narrative arrived with its
    headings, blank lines and table rows flattened onto a single line. The
    planning memorandum has always come back as Markdown from the same models,
    which is what this follows.
    """

    gateway = _Gateway([_markdown()])

    result = WORKERS.execute(_request(), gateway)

    call = gateway.calls[0]
    assert call["tools"] is None
    assert call["tool_choice"] is None
    assert "Markdown only" in call["system"]
    assert result.proposal["finding"]["title"] == "Duplicate invoice processing"
    assert result.proposal["finding"]["severity"] == "medium"
    # Title and severity are the finding's typed spine, read off the draft;
    # neither reaches the prose that is copied into the report.
    narrative = result.proposal["finding"]["narrative"]
    assert narrative.startswith("## Condition")
    assert "Severity" not in narrative


def test_a_narrative_flattened_onto_one_line_is_rejected_as_missing_headings():
    """The failure that cost a whole run of eight drafts, named accurately.

    Every section was present in the model's prose and every one was reported
    empty, because a single-line narrative parses as one enormous heading with
    no body. Saying the heading is missing is something a model can act on;
    saying the section is empty is what it re-emitted unchanged.
    """

    flattened = _markdown().replace("\n", " ")
    gateway = _Gateway([flattened, _markdown()])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is True
    repair = gateway.calls[1]["user"]
    assert "missing the `## Condition` heading" in repair
    assert "each on its own line" in repair


def test_a_markdown_table_survives_into_the_narrative():
    """Tables are why the narrative has to keep its line breaks.

    A Markdown table is only a table while each row is on its own line: the
    renderer needs the header, the delimiter row beneath it, and the body rows
    as separate lines to build one at all.
    """

    table = (
        "| Invoice | Invoice date | Payment date |\n"
        "| --- | --- | --- |\n"
        "| INV2024008 | 20 Dec 2024 | 29 Nov 2024 |"
    )
    narrative = NARRATIVE.replace(
        "Of the 1,284 payments released in the period, 37 were duplicates.",
        "Of the 1,284 payments released in the period, 37 were duplicates."
        f"\n\n{table}",
    )
    gateway = _Gateway([_markdown(narrative=narrative)])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is False
    assert table in result.proposal["finding"]["narrative"]


def test_a_finding_returned_as_json_anyway_is_still_read():
    # Tolerance, not the contract: a model that falls back on its JSON habit is
    # repaired against the same rules rather than discarded.
    gateway = _Gateway([json.dumps({"finding": _draft()})])

    result = WORKERS.execute(_request(), gateway)

    assert result.repaired is False
    assert result.proposal["finding"]["severity"] == "medium"
    assert result.proposal["finding"]["narrative"].startswith("## Condition")


def test_template_guidance_is_not_copied_into_the_narrative():
    echoed = _markdown(
        narrative=NARRATIVE.replace(
            "## Condition\n",
            "## Condition\n\n<!-- section: What was found, quantified. -->\n",
            1,
        )
    )
    gateway = _Gateway([echoed])

    result = WORKERS.execute(_request(), gateway)

    assert "<!--" not in result.proposal["finding"]["narrative"]


def test_the_severity_line_is_read_however_it_is_emphasised():
    for line in ("**Severity:** high", "**Severity**: high", "Severity: high"):
        gateway = _Gateway([_markdown().replace("**Severity:** medium", line)])

        result = WORKERS.execute(_request(), gateway)

        assert result.proposal["finding"]["severity"] == "high", line


# --------------------------------------------------------------------------- #
# Support the executor will refuse, refused before the model turn (step 0b)
# --------------------------------------------------------------------------- #
def _doc_test_observation(workspace_with_data, *, finished: bool):
    """An exception observation resting on a Document Test, open or finished.

    The open case is the shape of the two evidence failures: the observation is
    a real exception, the finding worker would happily draft it, and the
    executor would then refuse the draft because the test behind it is not
    complete — after the turn had been paid for.
    """
    ws = workspace_with_data
    row = ws.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Payments may be released without approval",
            "control": "Approval is evidenced before release",
            "risk_rating": "high",
        }
    )
    items = [
        {
            "label": "Invoice 1001",
            "state": "exception",
            "auditor_disposition": "exception",
            "attributes": [{"name": "Approval", "result": "fail"}],
        }
    ]
    if not finished:
        items.append({"label": "Invoice 1002", "state": "pending"})
    test = doc_tests.create_test(
        ws,
        {
            "kind": "attribute",
            "title": "Approval evidence",
            "objective": "Determine whether payments were approved.",
            "steps": [
                {"label": "Inspect the approval.", "instruction": "Inspect the approval."}
            ],
            "rcm_id": row["id"],
            "items": items,
        },
    )
    if finished:
        test = doc_tests.update_test(ws, test["id"], {"status": "completed"})
    observation = {
        "id": "OBS-PENDING",
        "rcm_id": row["id"],
        "test_id": test["id"],
        "execution_ref": f"doctest:{test['id']}",
        "summary": "Approval was not evidenced.",
        "outcome": "exception",
        "classification": "draft_finding_candidate",
    }
    ws.observations.append(observation)
    ws.save()
    return ws, row, test, observation


def test_an_observation_on_an_unfinished_test_expands_no_finding_unit(
    workspace_with_data,
):
    """The turn saved: the unit is never expanded, so it is never billed."""
    ws, _row, _test, _observation = _doc_test_observation(
        workspace_with_data, finished=False
    )
    capability = audit_capabilities.REGISTRY.get("findings.drafted")
    scope = {"target_refs": ["observation:OBS-PENDING"]}

    assert capability.expand_units(ws, scope) == []
    # Force is a decision about reuse, not a licence to run a unit that cannot
    # commit, so it does not reopen the gate either.
    assert capability.expand_units(ws, {**scope, "generation_mode": "force"}) == []


def test_the_same_observation_on_a_finished_test_expands_as_before(
    workspace_with_data,
):
    """The gate is the test's state, not the presence of the check."""
    ws, _row, _test, _observation = _doc_test_observation(
        workspace_with_data, finished=True
    )
    capability = audit_capabilities.REGISTRY.get("findings.drafted")

    units = capability.expand_units(ws, {"target_refs": ["observation:OBS-PENDING"]})

    assert [unit.parent_refs[0] for unit in units] == ["observation:OBS-PENDING"]


def test_readiness_names_the_observation_it_cannot_draft(workspace_with_data):
    """Blocked, not satisfied: a skipped observation must still be visible."""
    ws, _row, _test, _observation = _doc_test_observation(
        workspace_with_data, finished=False
    )
    capability = audit_capabilities.REGISTRY.get("findings.drafted")

    readiness = capability.readiness(ws, {"target_refs": ["observation:OBS-PENDING"]})

    assert readiness.state == "blocked"
    assert readiness.details["unsupported"] == ["OBS-PENDING"]
    assert any("is not complete" in reason for reason in readiness.reasons)
    assert any(
        "is not complete" in issue
        for issue in readiness.details["unsupported_issues"]["OBS-PENDING"]
    )


def test_readiness_on_a_finished_test_is_missing_and_says_nothing_about_support(
    workspace_with_data,
):
    ws, _row, _test, _observation = _doc_test_observation(
        workspace_with_data, finished=True
    )
    capability = audit_capabilities.REGISTRY.get("findings.drafted")

    readiness = capability.readiness(ws, {"target_refs": ["observation:OBS-PENDING"]})

    assert readiness.state == "missing"
    assert "unsupported" not in readiness.details


def test_the_executor_still_refuses_the_same_draft_by_itself(workspace_with_data):
    """The gate above is an optimization; it is not the guarantee.

    Expansion is skipped to save a model turn. What makes an unsupported
    finding impossible to commit is still the executor's own check, so a draft
    built by hand and handed straight to it is refused exactly as before.
    """
    ws, _row, _test, observation = _doc_test_observation(
        workspace_with_data, finished=False
    )
    request = _finding_request(ws, observation)
    target = FindingExecutorTarget(ws, "run-pending", observation["id"])

    with pytest.raises(workspaces.WorkspaceError, match="support validation"):
        FINDING_EXECUTOR.implementation(request, target)

    assert workspaces.load_workspace(ws.id).findings == []


# --------------------------------------------------------------------------- #
# Redrafting one named finding (step 2c)
# --------------------------------------------------------------------------- #
def test_a_drafted_observation_expands_nothing_until_its_finding_is_named(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    EXECUTORS.execute(
        _finding_request(ws, observation),
        FindingExecutorTarget(ws, "run-first", observation["id"]),
    )
    ws = workspaces.load_workspace(ws.id)
    finding = ws.findings[0]
    capability = audit_capabilities.REGISTRY.get("findings.drafted")

    # Already drafted, so an unscoped pass has nothing to do.
    assert capability.expand_units(ws, {}) == []

    units = capability.expand_units(ws, {"target_refs": [f"finding:{finding['id']}"]})

    # Naming the finding is the instruction; force is not asked for twice.
    assert [unit.parent_refs[0] for unit in units] == [
        f"observation:{observation['id']}"
    ]


def test_a_named_finding_scopes_to_its_own_row_not_the_whole_workspace(
    workspace_with_data,
):
    ws, observation = _observed_workspace(workspace_with_data)
    scope = {"target_refs": [f"observation:{observation['id']}"]}

    assert _shared.target_rcm_ids(ws, scope) == [observation["rcm_id"]]
    assert _shared.named_observation_ids(ws, scope) == (observation["id"],)


def test_naming_a_finding_is_the_permission_to_replace_an_auditors_draft(
    workspace_with_data,
):
    """An auditor pointing at their own draft and asking for a rewrite has
    consented to it. An unnamed run still must not touch it."""
    ws, observation = _observed_workspace(workspace_with_data)
    EXECUTORS.execute(
        _finding_request(ws, observation),
        FindingExecutorTarget(ws, "run-agent", observation["id"]),
    )
    ws = workspaces.load_workspace(ws.id)
    ws.findings[0]["source"] = "manual"
    ws.save()

    with pytest.raises(workspaces.WorkspaceError, match="manually maintained"):
        FINDING_EXECUTOR.implementation(
            _finding_request(ws, observation),
            FindingExecutorTarget(ws, "run-unnamed", observation["id"]),
        )

    target = FindingExecutorTarget(
        ws, "run-named", observation["id"], named_by_request=True
    )
    EXECUTORS.execute(_finding_request(ws, observation), target)

    assert target.workspace.findings[0]["source"] == "agent"


def test_the_finding_turn_is_told_what_the_auditor_asked_for():
    text = "Every template section must contain text."
    gateway = _Gateway([_markdown()])

    WORKERS.execute(_request(_bundle(instruction=text)), gateway)

    sent = json.loads(gateway.calls[0]["user"])
    assert sent["auditor_instruction"] == text
    # Promoted once, not also buried in the serialized bundle beneath it.
    assert gateway.calls[0]["user"].count(text) == 1


def test_a_finding_turn_with_no_instruction_carries_no_empty_key():
    gateway = _Gateway([_markdown()])

    WORKERS.execute(_request(), gateway)

    assert "auditor_instruction" not in json.loads(gateway.calls[0]["user"])


def test_the_finding_prompt_states_where_an_instruction_ranks():
    assert "auditor_instruction" in reporting_worker.FINDING_SYSTEM
    assert "never over the response contract" in reporting_worker.FINDING_SYSTEM
