"""A treasury cycle, end to end, on rules nobody shipped.

This suite exists because of how the old design failed. The packs covered
procure-to-pay and payroll, and a treasury engagement degraded *silently*: the
documents were read, no pack claimed them, no cycle test could be generated, and
the run reported success. Nothing in the old suite would have caught that,
because there was no treasury pack to write a test against.

There is still no treasury pack. There is a treasury *ruleset*, induced from the
documents this engagement holds and approved by its auditor — which is the whole
point, so this is kept permanently rather than as a regression note.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import (
    cycle_linking,
    cycle_measurement,
    cycle_rulesets,
    cycle_vouching,
    document_schemas,
    workspaces,
)

from tests.test_cycle_linking import extract  # noqa: F401 - the reduction path

PAYMENT_FIELDS = [
    {"name": "payment_reference", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "counterparty", "role": "party", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "value_date", "role": "attribute", "value_type": "date",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]

MANDATE_FIELDS = [
    {"name": "payment_reference", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "counterparty", "role": "party", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "approved_on", "role": "attribute", "value_type": "date",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
    {"name": "approver", "role": "control", "value_type": "text",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


def treasury_rules(**overrides) -> dict:
    payload = {
        "cycle_label": "Treasury payments",
        "roles": [
            {"name": "instruction", "document_type": "payment_instruction"},
            {"name": "mandate", "document_type": "board_minutes"},
        ],
        "anchor": {
            "table": "payments", "column": "PAYMENT_REF",
            "role": "instruction", "field": "payment_reference",
        },
        "join_keys": [{
            "id": "jk_reference",
            "left": {"role": "instruction", "field": "payment_reference"},
            "right": {"role": "mandate", "field": "payment_reference"},
            "match": "normalized_equal",
            "rationale": "The mandate authorises this payment by its reference.",
        }],
        "assertions": [
            {
                "id": "as_amount", "label": "Amount is the amount authorised",
                "left": {"role": "instruction", "field": "amount"},
                "right": {"role": "mandate", "field": "amount"},
                "operator": "numeric_within", "tolerance": {"absolute": 0},
                "rationale": "A payment may not exceed what was authorised.",
            },
            {
                "id": "as_sequence", "label": "Authorised before paid",
                "left": {"role": "mandate", "field": "approved_on"},
                "right": {"role": "instruction", "field": "value_date"},
                "operator": "date_on_or_before",
                "rationale": "Authority cannot be granted after the money moved.",
            },
            {
                "id": "as_approver", "label": "An approver is named",
                "left": {"role": "mandate", "field": "approver"},
                "operator": "present",
                "rationale": "An unsigned mandate authorises nobody.",
            },
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def treasury():
    ws = workspaces.create_workspace("Treasury")
    document_schemas.save_schema(ws, "payment_instruction", PAYMENT_FIELDS)
    document_schemas.save_schema(ws, "board_minutes", MANDATE_FIELDS)
    ws.add_table(
        "payments.csv",
        pl.DataFrame({
            "PAYMENT_REF": ["TT-9001", "TT-9002", "TT-9003"],
            "AMOUNT": [500000.0, 250000.0, 90000.0],
        }).write_csv().encode(),
    )
    row = ws.add_rcm({
        "process": "Treasury",
        "risk": "Funds may be transferred without board authority.",
        "risk_rating": "high",
        "control": "Every transfer is authorised by a board resolution before value date.",
    })

    # Authorised, in order, for the right amount.
    extract(ws, "tt9001.txt", "payment_instruction", payment_reference="TT-9001",
            counterparty="Meridian Bank", amount="500000", value_date="2024-06-14")
    extract(ws, "res9001.txt", "board_minutes", payment_reference="TT-9001",
            counterparty="Meridian Bank", amount="500000",
            approved_on="2024-06-10", approver="A. Nakamura")
    # Paid before it was authorised.
    extract(ws, "tt9002.txt", "payment_instruction", payment_reference="TT-9002",
            counterparty="Halcyon Ltd", amount="250000", value_date="2024-06-03")
    extract(ws, "res9002.txt", "board_minutes", payment_reference="TT-9002",
            counterparty="Halcyon Ltd", amount="250000",
            approved_on="2024-06-19", approver="A. Nakamura")
    # No mandate at all.
    extract(ws, "tt9003.txt", "payment_instruction", payment_reference="TT-9003",
            counterparty="Orbit SA", amount="90000", value_date="2024-06-21")
    return ws, row


def approve(ws, **overrides) -> dict:
    record = cycle_rulesets.save(ws, treasury_rules(**overrides), proposed_by="agent")
    return cycle_rulesets.approve(ws, record["ruleset_id"], approved_by="treasurer@example.com")


def build(ws, row) -> dict:
    return cycle_vouching.build_cycle_vouch_test(ws, {
        "title": "Vouch transfers to board authority",
        "objective": "Vouch each transfer to the resolution authorising it.",
        "rcm_id": str(row["id"]),
        "procedure_key": "transfer_to_mandate",
        "requirement_refs": [f"{row['id']}:authorised"],
        "definition": {"population": {"selection": {"mode": "evidence_linked"}}},
    })


def test_a_cycle_nobody_shipped_a_pack_for_runs_end_to_end(treasury):
    ws, row = treasury
    approve(ws)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, build(ws, row))
    verdicts = {
        item["label"]: {
            key: result["verdict"]
            for key, result in item["result_by_assertion"].items()
        }
        for item in evaluated["items"]
    }

    assert verdicts["TT-9001"] == {
        "as_amount": "match", "as_sequence": "match", "as_approver": "match"
    }
    # Authorised nineteen days after the money moved.
    assert verdicts["TT-9002"]["as_sequence"] == "mismatch"
    assert verdicts["TT-9002"]["as_amount"] == "match"
    # No mandate bound at all, which is a gap rather than a disagreement.
    assert verdicts["TT-9003"] == {
        "as_amount": "missing_evidence",
        "as_sequence": "missing_evidence",
        "as_approver": "missing_evidence",
    }


def test_the_treasury_gap_is_named_rather_than_passing_quietly(treasury):
    """The old design's actual failure: the run reported success over it."""

    ws, row = treasury
    approve(ws)

    evaluated = cycle_vouching.evaluate_cycle_test(ws, build(ws, row))
    unmandated = next(
        item for item in evaluated["items"] if item["label"] == "TT-9003"
    )

    assert unmandated["missing_roles"] == ["mandate"]
    assert unmandated["evaluation"]["state"] == "incomplete"
    assert evaluated["status"] == "review_required"


def test_the_rules_are_measured_against_the_treasury_corpus(treasury):
    ws, _row = treasury
    ruleset = approve(ws)

    measured = cycle_measurement.measure(ws, ruleset)

    reference = measured["join_keys"]["jk_reference"]
    assert reference["fan_out_p95"] == 1
    assert reference["left_stating_key"] == 3
    # One instruction states the reference and reaches no mandate.
    assert reference["left_unmatched"] == 1
    assert cycle_measurement.concerns(measured) == []


def test_joining_treasury_on_the_counterparty_is_visible_as_runaway_fan_out():
    """The mistake this design exists to make visible, in a cycle with no pack
    to have forbidden it.

    A counterparty is a perfectly good identifier and a catastrophic join key:
    every transfer to one bank would be authorised by every mandate naming it.
    Nothing in the rule text says so. The fan-out does.
    """

    ws = workspaces.create_workspace("Treasury fan-out")
    identifier = {
        "role": "identifier", "value_type": "identifier",
        "cardinality": "one", "verbatim": True, "confidence": "high",
    }
    fields = [
        {"name": "payment_reference", **identifier},
        {"name": "counterparty", **identifier},
    ]
    document_schemas.save_schema(ws, "payment_instruction", fields)
    document_schemas.save_schema(ws, "board_minutes", fields)
    for index in range(6):
        extract(ws, f"tt{index}.txt", "payment_instruction",
                payment_reference=f"TT-{index}", counterparty="Meridian Bank")
        extract(ws, f"res{index}.txt", "board_minutes",
                payment_reference=f"TT-{index}", counterparty="Meridian Bank")

    entity = {
        "roles": [
            {"name": "instruction", "document_type": "payment_instruction"},
            {"name": "mandate", "document_type": "board_minutes"},
        ],
        "join_keys": [{
            "id": "jk_counterparty",
            "left": {"role": "instruction", "field": "counterparty"},
            "right": {"role": "mandate", "field": "counterparty"},
            "match": "normalized_equal",
        }],
        "assertions": [],
    }
    reference = {
        **entity,
        "join_keys": [{
            "id": "jk_reference",
            "left": {"role": "instruction", "field": "payment_reference"},
            "right": {"role": "mandate", "field": "payment_reference"},
            "match": "normalized_equal",
        }],
    }

    fused = cycle_measurement.measure(ws, entity)
    sound = cycle_measurement.measure(ws, reference)

    assert fused["join_keys"]["jk_counterparty"]["fan_out_p95"] == 6
    assert [item["concern"] for item in cycle_measurement.concerns(fused)] == [
        "entity_fan_out"
    ]
    assert sound["join_keys"]["jk_reference"]["fan_out_p95"] == 1
    assert cycle_measurement.concerns(sound) == []


def test_a_treasury_type_needs_no_code_change_to_take_part(treasury):
    ws, _row = treasury

    catalog = {
        entry["document_type"] for entry in cycle_linking.schema_catalog(ws)
    }

    assert catalog == {"payment_instruction", "board_minutes"}
