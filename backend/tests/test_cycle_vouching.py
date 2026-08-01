"""Cycle vouching: population rows compared against linked, analyzed documents.

The unit of work is a transaction cycle — one anchor row plus every document that
names it, each in a role — so one item can carry a procurement chain (purchase
order, invoice, goods receipt) and compare its documents to each other as well as
to the population. Nothing here calls a model: every value on both sides was
extracted by the voucher analysis profile and is anchored to a verbatim citation,
so the comparison itself is deterministic and reproducible.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import doc_tests, document_analysis, documents, workspaces


def _analyzed(ws, filename: str, text: bytes, fields: dict, citations: list[dict]):
    """Attach a document with a pre-committed voucher analysis.

    The extraction itself is exercised in ``test_workflow_documents``; here the
    structured record is supplied directly so the comparison logic is tested
    without a provider call.
    """
    entry = documents.add_document(ws, filename, text, category="voucher")
    extracted = documents.extract_document(ws, entry["id"])
    reloaded = workspaces.load_workspace(ws.id)
    document = next(
        item for item in reloaded.documents if str(item["id"]) == str(entry["id"])
    )
    document_analysis.persist_analysis(
        reloaded,
        document,
        extracted,
        {
            "summary_markdown": "Record.",
            "audit_notes_markdown": "None.",
            "citations": citations,
            "fields": fields,
            "analysis_profile": "voucher",
        },
        provider="test",
        model="test",
    )
    return str(entry["id"])


def _citation(cid: str, excerpt: str) -> dict:
    return {"id": cid, "page": 1, "excerpt": excerpt}


@pytest.fixture
def procurement(tmp_path):
    """A two-row population with a three-document cycle on the first row."""

    ws = workspaces.create_workspace("Procurement cycle")
    frame = pl.DataFrame(
        {
            "po_number": ["PO-1001", "PO-1002"],
            "vendor": ["Acme Limited", "Globex"],
            "amount": [50000, 7500],
        }
    )
    path = ws.root / "Data" / "po_header.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_csv(path)
    ws.tables.append({"name": "po_header", "file": "po_header.csv"})
    ws.save()
    ws = workspaces.load_workspace(ws.id)

    _analyzed(
        ws,
        "PO-1001.txt",
        b"Purchase order PO-1001 to Acme Limited for PKR 50,000 dated 2025-03-01.",
        {
            "document_type": "purchase_order",
            "identifiers": [{"kind": "po_number", "value": "PO-1001", "citation": "C1"}],
            "parties": [{"role": "vendor", "name": "Acme Limited", "citation": "C1"}],
            "amounts": [{"kind": "total", "value": 50000.0, "citation": "C1"}],
            "dates": [{"kind": "document_date", "value": "2025-03-01", "citation": "C1"}],
        },
        [_citation("C1", "Purchase order PO-1001 to Acme Limited for PKR 50,000")],
    )
    ws = workspaces.load_workspace(ws.id)
    _analyzed(
        ws,
        "INV-77.txt",
        b"Invoice INV-77 against PO-1001 for PKR 50,000 dated 2025-03-20.",
        {
            "document_type": "invoice",
            "identifiers": [
                {"kind": "invoice_number", "value": "INV-77", "citation": "C1"},
                {"kind": "po_number", "value": "PO-1001", "citation": "C1"},
            ],
            "amounts": [{"kind": "total", "value": 50000.0, "citation": "C1"}],
            "dates": [{"kind": "invoice_date", "value": "2025-03-20", "citation": "C1"}],
        },
        [_citation("C1", "Invoice INV-77 against PO-1001 for PKR 50,000")],
    )
    ws = workspaces.load_workspace(ws.id)
    _analyzed(
        ws,
        "GRN-9.txt",
        b"Goods receipt GRN-9 for PO-1001 received 2025-03-10.",
        {
            "document_type": "goods_receipt",
            "identifiers": [
                {"kind": "grn_number", "value": "GRN-9", "citation": "C1"},
                {"kind": "po_number", "value": "PO-1001", "citation": "C1"},
            ],
            "dates": [{"kind": "delivery_date", "value": "2025-03-10", "citation": "C1"}],
        },
        [_citation("C1", "Goods receipt GRN-9 for PO-1001 received 2025-03-10")],
    )
    return workspaces.load_workspace(ws.id)


def _cycle_payload(**overrides) -> dict:
    payload = {
        "title": "Vouch purchase orders through the procurement cycle",
        "table": "po_header",
        "anchor_key": "po_number",
        "document_roles": [
            {"role": "purchase_order", "required": True},
            {"role": "invoice", "required": True},
            {"role": "goods_receipt", "required": True},
        ],
        "checks": [
            {
                "field": "vendor agrees to order",
                "method": "fuzzy",
                "tolerance": 0.85,
                "left": "row.vendor",
                "right": "purchase_order.party.vendor",
            },
            {
                "field": "invoice agrees to order",
                "method": "numeric_tolerance",
                "tolerance": 0,
                "left": "purchase_order.amount.total",
                "right": "invoice.amount.total",
            },
            {
                "field": "goods received before invoice",
                "method": "date_order",
                "left": "goods_receipt.date.delivery_date",
                "right": "invoice.date.invoice_date",
            },
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Chaining
# --------------------------------------------------------------------------- #
def test_one_row_carries_the_whole_cycle(procurement):
    """One anchor row links every document that names it, each in its role."""

    test = doc_tests.build_cycle_vouching(procurement, _cycle_payload())

    assert len(test["items"]) == 1
    item = test["items"][0]
    assert item["label"] == "PO-1001"
    assert {entry["role"] for entry in item["documents"]} == {
        "purchase_order",
        "invoice",
        "goods_receipt",
    }
    assert all(
        entry["matched_by"] == "extracted_identifier" for entry in item["documents"]
    )
    # The unlinked row is reported against the population, never dropped quietly.
    assert test["spec"]["coverage"] == {
        "population": 2,
        "linked": 1,
        "unlinked": 1,
        "incomplete_roles": 0,
    }
    assert "1 of 2 population row(s) have no linked document" in test["scope_limitations"]


def test_documents_are_compared_to_each_other_not_only_to_the_row(procurement):
    """The chaining case: both sides of a check may be different documents."""

    test = doc_tests.build_cycle_vouching(procurement, _cycle_payload())
    ws = workspaces.load_workspace(procurement.id)
    item = doc_tests.run_item(ws, test["id"], test["items"][0]["id"], run_id="test")

    assert item["state"] == "confirmed"
    by_field = {check["field"]: check for check in item["checks"]}

    # purchase_order -> invoice: neither side is the population row.
    order_to_invoice = by_field["invoice agrees to order"]
    assert order_to_invoice["verdict"] == "match"
    assert order_to_invoice["expected"] == 50000.0
    assert order_to_invoice["found"] == 50000.0
    sides = {entry["side"] for entry in order_to_invoice["comparisons"] if "side" in entry}
    assert sides == {"left", "right"}

    # goods_receipt -> invoice sequencing, again document to document.
    assert by_field["goods received before invoice"]["verdict"] == "match"
    # row -> purchase_order still works alongside them.
    assert by_field["vendor agrees to order"]["verdict"] == "match"


def test_cycle_exceptions_are_anchored_to_verbatim_citations(procurement):
    """A failed comparison carries the excerpt behind each side it read."""

    payload = _cycle_payload(
        checks=[
            {
                "field": "invoice agrees to order",
                "method": "numeric_tolerance",
                "tolerance": 0,
                "left": "row.amount",
                "right": "invoice.amount.total",
            }
        ]
    )
    # The population says 50,000 and so does the invoice, so force a difference
    # by testing the *other* row's expectation against this cycle.
    ws = workspaces.load_workspace(procurement.id)
    frame = ws.get_frame("po_header").with_columns(
        pl.when(pl.col("po_number") == "PO-1001")
        .then(pl.lit(49000))
        .otherwise(pl.col("amount"))
        .alias("amount")
    )
    frame.write_csv(ws.root / "Data" / "po_header.csv")
    ws = workspaces.load_workspace(ws.id)

    test = doc_tests.build_cycle_vouching(ws, payload)
    item = doc_tests.run_item(ws, test["id"], test["items"][0]["id"], run_id="test")

    assert item["state"] == "exception"
    check = item["checks"][0]
    assert check["verdict"] == "mismatch"
    assert check["expected"] == 49000
    assert check["found"] == 50000.0
    assert check["evidence_refs"], "a mismatch must carry the excerpt it read"
    assert "INV-77" in check["evidence_refs"][0]["excerpt"]


# --------------------------------------------------------------------------- #
# Role membership
# --------------------------------------------------------------------------- #
def test_a_role_accepts_the_document_types_the_test_declares(procurement):
    """Role membership is declared, not dictated by one model classification.

    A profile classifies each record independently, so an identically formatted
    pack can come back under a neighbouring type. Without a declared mapping that
    single word would take a whole cycle out of scope.
    """
    payload = _cycle_payload(
        document_roles=[
            {"role": "purchase_order", "required": True},
            # The invoice role also accepts a record classified as a credit note.
            {
                "role": "invoice",
                "required": True,
                "document_types": ["invoice", "credit_note"],
            },
            {"role": "goods_receipt", "required": False},
        ]
    )
    test = doc_tests.build_cycle_vouching(procurement, payload)
    item = test["items"][0]
    roles = {entry["document_id"]: entry["role"] for entry in item["documents"]}
    types = {entry["document_id"]: entry["document_type"] for entry in item["documents"]}
    assert "invoice" in roles.values()
    # The extracted type is preserved beside the role it was mapped into.
    assert "invoice" in types.values()
    assert item["missing_roles"] == []


def test_a_missing_required_role_goes_to_manual_review(procurement):
    """An incomplete cycle is surfaced, never silently confirmed."""

    payload = _cycle_payload(
        document_roles=[
            {"role": "purchase_order", "required": True},
            {"role": "invoice", "required": True},
            {"role": "goods_receipt", "required": True},
            {"role": "contract", "required": True},
        ]
    )
    test = doc_tests.build_cycle_vouching(procurement, payload)
    assert test["spec"]["coverage"]["incomplete_roles"] == 1
    assert "missing a required document role" in test["scope_limitations"]

    ws = workspaces.load_workspace(procurement.id)
    item = doc_tests.run_item(ws, test["id"], test["items"][0]["id"], run_id="test")
    assert item["state"] == "manual_review"
    assert "contract" in item["runner_note"]


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def test_paths_are_validated_when_the_check_is_written():
    """A path that could never resolve is rejected at definition time."""

    for path in ("", "row", "row.a.b", "invoice", "invoice.total", "invoice.nope.x"):
        with pytest.raises(workspaces.WorkspaceError):
            doc_tests._normalize_check(
                {"field": "f", "method": "normalized", "left": path, "right": "row.a"}
            )
    # Both shapes stay valid.
    doc_tests._normalize_check(
        {"field": "f", "method": "normalized", "left": "row.a", "right": "invoice.amount.total"}
    )
    doc_tests._normalize_check({"field": "f", "method": "normalized", "expected": "x"})


def test_unary_and_binary_methods_declare_the_sides_they_need():
    """``present`` reads one side; every other method needs both."""

    doc_tests._normalize_check(
        {"field": "f", "method": "present", "left": "invoice.attachment.receipt.present"}
    )
    with pytest.raises(workspaces.WorkspaceError):
        doc_tests._normalize_check(
            {"field": "f", "method": "numeric_tolerance", "left": "row.amount"}
        )
    with pytest.raises(workspaces.WorkspaceError):
        doc_tests._normalize_check({"field": "f", "method": "present", "expected": "x"})


def test_conflicting_matches_are_an_ambiguity_not_a_silent_choice(procurement):
    """Two differing values for one path must not be resolved by list order."""

    role_fields = {
        "invoice": [
            ("d1", {"amounts": [{"kind": "total", "value": 100.0, "citation": "C1"}]}, {}),
            ("d2", {"amounts": [{"kind": "total", "value": 250.0, "citation": "C1"}]}, {}),
        ]
    }
    matches = doc_tests.resolve_check_path(
        "invoice.amount.total", frozen={}, role_fields=role_fields
    )
    assert len(matches) == 2
    assert doc_tests._single_value(matches) == (None, "ambiguous")

    # The same value stated twice is one fact, not an ambiguity.
    same = [dict(matches[0]), dict(matches[0])]
    value, state = doc_tests._single_value(same)
    assert (value, state) == (100.0, "resolved")


def test_sequencing_and_presence_methods():
    """The two comparison shapes an equality method cannot express."""

    assert doc_tests.compare_values("2025-05-01", "2025-05-02", "date_order")["result"] == "match"
    assert doc_tests.compare_values("2025-05-02", "2025-05-02", "date_order")["result"] == "match"
    # Payment before approval: the left date falls after the right one.
    assert doc_tests.compare_values("2025-05-26", "2025-05-25", "date_order")["result"] == "mismatch"
    assert doc_tests.compare_values("not a date", "2025-05-25", "date_order")["result"] == "invalid"

    # An affirmative negative is a mismatch; an absent value is missing.
    assert doc_tests.compare_values(True, False, "present")["result"] == "mismatch"
    assert doc_tests.compare_values(True, True, "present")["result"] == "match"
    assert doc_tests.compare_values(True, None, "present")["result"] == "missing"


def test_legacy_literal_checks_are_untouched(procurement):
    """The original expected-value shape still normalizes and runs."""

    check = doc_tests._normalize_check(
        {"field": "amount", "expected": "50,000", "method": "normalized"}
    )
    assert check["expected"] == "50,000"
    assert check["left"] == "" and check["right"] == ""
