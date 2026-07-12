from app.agent import suggest


def _by_check(suggestions, check):
    return [s for s in suggestions if s["check"] == check]


def test_suggests_required_for_complete_columns(workspace_with_data):
    suggestions = suggest.suggest_rules(workspace_with_data, "transactions")
    required = _by_check(suggestions, "required")
    assert {s["column"] for s in required} >= {"invoice_no", "amount", "tx_date"}
    assert all(s["rationale"] for s in suggestions)


def test_suggests_sign_for_positive_numeric(workspace_with_data):
    suggestions = suggest.suggest_rules(workspace_with_data, "transactions")
    signs = _by_check(suggestions, "numeric_sign")
    amount = next(s for s in signs if s["column"] == "amount")
    assert amount["params"]["mode"] == "positive"


def test_suggests_not_in_future_for_dates(workspace_with_data):
    suggestions = suggest.suggest_rules(workspace_with_data, "transactions")
    dates = _by_check(suggestions, "date_range")
    assert any(
        s["column"] == "tx_date" and s["params"].get("not_in_future") for s in dates
    )


def test_suggests_unique_for_key_like_columns(workspace_with_data):
    suggestions = suggest.suggest_rules(workspace_with_data, "customers")
    uniques = _by_check(suggestions, "unique")
    assert {s["column"] for s in uniques} >= {"id"}


def test_no_unique_for_duplicated_column(workspace_with_data):
    # invoice_no 1006 appears twice, so unique must NOT be suggested for it.
    suggestions = suggest.suggest_rules(workspace_with_data, "transactions")
    uniques = _by_check(suggestions, "unique")
    assert all(s["column"] != "invoice_no" for s in uniques)


def test_suggestions_pass_on_current_data(workspace_with_data):
    """Conservative contract: every suggestion passes against today's frame."""
    from app import validation

    for table in ("transactions", "customers"):
        rules = [
            {**s, "id": f"s{i}", "enabled": True}
            for i, s in enumerate(suggest.suggest_rules(workspace_with_data, table))
        ]
        run = validation.run_rules(
            workspace_with_data.get_frame(table),
            rules,
            table,
            resolve=workspace_with_data.get_frame,
        )
        assert all(r["verdict"] == "ok" for r in run["results"]), run["results"]
