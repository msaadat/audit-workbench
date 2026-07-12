import polars as pl

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
