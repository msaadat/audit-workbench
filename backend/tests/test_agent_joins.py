import polars as pl
import pytest

from app.agent import joins


def test_diagnose_reports_match_metrics(workspace_with_data):
    left = workspace_with_data.get_frame("transactions")
    right = workspace_with_data.get_frame("customers")
    d = joins.diagnose(left, right, "cust_id", "id")
    assert d["match_rate"] == 1.0
    assert d["right_key_unique"] is True
    assert d["relationship"] == "many_to_one"
    assert d["expected_rows"] == left.height
    assert d["row_multiplication"] == 1.0


def test_find_candidates_links_fact_to_dimension(workspace_with_data):
    candidates = joins.find_candidates(workspace_with_data)
    assert candidates, "expected at least one candidate"
    best = candidates[0]
    assert best["strength"] == "strong"
    assert {best["left"], best["right"]} == {"transactions", "customers"}
    assert best["how"] == "left"


def test_row_multiplication_downgrades_to_weak():
    left = pl.DataFrame({"key": ["a", "a", "b"], "x": [1, 2, 3]})
    right = pl.DataFrame({"key": ["a", "a", "b"], "y": [1, 2, 3]})  # dup keys
    d = joins.diagnose(left, right, "key", "key")
    assert d["row_multiplication"] > 1
    assert joins.classify(d) == "weak"


def test_low_match_rate_is_not_strong():
    left = pl.DataFrame({"cust_id": ["C1", "C9", "C8", "C7"]})
    right = pl.DataFrame({"id": ["C1", "C2", "C3"], "n": ["a", "b", "c"]})
    d = joins.diagnose(left, right, "cust_id", "id")
    assert d["match_rate"] == 0.25
    assert joins.classify(d) == "weak"


def test_candidate_keys_recognize_link_suffixes():
    invoices = pl.DataFrame(
        {"GRN_ID_LINK": ["G1", "G2", "G3"], "INVOICE_ID": [1, 2, 3]}
    )
    purchase_orders = pl.DataFrame(
        {"GRN_ID": ["G1", "G2", "G3"], "PO_NUMBER": ["P1", "P2", "P3"]}
    )

    candidates = joins.candidate_keys(invoices, purchase_orders, "po_data")
    assert ("GRN_ID_LINK", "GRN_ID", 0.9) in candidates


ROLE_ENTITIES = joins.entity_tokens(
    ["requisitions", "staff_details", "vendor_master_file", "po_data"]
)


@pytest.mark.parametrize(
    "column",
    [
        "FIN_APPROVED_BY_ID",  # role qualifier plus an explicit _BY segment
        "APPROVED_BY_ID",
        "VERIFIED_BY_ID",
        "REQUESTER_ID",  # agent noun, no _BY segment
        "SUPERVISOR_APPROVAL_ID",
        "UPDATED_BY",  # no trailing _ID at all
        "BUYER_ID",
    ],
)
def test_role_qualified_keys_reach_the_person_dimension(column):
    assert (
        joins._name_affinity(column, "STAFF_ID", "staff_details", ROLE_ENTITIES) > 0
    )


@pytest.mark.parametrize(
    "column",
    [
        "VENDOR_ID",  # an entity key that merely ends like an agent noun
        "PO_NUMBER_LINK",
        "ITEM_LINE_NUMBER",  # "number" is a key word, not an agent noun
        "BANK_ACCOUNT_NUMBER",
        "VENDOR_STATUS",
    ],
)
def test_entity_and_value_columns_are_not_role_references(column):
    assert (
        joins._name_affinity(column, "STAFF_ID", "staff_details", ROLE_ENTITIES) == 0
    )


def test_role_scoring_stays_off_without_workspace_entities():
    """The entity list is the only thing separating a role from an entity key,
    so a caller that supplies none gets the original name-equality behaviour."""
    assert joins._name_affinity("APPROVED_BY_ID", "STAFF_ID", "staff_details") == 0


def test_role_keys_never_outrank_a_table_s_own_key():
    orders = pl.DataFrame(
        {
            "VENDOR_ID": ["V1", "V2", "V3"],
            "APPROVED_BY_ID": ["S1", "S2", "S3"],
        }
    )
    vendors = pl.DataFrame(
        {"VENDOR_ID": ["V1", "V2", "V3"], "NAME": ["a", "b", "c"]}
    )
    candidates = joins.candidate_keys(orders, vendors, "vendor_master_file", ROLE_ENTITIES)
    assert candidates[0][:2] == ("VENDOR_ID", "VENDOR_ID")


def test_fact_side_accepts_a_low_cardinality_key():
    """A foreign key into a small dimension repeats by design — 112 rows over
    four job titles is the shape of a many-to-one join, not a category column."""
    facts = pl.DataFrame({"JOB_TITLE": ["CFO", "CEO"] * 30})  # 2 distinct in 60 rows
    dimension = pl.DataFrame(
        {"JOB_TITLE": ["CFO", "CEO"], "MAX_APPROVAL_AMOUNT": [10, 20]}
    )
    assert not joins._is_plausible_key(facts, "JOB_TITLE")
    assert joins._is_plausible_key(facts, "JOB_TITLE", dimension=False)
    assert joins.candidate_keys(facts, dimension, "financial_approval_matrix")


def _candidate(left_on, *, role_key=False, null_keys=0):
    return {
        "left_on": [left_on],
        "strength": "strong",
        "role_key": role_key,
        "diagnostics": {
            "match_rate": 1.0,
            "row_multiplication": 1.0,
            "left_null_keys": null_keys,
        },
    }


def test_tied_role_keys_are_not_decisive():
    """Two roles reaching the same dimension with identical evidence are a
    question for the auditor; ranking them would answer it by accident."""
    tied = [
        _candidate("REQUESTER_ID", role_key=True),
        _candidate("FIN_APPROVED_BY_ID", role_key=True),
    ]
    assert not joins.decisive(tied)
    assert joins.decisive(tied[:1])


def test_tied_entity_keys_stay_decisive():
    """Two entity keys linking the same pair express one relationship, so
    picking the best-ranked route is not a choice of meaning."""
    tied = [_candidate("PO_NUMBER"), _candidate("REQUISITION_ID")]
    assert joins.decisive(tied)


def test_better_key_coverage_breaks_a_tie():
    candidates = [
        _candidate("APPROVED_BY_ID", role_key=True, null_keys=4),
        _candidate("FIN_APPROVED_BY_ID", role_key=True, null_keys=0),
    ]
    assert joins.decisive(candidates)
    best = sorted(candidates, key=joins.evidence_rank)[0]
    assert best["left_on"] == ["FIN_APPROVED_BY_ID"]


def test_chained_join_is_offered_once_the_first_hop_exists(workspace_with_data):
    """A dimension two hops out is unreachable until the first hop is real, so
    a materialized join has to become a fact side in its own right."""
    ws = workspace_with_data
    tiers = pl.DataFrame({"customer": ["Alpha", "Beta", "Gamma"], "tier": [1, 2, 3]})
    ws.add_table("tiers.csv", tiers.write_csv().encode())

    assert not any(
        c["left"] == "tx_customers" for c in joins.find_candidates(ws)
    ), "no chain exists before its first hop is materialized"

    ws.add_join(
        {
            "name": "tx_customers",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    chained = [c for c in joins.find_candidates(ws) if c["left"] == "tx_customers"]
    assert chained, "the materialized join should now be a candidate fact side"
    assert chained[0]["right"] == "tiers"
    assert chained[0]["left_on"] == ["customer"]


def test_a_chain_is_never_offered_a_table_it_already_contains(workspace_with_data):
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "tx_customers",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    assert joins.frame_lineage(ws, "tx_customers") == {"transactions", "customers"}
    assert not [
        c
        for c in joins.find_candidates(ws)
        if c["left"] == "tx_customers" and c["right"] in {"transactions", "customers"}
    ]


def test_a_chain_that_restates_existing_reachability_is_not_offered(workspace_with_data):
    """Both sides of the join already reach the added table directly, so the
    three-table frame proves nothing a pairwise frame does not."""
    ws = workspace_with_data
    regions = pl.DataFrame(
        {"cust_id": ["C1", "C2", "C3"], "region": ["N", "S", "E"]}
    )
    ws.add_table("regions.csv", regions.write_csv().encode())
    for name, left, left_on, right, right_on in (
        ("tx_customers", "transactions", "cust_id", "customers", "id"),
        ("tx_regions", "transactions", "cust_id", "regions", "cust_id"),
        ("cust_regions", "customers", "id", "regions", "cust_id"),
    ):
        ws.add_join(
            {
                "name": name,
                "left": left,
                "right": right,
                "how": "left",
                "left_on": [left_on],
                "right_on": [right_on],
            }
        )
    lineage = joins.frame_lineage(ws, "tx_customers")
    assert not joins.chain_extends_reach(ws, lineage, "regions")
    assert not [c for c in joins.find_candidates(ws) if c["left"] == "tx_customers"]


def test_column_origins_resolve_through_a_chain(workspace_with_data):
    ws = workspace_with_data
    ws.add_join(
        {
            "name": "tx_customers",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    origins = joins.column_origins(ws, "tx_customers")
    assert origins["cust_id"] == "transactions"
    assert origins["customer"] == "customers"
    base = joins.column_origins(ws, "transactions")
    assert set(base.values()) == {"transactions"}
    # The same column resolves to the same table from either frame — the
    # property the analysis identity relies on.
    assert origins["cust_id"] == base["cust_id"]


def test_existing_join_not_reproposed(workspace_with_data):
    workspace_with_data.add_join(
        {
            "name": "tx_customers",
            "left": "transactions",
            "right": "customers",
            "how": "left",
            "left_on": ["cust_id"],
            "right_on": ["id"],
        }
    )
    candidates = joins.find_candidates(workspace_with_data)
    assert all(
        (c["left"], c["right"], c["left_on"], c["right_on"])
        != ("transactions", "customers", ["cust_id"], ["id"])
        for c in candidates
    )
