"""The data map and the population profile, held to the two sample packs.

The map names no column of any engagement, so it is tested on two that share
nothing: procurement (a one-to-one requisition, order and invoice chain with a
staff master and an approval matrix) and expenses (claims with line items
beneath them, an approver list kept outside the employee file, a limit and an
account code among the numbers). What each asserts is a figure the hand-written
benchmark in ``docs/sample-procurement/eda-benchmark`` establishes, or one of
the four ways the first prototype misread expenses.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app import analysis_payloads, analytics, planning_cycle, workspaces
from app.agent import data_map
from app.agent.capabilities import analysis as analysis_capabilities
from app.agent.workflows import analysis as analysis_workflow

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _workspace(name: str, folder: Path, pattern: str) -> workspaces.Workspace:
    ws = workspaces.create_workspace(name)
    for path in sorted(folder.glob(pattern)):
        ws.add_table(path.name, path.read_bytes())
    return workspaces.load_workspace(ws.id)


@pytest.fixture
def procurement():
    return _workspace("Procurement map", DOCS / "sample-procurement" / "data", "*.xlsx")


@pytest.fixture
def expenses():
    return _workspace("Expenses map", DOCS / "sample-expense" / "data", "*.csv")


def _table(ws, stem: str) -> str:
    return next(name for name in ws.table_names() if stem in name)


def _run(ws, definition: dict) -> dict:
    return analysis_payloads.compute_payload(ws, {**definition, "id": "probe"})


def _flagged(payload: dict, column: str) -> set[str]:
    frame = payload["exceptions"] or {"columns": [], "rows": []}
    index = frame["columns"].index(column)
    return {str(row[index]) for row in frame["rows"]}


def _by_test(definitions: list[dict], test: str) -> list[dict]:
    return [item for item in definitions if item["spec"]["test"] == test]


# --------------------------------------------------------------------- the map
def test_the_procurement_map_reads_the_process_from_the_data(procurement):
    mapping = data_map.attach_people(data_map.build_data_map(procurement))
    requisitions, orders, invoices = (_table(procurement, s) for s in ("requisition", "po", "invoice"))
    staff, vendors, matrix = (_table(procurement, s) for s in ("staff", "vendor", "matrix"))

    assert set(mapping["facts"]) == {requisitions, orders, invoices}
    assert mapping["chain"]["tables"] == [requisitions, orders, invoices]
    assert not mapping["children"]
    # The process order the data puts the dates in, with who did each step.
    steps = [(s["column"], s["person"]) for s in mapping["chain"]["steps"]]
    assert steps[:6] == [
        ("REQUISITION_DATE", "REQUESTER_ID"),
        ("VERIFIED_DATE", "VERIFIED_BY_ID"),
        ("APPROVAL_DATE", "APPROVED_BY_ID"),
        ("FIN_APPROVAL_DATE", "FIN_APPROVED_BY_ID"),
        ("PO_DATE", None),
        ("GRN_DATE", None),
    ]
    assert ("SUPERVISOR_APPROVAL_DATE", "SUPERVISOR_APPROVAL_ID") in steps
    assert {r["master"] for r in mapping["roles"]} == {staff}
    assert [c["master"] for c in mapping["counterparties"]] == [vendors]
    assert mapping["counterparties"][0]["status"] == "VENDOR_STATUS"
    assert [(l["table"], l["limit"], l["via"]) for l in mapping["lookups"]] == [
        (matrix, "MAX_APPROVAL_AMOUNT", "JOB_TITLE")
    ]
    # An invoice cites its goods receipt beside its order: a second link the
    # chain does not use, and the one an unmatched payment leaves blank.
    assert any(l["column"] == "GRN_ID_LINK" for l in mapping["chain"]["links"])


def test_the_expenses_map_keeps_children_limits_codes_and_aliases_apart(expenses):
    """The four misreadings of the first prototype, one assertion each."""
    mapping = data_map.build_data_map(expenses)
    claims, lines, approvals, ledger = (
        _table(expenses, s) for s in ("expense_claims", "claim_line_items", "approval_log", "gl_postings")
    )
    # Grain: a claim's lines hang beneath it and never join the chain.
    assert [(c["table"], c["parent"]) for c in mapping["children"]] == [(lines, claims)]
    assert lines not in mapping["chain"]["tables"]
    assert mapping["chain"]["tables"][0] == claims
    # Typing: a delegated limit and an account code are not the amount.
    assert mapping["tables"][approvals]["amounts"] == []
    assert "delegated_limit_pkr" in mapping["tables"][approvals]["limits"]
    assert "gl_account" in mapping["tables"][ledger]["codes"]
    # A cast with no master is still a cast of roles.
    assert {"table": approvals, "column": "approver_id", "master": None} in mapping["roles"]
    # One category under two headings is one category.
    aliased = {(a["table"], a["column"]) for a in mapping["aliases"]}
    assert (claims, "cost_center") in aliased
    assert (approvals, "approver_name") in aliased
    assert "cost_center" not in mapping["tables"][claims]["dimensions"]


# ----------------------------------------------------------------- the profile
def test_the_procurement_profile_reaches_the_benchmark(procurement):
    definitions = data_map.profile_definitions(procurement, data_map.build_data_map(procurement))
    assert all(item["semantic_id"].startswith(data_map.PROFILE_PREFIX) for item in definitions)
    assert len({item["semantic_id"] for item in definitions}) == len(definitions)

    skipped = _run(procurement, _by_test(definitions, "skipped_steps")[0])
    assert _flagged(skipped, "REQUISITION_ID") == {
        # P-03: procurement approval blank, ordered anyway.
        "REQ2024116", "REQ2024130", "REQ2024152",
        # P-08: paid with no approval; P-06: paid with no receipt reference.
        "REQ2024104", "REQ2024131", "REQ2024154",
        "REQ2024110", "REQ2024132", "REQ2024147",
    }
    overlap = _run(procurement, _by_test(definitions, "role_overlap")[0])
    assert _flagged(overlap, "REQUISITION_ID") == {"REQ2024108", "REQ2024120", "REQ2024134", "REQ2024149"}
    patterns = {(row[0], row[1]): row[4] for row in overlap["frame"]["rows"]}
    assert patterns[("VERIFIED_BY_ID", next(k[1] for k in patterns if k[1].startswith("VERIFIED_BY_ID_")))] == 92.3
    ratio = _run(procurement, _by_test(definitions, "ratio_consistency")[0])
    assert _flagged(ratio, "PO_NUMBER") == {"PO2024127"}
    reconciliation = _run(procurement, _by_test(definitions, "chain_reconciliation")[0])
    rows = {row[0]: row for row in reconciliation["frame"]["rows"]}
    columns = reconciliation["frame"]["columns"]
    assert rows["PO_TOTAL_AMOUNT"][columns.index("equal_to_previous")] == 52
    assert rows["INVOICE_AMOUNT"][columns.index("excess_over_previous")] == 12_553_800
    grids = [_run(procurement, item) for item in _by_test(definitions, "authority_grid")]
    assert sorted(len(grid["exceptions"]["rows"]) for grid in grids) == [2, 3]
    ids = _run(procurement, _by_test(definitions, "id_series")[0])
    assert _flagged(ids, "REQUISITION_ID") == {"REQ2024009"}


def test_profile_analyses_are_the_workflow_s_own_and_are_executed(procurement):
    """Descriptive tests are withheld from model proposals, not from execution."""
    descriptive = analytics.ids_with_signal(analytics.SIGNAL_DESCRIPTIVE)
    assert descriptive <= analysis_workflow.EXCLUDED_ANALYTICS_TEST_IDS
    definitions = data_map.profile_definitions(procurement, data_map.build_data_map(procurement))
    profile = {**_by_test(definitions, "value_bands")[0], "id": "A-1", "created_by": "agent"}
    authored = {**profile, "id": "A-2", "semantic_id": "analysis:x"}
    scope = analysis_capabilities.resolve_table_scope(procurement, {})
    kept = {item["id"] for item in analysis_capabilities.agent_analyses(
        type("W", (), {"analyses": [profile, authored]})(), scope)}
    assert kept == {"A-1"}


def test_every_planned_profile_analysis_executes(procurement, expenses):
    for ws in (procurement, expenses):
        for definition in data_map.profile_definitions(ws, data_map.build_data_map(ws)):
            assert _run(ws, definition)["error"] is None, definition["title"]


#: The memo's exhibit forms (``frontend/src/components/analysis/exhibitCharts``).
EXHIBIT_FORMS = {"hbar", "line", "share", "heatmap", "range", "strip", "bridge", "table"}


def test_every_profile_exhibit_names_columns_its_own_result_has(procurement, expenses):
    """A form that names a column the result lacks is drawn as a table instead —
    silently, which is the one way a chart spec can be wrong without failing."""
    seen = set()
    for ws in (procurement, expenses):
        for definition in data_map.profile_definitions(ws, data_map.build_data_map(ws)):
            payload = _run(ws, definition)
            viz, columns = payload["viz"], set(payload["frame"]["columns"])
            seen.add(viz["type"])
            assert viz["type"] in EXHIBIT_FORMS, definition["title"]
            named = [viz.get("x"), viz.get("row"), viz.get("flag"), *(viz.get("y") or []), *(viz.get("label") or [])]
            missing = [name for name in named if name and name not in columns]
            assert not missing, (definition["title"], viz, missing)
    # The profile reaches most of the forms on these two packs.
    assert {"hbar", "share", "heatmap", "range", "strip", "bridge"} <= seen


# -------------------------------------------------------------- the cycle side
def test_data_steps_bind_cycle_populations_and_find_steps_with_no_record():
    steps = [
        {"id": "S01", "table": "req", "columns": ["REQ_DATE", "REQUESTER_ID"]},
        {"id": "S02", "table": "req", "columns": ["APPROVAL_DATE", "APPROVED_BY"]},
        {"id": "S03", "table": "inv", "columns": ["INVOICE_DATE"]},
    ]
    proposal = {"name": "Cycle", "steps": [
        {"name": "Raise", "data_steps": ["S01"], "populations": [{"table": "req", "anchor": True}]},
        {"name": "Approve", "data_steps": ["S02"], "populations": [{"table": "req", "columns": ["WRONG"]}]},
        {"name": "Refer excess", "data_steps": [], "populations": [{"table": "inv", "columns": ["AMOUNT"]}]},
        {"name": "Ghost", "data_steps": ["S09"]},
    ]}
    cycle, unknown = planning_cycle.expand_data_steps(proposal, steps)
    assert unknown == ["S09"]
    # The model named the step; local code wrote its columns over the guess.
    assert cycle["steps"][1]["populations"] == [{"table": "req", "columns": ["APPROVAL_DATE", "APPROVED_BY"]}]
    # A whole-table population stays whole; the step's dates are recorded
    # beside it rather than narrowing it to them.
    assert cycle["steps"][0]["populations"] == [
        {"table": "req", "anchor": True, "dates": ["REQ_DATE", "REQUESTER_ID"]}
    ]
    assert all("data_steps" not in step for step in cycle["steps"])
    unrecorded, unclaimed = planning_cycle.cycle_record_coverage(cycle, steps)
    assert unrecorded == ["Refer excess", "Ghost"]
    assert [s["id"] for s in unclaimed] == ["S03"]


def test_two_steps_on_one_whole_table_each_keep_their_own_date():
    """Treasury's shape: the deal and its capture are both the deals table.

    The capture step named its own data step and lost it, so coverage credited
    it with the deal date and warned that ``CAPTURE_TIMESTAMP`` recorded a step
    the cycle did not name. The same for the confirmation's receipt.
    """

    steps = [
        {"id": "S01", "table": "04_deals", "columns": ["DEAL_DATE", "COUNTERPARTY_ID"]},
        {"id": "S02", "table": "04_deals", "columns": ["CAPTURE_TIMESTAMP", "CAPTURED_BY_ID"]},
        {"id": "S03", "table": "05_confirmations", "columns": ["SENT_DATE", "CONFIRMED_BY_ID"]},
        {"id": "S04", "table": "05_confirmations", "columns": ["COUNTERPARTY_RECEIVED_DATE"]},
    ]
    proposal = {"name": "Treasury", "steps": [
        {"name": "Execute", "data_steps": ["S01"], "populations": [{"table": "04_deals"}]},
        {"name": "Capture", "data_steps": ["S02"], "populations": [{"table": "04_deals", "anchor": True}]},
        {"name": "Dispatch", "data_steps": ["S03"], "populations": [{"table": "05_confirmations"}]},
        {"name": "Receive", "data_steps": ["S04"], "populations": [{"table": "05_confirmations"}]},
    ]}
    cycle, unknown = planning_cycle.expand_data_steps(proposal, steps)
    assert unknown == []
    assert cycle["steps"][1]["populations"] == [
        {"table": "04_deals", "anchor": True, "dates": ["CAPTURE_TIMESTAMP", "CAPTURED_BY_ID"]}
    ]
    unrecorded, unclaimed = planning_cycle.cycle_record_coverage(cycle, steps)
    assert unrecorded == []
    assert unclaimed == []
    # And the shape validator keeps what the expansion wrote.
    stored = planning_cycle.validate_cycle_shape(
        cycle, allowed_types=set(), base_tables={"04_deals", "05_confirmations"}
    )
    assert stored["steps"][3]["populations"] == [
        {"table": "05_confirmations", "dates": ["COUNTERPARTY_RECEIVED_DATE"]}
    ]


# -------------------------------------------------------------- the library
def test_a_skipped_step_needs_a_later_step_to_have_happened():
    frame = pl.DataFrame({
        "raised": ["2024-01-01", "2024-01-01", "2024-01-01"],
        "approved": [None, None, "2024-01-03"],
        "paid": ["2024-01-09", None, "2024-01-09"],
    })
    result = analytics.run_test(frame, "skipped_steps", {"columns": ["raised", "approved", "paid"]})
    # Paid without approval was skipped; not yet approved and not paid is pending.
    assert result.detail.height == 1
    assert result.detail["skipped"].to_list() == ["approved"]


def test_role_overlap_flags_rare_coincidence_and_describes_common_ones():
    frame = pl.DataFrame({
        "requester": [f"P{i}" for i in range(20)],
        "checker": ["P0"] + ["F1"] * 19,
        "approver": ["F1"] * 20,
    })
    result = analytics.run_test(frame, "role_overlap", {"columns": ["requester", "checker", "approver"]})
    assert result.detail is not None and result.detail.height == 1
    rates = {(r["role"], r["other_role"]): r["rate_pct"] for r in result.summary.iter_rows(named=True)}
    assert rates[("checker", "approver")] == 95.0


def test_id_series_treats_a_child_identifier_as_its_parent_s():
    claims = [f"EXP-00{n}" for n in range(1, 7)]
    lines = [f"{claim}-L1" for claim in claims[:5]] + ["EXP-009-L1"]
    frame = pl.DataFrame({"claim": claims, "line": lines})
    result = analytics.run_test(frame, "id_series", {"columns": ["claim", "line"]})
    assert result.detail["claim"].to_list() == ["EXP-006"]
