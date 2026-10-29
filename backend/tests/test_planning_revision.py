"""Revisions an auditor asks for, and the documents an agent edits by passage.

Three things are held here. A risk taken out of the matrix stays out until a
person brings it back. A revision decides about every row it was shown and
nothing else, and is filed by the request it answers. And a long document is
edited the way a file is — one exact passage, against the version that was
read, inside the sections the template set.
"""

from __future__ import annotations

import json

import pytest

from app import data_tests, findings, markdown_artifacts, markdown_sections, planning_revision
from app.agent import loop_tools, operations
from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    supplied_size,
    total_supplied_size,
)
from app.agent.workers import WORKERS, WorkerRequest
from app.agent.workers.model import WorkerContractError, WorkerResponseValidationError
from app.agent.workers.planning import (
    validate_apm_revision_proposal,
    validate_rcm_revision_proposal,
)
from app.workspaces import WorkspaceError
from conftest import stamp_planning_cycle, wait_run
from test_agent_loop import COMPLETE_APM, LoopScript, configured, finish_turn, start_loop, tool_turn


# --------------------------------------------------------------------------- #
# Retirement
# --------------------------------------------------------------------------- #
def _row(ws, risk, process="Payments", control="A reviewer checks it"):
    return ws.add_rcm({"process": process, "risk": risk, "control": control})


def test_removing_a_row_records_it_and_an_auditor_adding_it_back_lifts_that(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws, "Duplicate payments are made")

    ws.remove_rcm(row["id"], reason="Out of scope this year")

    retired = ws.reload().retired_rcm_rows()
    assert [entry["semantic_id"] for entry in retired] == [row["semantic_id"]]
    assert retired[0]["reason"] == "Out of scope this year"
    assert retired[0]["retired_by"] == "user"

    ws = ws.reload()
    ws.add_rcm({"process": "Payments", "risk": "Duplicate payments are made"})
    assert ws.reload().retired_rcm_rows() == []


def test_merging_moves_tests_and_findings_onto_the_survivor(workspace_with_data):
    ws = workspace_with_data
    survivor = _row(ws, "Payments are made without approval")
    merged = _row(ws, "Payments above the limit are not approved")
    test = data_tests.create(ws, {
        "title": "Unapproved payments", "objective": "Find unapproved payments",
        "rcm_id": merged["id"], "engine": "analytics", "table_refs": ["transactions"],
        "spec": {"test_id": "duplicates", "params": {"columns": ["invoice_no"]}},
    })
    finding = findings.add(ws.reload(), {"title": "Unapproved payment", "rcm_refs": [merged["id"]]})

    ws = ws.reload()
    ws.merge_rcm(merged["id"], survivor["id"], reason="Same failure", run_id="run-1")

    ws = ws.reload()
    assert [row["id"] for row in ws.rcm] == [survivor["id"]]
    assert f"datatest:{test['id']}" in ws.rcm[0]["test_refs"]
    assert next(item for item in ws.data_tests if item["id"] == test["id"])["rcm_id"] == survivor["id"]
    refs = next(item for item in ws.findings if item["id"] == finding["id"])["rcm_refs"]
    # The finding keeps what it cited and gains the row that now carries it.
    assert merged["id"] in refs and survivor["id"] in refs
    retired = ws.retired_rcm_rows()
    assert retired[0]["merged_into"] == survivor["id"]
    assert retired[0]["retired_by"] == "agent"


# --------------------------------------------------------------------------- #
# The revision workers' validators
# --------------------------------------------------------------------------- #
def _bundle(values, capability="planning.rcm_revised", unit="rcm_revision"):
    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=ContextRepresentation("current_artifact"),
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, content in values
    )
    return ContextBundle(
        capability_id=capability,
        unit_id=unit,
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


ROWS = [
    {"id": "RCM-1", "process": "Payments", "risk": "Duplicate payments"},
    {"id": "RCM-2", "process": "Payments", "risk": "Payments split under the limit"},
    {"id": "RCM-3", "process": "Payments", "risk": "Unapproved payments"},
]


def rcm_request(rows=ROWS, row_ids=None, instruction="Keep the core risks"):
    values = [("current_rcm", f"rcm:{row['id']}", row) for row in rows]
    values.append(("rcm_attributes_template", "template:rcm_attributes", "Attributes."))
    if instruction:
        values.append(("instruction", "instruction:abc", instruction))
    return WorkerRequest(
        worker_id="planning.rcm_revision",
        capability_id="planning.rcm_revised",
        unit_id="rcm_revision",
        context=_bundle(values),
        unit_input={
            "row_ids": row_ids or [row["id"] for row in ROWS],
            "process_names": ["Payments"],
        },
        activity={},
    )


def _revision(**overrides):
    return {
        "summary": "Three rows in scope, two after the revision.",
        "decisions": [
            {"rcm_id": "RCM-1", "decision": "keep", "reason": ""},
            {"rcm_id": "RCM-2", "decision": "merge", "into": "RCM-3", "reason": "Same failure."},
            {
                "rcm_id": "RCM-3",
                "decision": "revise",
                "changes": {"risk": "Payments are not approved within limits"},
                "reason": "Widened to cover splitting.",
            },
        ],
        "additions": [],
        **overrides,
    }


def test_a_revision_must_decide_about_every_row_it_was_shown():
    proposal = _revision()
    proposal["decisions"] = proposal["decisions"][:2]
    with pytest.raises(WorkerResponseValidationError, match="no decision for 1 row"):
        validate_rcm_revision_proposal(proposal, rcm_request())


def test_a_merge_must_land_on_a_row_that_survives():
    proposal = _revision()
    proposal["decisions"][2] = {"rcm_id": "RCM-3", "decision": "retire", "reason": "Gone."}
    with pytest.raises(WorkerResponseValidationError, match="not kept or revised"):
        validate_rcm_revision_proposal(proposal, rcm_request())


def test_a_revision_writes_only_what_a_row_says():
    accepted = validate_rcm_revision_proposal(_revision(), rcm_request())
    revised = accepted["decisions"][2]
    assert revised["changes"] == {"risk": "Payments are not approved within limits"}
    proposal = _revision()
    proposal["decisions"][2]["changes"] = {"test_refs": ["datatest:X"]}
    with pytest.raises(WorkerResponseValidationError, match="changed no field"):
        validate_rcm_revision_proposal(proposal, rcm_request())


def test_a_revision_refuses_before_spending_when_rows_in_scope_did_not_fit():
    class Gateway:
        def complete(self, *_args, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("no model call for a scope that did not fit")

    request = rcm_request(rows=ROWS[:2])
    with pytest.raises(WorkerContractError, match="did not fit"):
        WORKERS.get("planning.rcm_revision").implementation(
            request, Gateway(), _first_attempt()
        )


def _first_attempt():
    from app.agent.workers.model import WorkerAttempt

    return WorkerAttempt(1)


APM = (
    "# Audit Planning Memorandum\n\n## Engagement\n\nEntity and scope.\n\n"
    "## Key risks and planned response\n\nTest approval compliance.\n"
)


def apm_request():
    return WorkerRequest(
        worker_id="planning.apm_revision",
        capability_id="planning.apm_revised",
        unit_id="apm_revision",
        context=_bundle(
            [("current_apm", "planning:apm", APM), ("instruction", "instruction:abc", "Shorten")],
            capability="planning.apm_revised",
            unit="apm_revision",
        ),
        unit_input={"sections": markdown_sections.section_headings(APM)},
        activity={},
    )


def test_a_memorandum_revision_rewrites_only_sections_the_memorandum_has():
    with pytest.raises(WorkerResponseValidationError, match="not one of the memorandum"):
        validate_apm_revision_proposal(
            {"summary": "x", "sections": [{"heading": "Budget", "markdown": "Text"}]},
            apm_request(),
        )
    with pytest.raises(WorkerResponseValidationError, match="contains a # or ## heading"):
        validate_apm_revision_proposal(
            {
                "summary": "x",
                "sections": [{"heading": "Engagement", "markdown": "Text\n\n## New\n\nMore"}],
            },
            apm_request(),
        )
    accepted = validate_apm_revision_proposal(
        {"summary": "x", "sections": [{"heading": "engagement", "markdown": "Shorter."}]},
        apm_request(),
    )
    assert accepted["sections"][0]["heading"] == "Engagement"


def test_section_replacement_leaves_every_other_line_alone():
    revised = markdown_sections.replace_bodies(APM, {"Engagement": "New scope."})
    assert "New scope." in revised
    assert "Entity and scope." not in revised
    assert revised.endswith("## Key risks and planned response\n\nTest approval compliance.\n")
    assert markdown_sections.section_headings(revised) == markdown_sections.section_headings(APM)


# --------------------------------------------------------------------------- #
# The revisions, run by the loop
# --------------------------------------------------------------------------- #
def _planned_workspace(ws):
    ws.update_planning({"context": {"objective": "Assess payments"}, "apm_markdown": COMPLETE_APM})
    rows = [
        _row(ws, "Duplicate payments are made"),
        _row(ws, "Payments are split to stay under the approval limit"),
        _row(ws, "Payments are made without approval"),
    ]
    stamp_planning_cycle(ws)
    return ws.reload(), [row["id"] for row in rows]


def test_the_loop_trims_the_matrix_through_one_revision(monkeypatch, workspace_with_data):
    ws, ids = _planned_workspace(workspace_with_data)
    instruction = "Trim the matrix to the two core risks"

    def revision(user: str) -> dict:
        payload = json.loads(user)
        assert sorted(row["id"] for row in payload["ROWS IN SCOPE"]) == sorted(ids)
        assert payload["auditor_instruction"] == instruction
        return {
            "summary": "Three rows in scope, two after the revision.",
            "decisions": [
                {"rcm_id": ids[0], "decision": "keep", "reason": ""},
                {"rcm_id": ids[1], "decision": "merge", "into": ids[2], "reason": "Same failure."},
                {
                    "rcm_id": ids[2],
                    "decision": "revise",
                    "changes": {"risk": "Payments are made without approval within limits"},
                    "reason": "Widened to cover splitting.",
                },
            ],
            "additions": [],
        }

    script = LoopScript(
        tool_turn(
            "run_outcomes",
            {"requested_outcomes": ["planning.rcm_revised"], "instruction": instruction},
        ),
        tool_turn(
            "run_outcomes",
            {"requested_outcomes": ["planning.rcm_revised"], "instruction": instruction},
            "call_2",
        ),
        finish_turn("Trimmed."),
    )
    fake = configured(monkeypatch, script, {"agent:rcm_revision": revision})

    run = wait_run(ws, start_loop(ws, instruction)["id"], timeout=90)

    assert run["status"] == "completed", run.get("summary_markdown")
    ws = ws.reload()
    assert sorted(row["id"] for row in ws.rcm) == sorted([ids[0], ids[2]])
    survivor = next(row for row in ws.rcm if row["id"] == ids[2])
    assert survivor["risk"] == "Payments are made without approval within limits"
    assert [entry["merged_into"] for entry in ws.retired_rcm_rows()] == [ids[2]]
    # The second identical request is answered from the record, not re-asked.
    assert [call["tag"] for call in fake.calls].count("agent:rcm_revision") == 1
    key = planning_revision.request_key("rcm", instruction=instruction)
    assert planning_revision.load(ws, "rcm", key)["run_id"] in run["children"]


def test_a_redraft_does_not_bring_back_a_risk_someone_retired(monkeypatch, workspace_with_data):
    from app.agent.executors import planning as planning_executors

    ws, ids = _planned_workspace(workspace_with_data)
    retired = next(row for row in ws.rcm if row["id"] == ids[0])
    ws.remove_rcm(ids[0], reason="Covered by another engagement")
    ws = ws.reload()

    assert planning_executors.match_retired_rcm(
        ws, {"process": "Payments", "risk": retired["risk"]}
    ) is not None
    assert planning_executors.match_retired_rcm(
        ws, {"process": "Payments", "risk": "Vendor bank details are changed without review"}
    ) is None


def test_a_revision_is_refused_while_the_matrix_is_out_of_date(workspace_with_data):
    from app.agent.capabilities import REGISTRY

    ws, _ids = _planned_workspace(workspace_with_data)
    capability = REGISTRY.get("planning.rcm_revised")
    scope = {"target_refs": ["workspace:current"], "instruction": "Trim it"}
    assert capability.readiness(ws, scope).state == "missing"
    assert capability.readiness(ws, {"target_refs": ["workspace:current"]}).state == "blocked"

    # Stamp the rows against this memorandum, then move the memorandum.
    from app.workspace_transactions import parent_hashes

    stamps = parent_hashes(ws, ["planning:apm"])
    for row in ws.rcm:
        ws.update_rcm(row["id"], {"workflow_parents": stamps}, agent=True)
    ws = ws.reload()
    ws.update_planning({"apm_markdown": COMPLETE_APM + "\nA new paragraph."})

    readiness = capability.readiness(ws.reload(), scope)
    assert readiness.state == "blocked"
    assert "out of date" in readiness.reasons[0]


def test_the_loop_rewrites_one_memorandum_section(monkeypatch, workspace_with_data):
    ws, _ids = _planned_workspace(workspace_with_data)
    owner = ws.planning.get("created_by")
    script = LoopScript(
        tool_turn(
            "run_outcomes",
            {
                "requested_outcomes": ["planning.apm_revised"],
                "instruction": "State that management override is presumed in two sentences",
            },
        ),
        finish_turn("Rewrote the fraud section."),
    )
    configured(
        monkeypatch,
        script,
        {
            "agent:apm_revision": {
                "summary": "Rewrote the fraud section.",
                "sections": [
                    {
                        "heading": "Fraud risk and management override",
                        "markdown": "Management override is presumed. It is tested through journals.",
                        "reason": "Asked for two sentences.",
                    }
                ],
            }
        },
    )

    run = wait_run(ws, start_loop(ws, "Tighten the fraud section")["id"], timeout=90)

    assert run["status"] == "completed", run.get("summary_markdown")
    ws = ws.reload()
    memo = ws.planning["apm_markdown"]
    assert "It is tested through journals." in memo
    assert markdown_sections.section_headings(memo) == markdown_sections.section_headings(COMPLETE_APM)
    # Every other section is untouched, and so is who owns the memorandum.
    assert markdown_sections.section_body(memo, "Engagement") == "Entity and scope."
    assert ws.planning.get("created_by") == owner


# --------------------------------------------------------------------------- #
# Editing a long document by passage
# --------------------------------------------------------------------------- #
def test_a_document_is_searched_and_read_in_numbered_windows(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"apm_markdown": COMPLETE_APM})

    found = markdown_artifacts.search(ws, "planning:apm", "override")
    assert found["match_count"] >= 1
    assert found["matches"][0]["section"] == "Fraud risk and management override"

    section = markdown_artifacts.read(ws, "planning:apm", section="Engagement")
    assert "Entity and scope." in section["text"]
    assert section["sha1"] == markdown_artifacts.text_sha1(COMPLETE_APM)
    first = markdown_artifacts.read(ws, "planning:apm")
    assert first["outline"][0]["heading"] == "Audit Planning Memorandum"


def test_an_edit_names_one_passage_of_the_version_it_read(workspace_with_data):
    ws = workspace_with_data
    ws.update_planning({"apm_markdown": COMPLETE_APM})
    sha1 = markdown_artifacts.text_sha1(COMPLETE_APM)

    with pytest.raises(WorkspaceError, match="does not occur"):
        markdown_artifacts.edit(ws, "planning:apm", "Not there", "x", expected_sha1=sha1)
    with pytest.raises(WorkspaceError, match="section heading"):
        markdown_artifacts.edit(
            ws, "planning:apm", "## Prior audit findings", "## Earlier findings", expected_sha1=sha1
        )
    with pytest.raises(WorkspaceError, match="changed since it was read"):
        markdown_artifacts.edit(
            ws, "planning:apm", "Entity and scope.", "Entity.", expected_sha1="stale"
        )

    markdown_artifacts.edit(
        ws, "planning:apm", "Entity and scope.", "Global Bank, procurement.", expected_sha1=sha1
    )
    assert "Global Bank, procurement." in ws.reload().planning["apm_markdown"]


def test_the_loop_edits_a_passage_and_is_not_offered_the_whole_memo_rewrite(
    monkeypatch, workspace_with_data
):
    ws = workspace_with_data
    ws.update_planning({"apm_markdown": COMPLETE_APM})
    assert "edit_apm" not in loop_tools.action_tool_names()
    assert "edit_document_text" in loop_tools.action_tool_names()

    def edit_turn(_script):
        return tool_turn(
            "edit_document_text",
            {
                "target": {"kind": "planning", "id": "apm"},
                "args": {
                    "old_str": "Payments background.",
                    "new_str": "Procurement background.",
                    "sha1": markdown_artifacts.text_sha1(COMPLETE_APM),
                },
            },
            "call_edit",
        )

    script = LoopScript(
        tool_turn("search_document", {"ref": "planning:apm", "pattern": "background"}),
        edit_turn,
        finish_turn("Edited one sentence."),
    )
    configured(monkeypatch, script)

    run = wait_run(ws, start_loop(ws, "Say procurement, not payments, in the background")["id"], timeout=90)

    assert run["status"] == "completed", run.get("summary_markdown")
    memo = ws.reload().planning["apm_markdown"]
    assert "Procurement background." in memo
    assert "Payments background." not in memo
    edit = next(item for item in run["actions"] if item["type"] == "edit_document_text")
    assert edit["status"] == "succeeded"


def test_revision_outcomes_are_published_on_the_artifacts_they_change():
    rcm = {item["outcome"]: item for item in operations.artifact_operations("rcm")["outcomes"]}
    assert rcm["planning.rcm_revised"]["accepts_this_ref"] is True
    assert rcm["planning.rcm_revised"]["redraws_when_named"] is True
    planning = {item["outcome"] for item in operations.artifact_operations("planning")["outcomes"]}
    assert "planning.apm_revised" in planning


def test_a_change_assessment_is_applied_through_the_matrix_revision(
    monkeypatch, workspace_with_data
):
    """The bridge: assess, then revise by the assessment's basis, no words needed."""

    from app import documents, planning_delta

    ws, ids = _planned_workspace(workspace_with_data)
    document = documents.add_document(
        ws, "policy.txt", b"Payments above 50,000 now need two approvers."
    )
    ws = ws.reload()
    assessment = {
        "impact": "rcm",
        "summary": "The policy adds a second approver above 50,000.",
        "apm_changes": [],
        "rcm_changes": [
            {
                "rcm_id": ids[2],
                "change": "revise",
                "summary": "Require two approvers above 50,000.",
                "reason": "The policy changed.",
            }
        ],
    }
    seen: dict = {}

    def revision(user: str) -> dict:
        seen["instruction"] = json.loads(user)["auditor_instruction"]
        return {
            "summary": "One row revised for the new approval rule.",
            "decisions": [
                {"rcm_id": ids[0], "decision": "keep", "reason": ""},
                {"rcm_id": ids[1], "decision": "keep", "reason": ""},
                {
                    "rcm_id": ids[2],
                    "decision": "revise",
                    "changes": {"control": "Two approvers sign payments above 50,000"},
                    "reason": "The policy changed.",
                },
            ],
            "additions": [],
        }

    def revise_turn(_script):
        basis = planning_delta.basis_sha1(ws.reload(), [document["id"]])
        return tool_turn(
            "run_outcomes",
            {"requested_outcomes": ["planning.rcm_revised"], "from_assessment": basis},
            "call_revise",
        )

    script = LoopScript(
        tool_turn("assess_change", {"document_ids": [document["id"]]}),
        revise_turn,
        finish_turn("Revised the approval row."),
    )
    configured(
        monkeypatch,
        script,
        {"agent:delta_review": assessment, "agent:rcm_revision": revision},
    )

    run = wait_run(ws, start_loop(ws, f"I uploaded {document['id']}; update the plan")["id"], timeout=90)

    assert run["status"] == "completed", run.get("summary_markdown")
    assert "Require two approvers above 50,000." in seen["instruction"]
    row = next(item for item in ws.reload().rcm if item["id"] == ids[2])
    assert row["control"] == "Two approvers sign payments above 50,000"


# --------------------------------------------------------------------------- #
# The registered command
# --------------------------------------------------------------------------- #
def test_the_revise_command_takes_the_message_as_its_instruction(
    monkeypatch, workspace_with_data
):
    """The shortcut the chat reached for instead of the loop, made to do the work.

    "Generate RCM" was described as able to revise, was chosen for a request to
    shorten the matrix, found a current matrix, and reported success having
    done nothing. The revision has its own command now, and the message that
    starts it is what it is asked to do.
    """

    from app.agent import runner, store

    ws, ids = _planned_workspace(workspace_with_data)
    message = "the rcm has 3 rows. keep only the core one"
    seen: dict = {}

    def revision(user: str) -> dict:
        seen["instruction"] = json.loads(user)["auditor_instruction"]
        return {
            "summary": "Three rows in scope, one after the revision.",
            "decisions": [
                {"rcm_id": ids[0], "decision": "keep", "reason": ""},
                {"rcm_id": ids[1], "decision": "retire", "reason": "Not core."},
                {"rcm_id": ids[2], "decision": "retire", "reason": "Not core."},
            ],
            "additions": [],
        }

    configured(monkeypatch, LoopScript(finish_turn("unused")), {"agent:rcm_revision": revision})

    started = runner.start_command_run(
        ws,
        "auto",
        {"source": "goal_template", "text": message, "goal_template": "rcm_revision"},
    )
    run = wait_run(ws, started["id"], timeout=90)

    assert run["engine"] == "workflow"
    assert run["status"] == "completed", run.get("summary_markdown")
    assert seen["instruction"] == message
    assert [row["id"] for row in ws.reload().rcm] == [ids[0]]


def test_the_generate_commands_no_longer_claim_to_revise():
    from app.agent import commands

    for command_id in ("generate_rcm", "generate_apm"):
        description = commands.COMMANDS[command_id].description
        assert "Leaves a current" in description
    assert commands.COMMANDS["revise_rcm"].goal_template == "rcm_revision"
    assert commands.COMMANDS["revise_apm"].goal_template == "apm_revision"
