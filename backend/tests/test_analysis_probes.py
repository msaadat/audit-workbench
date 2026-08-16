"""The deterministic probe sweep: what a frame's own data already asserts.

These tests pin the judgments the sweep makes on its own, before any model turn
sees a frame. Each one stands for a nomination that was missed, or a piece of
noise that crowded a real one out, on a measured engagement.
"""

from __future__ import annotations

import polars as pl

from app import workspaces
from app.agent import probes
from app.agent.context.adapters import analysis_definition_scope
from app.agent.workers.analysis import (
    MAX_MEASURED_ANALYSES,
    MAX_PROPOSED_ANALYSES,
    proposal_budget,
)


def _rows(count: int, start: int = 1) -> list[int]:
    return list(range(start, start + count))


def _workspace(name: str, **frames: pl.DataFrame) -> workspaces.Workspace:
    ws = workspaces.create_workspace(name)
    for table, frame in frames.items():
        ws.add_table(f"{table}.csv", frame.write_csv().encode())
    return ws


def _find(nominations: list[dict], test: str, **params) -> dict | None:
    for item in nominations:
        if item["test"] != test:
            continue
        if all(item["params"].get(key) == value for key, value in params.items()):
            return item
    return None


# --------------------------------------------------------------- referential
def test_an_unresolved_reference_is_nominated_with_where_it_does_resolve():
    """The 1,054M case: a link column carrying another table's key."""
    invoices = _workspace(
        "Orphan links",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(30)],
                # Five of thirty name a requisition instead of a purchase order.
                "PO_NUMBER_LINK": [
                    f"REQ{n:03d}" if n <= 5 else f"PO{n:03d}" for n in _rows(30)
                ],
            }
        ),
        po_data=pl.DataFrame({"PO_NUMBER": [f"PO{n:03d}" for n in _rows(30)]}),
        requisitions=pl.DataFrame({"REQUISITION_ID": [f"REQ{n:03d}" for n in _rows(30)]}),
    )
    found = probes.probe_frame(invoices, "invoice_data")
    nomination = _find(found, "referential", column="PO_NUMBER_LINK")
    assert nomination is not None
    assert nomination["params"]["lookup_table"] == "po_data"
    assert nomination["flagged"] == 5
    # The diagnosis, not just the count: the values are another table's key, and
    # saying so is the difference between a data-quality note and the finding.
    assert "requisitions.REQUISITION_ID" in nomination["reading"]


def test_a_reference_resolving_nowhere_is_the_strongest_nomination():
    """The buyer identifier that reconciles to no imported master."""
    ws = _workspace(
        "No master",
        po_data=pl.DataFrame(
            {
                "PO_NUMBER": [f"PO{n:03d}" for n in _rows(30)],
                "BUYER_ID": [f"B{n % 6:03d}" for n in _rows(30)],
            }
        ),
        staff_details=pl.DataFrame({"STAFF_ID": [1000 + n for n in _rows(30)]}),
    )
    nomination = _find(probes.probe_frame(ws, "po_data"), "referential", column="BUYER_ID")
    assert nomination is not None
    assert nomination["flagged"] == nomination["tested"] == 30
    assert "resolve in no imported key" in nomination["reading"]


def test_a_reference_aimed_at_the_wrong_master_is_not_a_finding():
    """Name affinity offers every role column against every dimension key.

    Every value failing *and* every value being some other table's key means the
    sweep asked the wrong question, not that the population is broken.
    """
    ws = _workspace(
        "Wrong master",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(30)],
                "VERIFIED_BY_ID": [1000 + (n % 5) for n in _rows(30)],
            }
        ),
        staff_details=pl.DataFrame({"STAFF_ID": [1000 + n for n in range(5)]}),
        vendor_master_file=pl.DataFrame({"VENDOR_ID": [f"V{n:03d}" for n in _rows(20)]}),
    )
    found = probes.probe_frame(ws, "invoice_data")
    # It reconciles cleanly against staff and so is not nominated at all; the
    # vendor master, where nothing matches, must not be nominated either.
    assert _find(found, "referential", column="VERIFIED_BY_ID") is None


# ---------------------------------------------------------------- comparison
def test_the_breached_direction_wins_over_the_one_equality_makes_vacuous():
    """Two mostly-equal columns satisfy both directions; only one says anything.

    An invoice billed at exactly its purchase-order total is both at most and at
    least that total, so ``≥`` holds on every row while ``≤`` holds on all but
    the overbilled ones. Taking the higher rate takes the empty direction — which
    is how a 79.75M overbilling went unproposed and its mirror image was recorded
    as a confirmed invariant.
    """
    assert probes._chosen_direction(
        [("le", 93, 96, frozenset({1, 2, 3})), ("ge", 96, 96, frozenset())]
    ) == ("le", 93, 96, frozenset({1, 2, 3}))


def test_two_columns_equal_wherever_both_are_present_are_not_an_ordering():
    """Neither direction breached means an identity, which is not a comparison."""
    assert (
        probes._chosen_direction(
            [("le", 50, 50, frozenset()), ("ge", 50, 50, frozenset())]
        )
        is None
    )


def test_an_amount_ceiling_the_population_breaks_is_nominated():
    ws = _workspace(
        "Ceiling",
        billing=pl.DataFrame(
            {
                "DOC_ID": [f"D{n:03d}" for n in _rows(30)],
                # Equal everywhere but three rows that exceed the ceiling.
                "INVOICE_AMOUNT": [100 + (900 if n <= 3 else 0) for n in _rows(30)],
                "PO_TOTAL_AMOUNT": [100 for _ in _rows(30)],
            }
        ),
    )
    nomination = _find(
        probes.probe_frame(ws, "billing"),
        "compare_columns",
        column="INVOICE_AMOUNT",
        other="PO_TOTAL_AMOUNT",
    )
    assert nomination is not None
    assert nomination["params"]["op"] == "le"
    assert nomination["flagged"] == 3


def test_orderings_broken_by_the_same_rows_collapse_to_one_nomination():
    """Eight date columns restate one finding until it takes the whole budget."""
    early = [n <= 4 for n in _rows(30)]
    ws = _workspace(
        "One finding, many orderings",
        events=pl.DataFrame(
            {
                "DOC_ID": [f"D{n:03d}" for n in _rows(30)],
                "PO_DATE": ["2024-06-01"] * 30,
                # The same four rows precede the order on every later date.
                "INVOICE_DATE": [
                    "2024-05-01" if flag else "2024-07-01" for flag in early
                ],
                "PAYMENT_DATE": [
                    "2024-05-02" if flag else "2024-07-02" for flag in early
                ],
            }
        ),
    )
    found = [
        item
        for item in probes.probe_frame(ws, "events")
        if item["family"] == probes.COMPARISON and item["flagged"]
    ]
    assert len(found) == 1
    assert found[0]["evidence"]["equivalent_orderings"]
    assert "The same 4 rows also breach" in found[0]["reading"]


def test_a_frame_too_small_to_carry_a_rate_gets_no_comparison():
    ws = _workspace(
        "Tiny",
        small=pl.DataFrame(
            {
                "DOC_ID": ["A", "B", "C", "D"],
                "FIRST_DATE": ["2024-01-01"] * 4,
                "SECOND_DATE": ["2024-02-01", "2024-02-01", "2024-02-01", "2023-01-01"],
            }
        ),
    )
    assert not [
        item
        for item in probes.probe_frame(ws, "small")
        if item["family"] == probes.COMPARISON
    ]


# ------------------------------------------------------------------ equality
def test_two_roles_over_one_domain_are_expected_to_differ():
    """Segregation of duties: two columns of one table, no join involved."""
    ws = _workspace(
        "Roles",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(30)],
                "VERIFIED_BY_ID": [1000 + (n % 6) for n in _rows(30)],
                # Two rows verified and approved by the same person.
                "SUPERVISOR_APPROVAL_ID": [
                    1000 + (n % 6) if n <= 2 else 1000 + ((n + 3) % 6) for n in _rows(30)
                ],
            }
        ),
    )
    nomination = _find(
        probes.probe_frame(ws, "invoice_data"),
        "compare_columns",
        column="VERIFIED_BY_ID",
        other="SUPERVISOR_APPROVAL_ID",
    )
    assert nomination is not None
    assert nomination["params"]["op"] == "ne"
    assert nomination["evidence"]["reading"] == "two_roles"
    assert nomination["evidence"]["equal_rows"] == 2


def test_one_attribute_stated_twice_is_expected_to_agree():
    """The same shape statistically, the opposite comparison in audit terms."""
    ws = _workspace(
        "Attribute",
        requisitions=pl.DataFrame(
            {
                "REQ_ID": [f"R{n:03d}" for n in _rows(30)],
                "REQUESTER_DEPARTMENT": ["IT" if n % 2 else "Finance" for n in _rows(30)],
                # Agrees on ten rows, contradicts the master on twenty.
                "DEPARTMENT": [
                    ("IT" if n % 2 else "Finance") if n <= 10 else "Operations"
                    for n in _rows(30)
                ],
            }
        ),
    )
    nomination = _find(
        probes.probe_frame(ws, "requisitions"),
        "compare_columns",
        column="REQUESTER_DEPARTMENT",
        other="DEPARTMENT",
    )
    assert nomination is not None
    assert nomination["params"]["op"] == "eq"
    assert nomination["evidence"]["reading"] == "same_attribute"
    assert nomination["evidence"]["differing_rows"] == 20


# ---------------------------------------------------- duplicates and formats
def test_a_near_unique_identifier_is_probed_for_repeats():
    ws = _workspace(
        "Repeats",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(30)],
                # Two references reused once each: 29 repeats 1, 30 repeats 2.
                "VENDOR_INVOICE_NUMBER": [
                    f"VINV{(n - 28 if n > 28 else n):03d}-2024" for n in _rows(30)
                ],
            }
        ),
    )
    nomination = _find(
        probes.probe_frame(ws, "invoice_data"),
        "duplicates",
        columns=["VENDOR_INVOICE_NUMBER"],
    )
    assert nomination is not None
    assert nomination["flagged"] == 4


def test_a_near_unique_date_or_amount_is_not_a_repeated_key():
    """Rows sharing a date share it by arithmetic, not by anybody's intent."""
    ws = _workspace(
        "Continuous",
        ledger=pl.DataFrame(
            {
                "DOC_ID": [f"D{n:03d}" for n in _rows(30)],
                "POSTED_DATE": [f"2024-01-{min(n, 28):02d}" for n in _rows(30)],
                "AMOUNT": [float(min(n, 28)) for n in _rows(30)],
            }
        ),
    )
    found = probes.probe_frame(ws, "ledger")
    assert _find(found, "duplicates", columns=["POSTED_DATE"]) is None
    assert _find(found, "duplicates", columns=["AMOUNT"]) is None


def test_a_minority_identifier_format_is_nominated():
    ws = _workspace(
        "Formats",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(40)],
                # Three references built unlike the thirty-seven around them.
                "VENDOR_INVOICE_NUMBER": [
                    f"SUSP{n:03d}" if n <= 3 else f"VINV{n:03d}-202401" for n in _rows(40)
                ],
            }
        ),
    )
    nomination = _find(
        probes.probe_frame(ws, "invoice_data"),
        "format_anomaly",
        column="VENDOR_INVOICE_NUMBER",
    )
    assert nomination is not None
    assert nomination["flagged"] == 3


# --------------------------------------------------------------------- shape
def test_no_family_can_take_the_whole_budget():
    ws = _workspace(
        "Crowded",
        wide=pl.DataFrame(
            {
                "DOC_ID": [f"D{n:03d}" for n in _rows(40)],
                "VENDOR_REF": [f"VR{min(n, 38):03d}" for n in _rows(40)],
                **{
                    f"DATE_{index}": [
                        f"2024-0{index + 1}-{min(n, 28):02d}" for n in _rows(40)
                    ]
                    for index in range(6)
                },
            }
        ),
    )
    found = probes.probe_frame(ws, "wide")
    assert len(found) <= probes.MAX_NOMINATIONS_PER_FRAME
    for family, cap in probes.MAX_PER_FAMILY.items():
        assert len([item for item in found if item["family"] == family]) <= cap


def test_a_nomination_carries_counts_and_never_a_value():
    ws = _workspace(
        "No values",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(30)],
                "PO_NUMBER_LINK": [f"PO{n:03d}" if n > 3 else "MISSING" for n in _rows(30)],
            }
        ),
        po_data=pl.DataFrame({"PO_NUMBER": [f"PO{n:03d}" for n in _rows(30)]}),
    )
    nomination = _find(
        probes.probe_frame(ws, "invoice_data"), "referential", column="PO_NUMBER_LINK"
    )
    assert nomination is not None
    assert set(nomination) >= {"frame", "family", "test", "params", "tested", "flagged"}
    # The spec and the counts, and nothing that names a row.
    assert "INV004" not in str(nomination)


def test_a_sweep_is_stable_across_runs():
    ws = _workspace(
        "Stable",
        ledger=pl.DataFrame(
            {
                "DOC_ID": [f"D{n:03d}" for n in _rows(30)],
                "OPENED": ["2024-01-01"] * 30,
                "CLOSED": ["2024-02-01" if n > 2 else "2023-01-01" for n in _rows(30)],
            }
        ),
    )
    first = probes.probe_frame(ws, "ledger")
    assert first == probes.probe_frame(ws, "ledger")


def test_measured_findings_widen_a_frame_beyond_its_size_budget():
    """The size budget stops padding, and a measured finding is not padding.

    Observed: a frame carrying ten measured findings saved four of them, because
    four was all its row count allowed. The six that did not fit included the
    invoices dated before their own purchase order.
    """
    # Size alone is unchanged where nothing has been measured.
    assert proposal_budget(118) == MAX_PROPOSED_ANALYSES
    assert proposal_budget(12) == 2
    # A frame with more measured findings than slots gets the slots.
    assert proposal_budget(118, 10) == MAX_MEASURED_ANALYSES
    assert proposal_budget(12, 5) == 5
    # Never narrower than the size budget, and never unbounded.
    assert proposal_budget(118, 1) == MAX_PROPOSED_ANALYSES
    assert proposal_budget(118, 500) == MAX_MEASURED_ANALYSES


def test_probe_findings_reach_the_definition_context():
    ws = _workspace(
        "Wired",
        invoice_data=pl.DataFrame(
            {
                "INVOICE_ID": [f"INV{n:03d}" for n in _rows(30)],
                "PO_NUMBER_LINK": [f"PO{n:03d}" if n > 3 else "MISSING" for n in _rows(30)],
            }
        ),
        po_data=pl.DataFrame({"PO_NUMBER": [f"PO{n:03d}" for n in _rows(30)]}),
    )
    found = probes.probe_frame(ws, "invoice_data")
    scope = analysis_definition_scope(ws, "invoice_data", probe_findings=found)
    supplied = scope.candidates["probe_findings"]
    assert len(supplied) == len(found)
    assert {item.source["test"] for item in supplied} == {item["test"] for item in found}
    # Absent by default rather than fabricated: a frame nobody swept supplies none.
    assert not analysis_definition_scope(ws, "invoice_data").candidates["probe_findings"]
