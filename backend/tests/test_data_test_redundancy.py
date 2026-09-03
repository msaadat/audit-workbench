"""Post-run detection of Data Tests that flag the same records."""

from fastapi.testclient import TestClient

from app import data_test_redundancy, data_tests
from app.main import create_app


def _rcm_row(ws, *, risk="Transactions may bypass controls"):
    return ws.add_rcm(
        {
            "process": "Procurement",
            "risk": risk,
            "risk_rating": "high",
            "control": "Automated validation",
        }
    )


def _polars_test(ws, row, *, title, code, table_refs=("transactions",)):
    step = {
        "label": title,
        "instruction": title,
        "table_refs": list(table_refs),
        "code": code,
    }
    return data_tests.create(
        ws,
        {
            "title": title,
            "objective": title,
            "rcm_id": row["id"],
            "engine": "polars",
            "table_refs": list(table_refs),
            "steps": [step],
            "spec": {"schema_version": 2, "steps": [step]},
        },
    )


# Written differently, selecting the same two rows (customer C1's invoices
# 1001 and 1003) — the shape the detector exists for, and the shape a code
# comparison misses. They share two identifier columns of different grain, so
# they also exercise the choice between them.
_BY_CUSTOMER = 'result = tables["transactions"].filter(pl.col("cust_id") == "C1")'
_BY_INVOICE = (
    'result = tables["transactions"].filter(pl.col("invoice_no").is_in([1001, 1003]))'
)
_BY_AMOUNT = (
    'result = tables["transactions"].filter('
    '(pl.col("amount") < 200) & (pl.col("cust_id") == "C1"))'
)
_WIDER = (
    'result = tables["transactions"].filter('
    'pl.col("invoice_no").is_in([1001, 1003, 1005]))'
)
_UNRELATED = 'result = tables["transactions"].filter(pl.col("cust_id") == "C3")'


def test_two_tests_selecting_the_same_rows_are_marked_as_one_duplicate_group(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _rcm_row(ws)
    left = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    right = _polars_test(ws, row, title="Invoices 1001 and 1003", code=_BY_INVOICE)

    data_tests.run_all(ws)

    assert left["redundancy"]["state"] == data_test_redundancy.DUPLICATE
    assert right["redundancy"]["state"] == data_test_redundancy.DUPLICATE
    assert left["redundancy"]["group_id"] == right["redundancy"]["group_id"]

    peer = left["redundancy"]["peers"][0]
    assert peer["test_id"] == right["id"]
    assert peer["relation"] == data_test_redundancy.IDENTICAL
    # Both tests also agree on cust_id, but that is one value covering both
    # rows. The finer key is the more specific claim, so it is the one reported.
    assert peer["key"] == "invoice_no"
    assert peer["confidence"] == "confirmed"
    assert peer["jaccard"] == 1.0


def test_a_wider_test_is_marked_as_subsuming_the_narrower_one(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    wide = _polars_test(ws, row, title="Three invoices", code=_WIDER)
    narrow = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)

    data_tests.run_all(ws)

    # Containment is not duplication: the wider test still says more, so
    # neither is grouped and neither is proposed for removal.
    assert wide["redundancy"]["state"] == data_test_redundancy.SUBSUMING
    assert narrow["redundancy"]["state"] == data_test_redundancy.SUBSUMED
    assert wide["redundancy"]["group_id"] == ""
    assert narrow["redundancy"]["peers"][0]["relation"] == data_test_redundancy.SUBSUMED_BY


def test_duplicates_are_found_across_different_rcm_rows(workspace_with_data):
    """The case both creation-path guards are structurally unable to catch."""
    ws = workspace_with_data
    left = _polars_test(ws, _rcm_row(ws, risk="Risk A"), title="A", code=_BY_CUSTOMER)
    right = _polars_test(ws, _rcm_row(ws, risk="Risk B"), title="B", code=_BY_INVOICE)

    data_tests.run_all(ws)

    assert left["rcm_id"] != right["rcm_id"]
    assert left["redundancy"]["group_id"] == right["redundancy"]["group_id"]
    assert left["redundancy"]["group_id"]


def test_unrelated_tests_are_left_clear(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    left = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    right = _polars_test(ws, row, title="Customer C3", code=_UNRELATED)

    data_tests.run_all(ws)

    assert left["redundancy"]["state"] == data_test_redundancy.CLEAR
    assert right["redundancy"]["state"] == data_test_redundancy.CLEAR
    assert left["redundancy"]["peers"] == []


def test_a_test_with_nothing_to_compare_is_not_comparable(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    passing = _polars_test(
        ws,
        row,
        title="Nothing over a million",
        code='result = tables["transactions"].filter(pl.col("amount") > 1_000_000)',
    )

    data_tests.run_all(ws)

    # A passing test flagged no records, so its result cannot be evidence that
    # some other test flags the same ones. Saying so beats implying it is clear.
    assert passing["redundancy"]["state"] == data_test_redundancy.NOT_COMPARABLE
    assert passing["redundancy"]["peers"] == []


def test_scan_does_not_write_and_annotate_does(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    _polars_test(ws, row, title="Invoices 1001 and 1003", code=_BY_INVOICE)
    for item in ws.data_tests:
        data_tests.run(ws, item["id"])
    for item in ws.data_tests:
        item.pop("redundancy", None)
    ws.save()

    revision = ws.revision
    outcome = data_test_redundancy.scan(ws)

    assert outcome["groups"]
    assert ws.revision == revision
    assert all("redundancy" not in item for item in ws.data_tests)

    stored = data_test_redundancy.annotate(ws, persist=True)

    assert stored["persisted"] is True
    assert ws.revision > revision
    assert all(item["redundancy"]["state"] for item in ws.data_tests)


def test_rescanning_an_unchanged_workspace_does_not_advance_the_revision(
    workspace_with_data,
):
    ws = workspace_with_data
    row = _rcm_row(ws)
    _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    _polars_test(ws, row, title="Invoices 1001 and 1003", code=_BY_INVOICE)
    data_tests.run_all(ws)

    revision = ws.revision
    again = data_test_redundancy.annotate(ws, persist=True)

    assert again["persisted"] is False
    assert ws.revision == revision


def test_a_detector_failure_never_fails_the_batch(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    row = _rcm_row(ws)
    item = _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)

    def boom(*_args, **_kwargs):
        raise RuntimeError("detector is broken")

    monkeypatch.setattr(data_test_redundancy, "annotate", boom)
    outcome = data_tests.run_all(ws)

    assert outcome["failed"] == []
    assert item["last_run"]["id"] == data_tests.CURRENT_RESULT_ID


def test_grouping_is_transitive_across_more_than_two_tests(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    made = [
        _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER),
        _polars_test(ws, row, title="Invoices 1001 and 1003", code=_BY_INVOICE),
        _polars_test(ws, row, title="C1 under 200", code=_BY_AMOUNT),
    ]

    outcome = data_tests.run_all(ws)
    assert outcome["failed"] == []

    groups = data_test_redundancy.scan(ws)["groups"]
    assert len(groups) == 1
    assert groups[0]["members"] == sorted(item["id"] for item in made)


def test_redundancy_endpoints_read_and_store(workspace_with_data):
    ws = workspace_with_data
    row = _rcm_row(ws)
    _polars_test(ws, row, title="Customer C1", code=_BY_CUSTOMER)
    _polars_test(ws, row, title="Invoices 1001 and 1003", code=_BY_INVOICE)
    for item in list(ws.data_tests):
        data_tests.run(ws, item["id"])

    client = TestClient(create_app())
    base = f"/api/workspaces/{ws.id}"

    read = client.get(f"{base}/data-tests/redundancy")
    assert read.status_code == 200
    assert read.json()["groups"]

    stored = client.post(f"{base}/data-tests/redundancy")
    assert stored.status_code == 200
    assert stored.json()["persisted"] is True

    listed = client.get(f"{base}/data-tests").json()["items"]
    assert {item["redundancy"]["state"] for item in listed} == {
        data_test_redundancy.DUPLICATE
    }


def test_sharing_only_a_grouping_column_does_not_make_a_duplicate_group(
    workspace_with_data,
):
    """Both tests flag only customer C1's rows, but different ones of them.

    ``cust_id`` is one value across each frame, so it identifies a group rather
    than a record. Treating that agreement as duplication once fused nine
    unrelated tests into a single bogus group, because one weak edge is enough
    to merge two real ones transitively.
    """
    ws = workspace_with_data
    row = _rcm_row(ws)
    left = _polars_test(
        ws,
        row,
        title="C1 invoice 1001",
        code='result = tables["transactions"].filter(pl.col("invoice_no") == 1001)',
    )
    right = _polars_test(
        ws,
        row,
        title="C1 invoice 1003",
        code='result = tables["transactions"].filter(pl.col("invoice_no") == 1003)',
    )

    data_tests.run_all(ws)

    assert left["redundancy"]["group_id"] == ""
    assert right["redundancy"]["group_id"] == ""
    assert left["redundancy"]["state"] != data_test_redundancy.DUPLICATE


def _signature(test_id, entity_key, identifiers, row_count=1):
    return data_test_redundancy.Signature(
        test_id=test_id,
        entity_key=entity_key,
        row_count=row_count,
        identifiers={name: frozenset(values) for name, values in identifiers.items()},
        result_sha1="sha",
    )


def test_a_single_shared_record_is_confirmed_only_on_a_mutual_entity_key():
    """Every column is one-per-row in a one-row frame.

    Two tests flagging one row each would otherwise read as identical the moment
    they share any identifier a join carried in — in one engagement a settlement
    test and a dealer-master test were fused because both rows named dealer
    TS-010. So a single shared record counts as confirmed only when both tests
    resolved that column as the record they are about.
    """
    dealer_row = _signature("DAT-A", "DEALER_ID", {"DEALER_ID": {"D1"}})
    other_dealer_row = _signature("DAT-B", "DEALER_ID", {"DEALER_ID": {"D1"}})

    mutual = data_test_redundancy.relate(dealer_row, other_dealer_row)
    assert mutual["relation"] == data_test_redundancy.IDENTICAL
    assert mutual["confidence"] == "confirmed"

    # The settlement test is about a settlement; its dealer came in on a join.
    settlement = _signature(
        "DAT-C", "SETTLEMENT_ID", {"SETTLEMENT_ID": {"S1"}, "DEALER_ID": {"D1"}}
    )
    incidental = data_test_redundancy.relate(settlement, dealer_row)

    assert incidental["key"] == "DEALER_ID"
    assert incidental["shared"] == 1
    assert incidental["confidence"] == "possible"


def test_more_than_one_shared_record_needs_no_mutual_entity_key():
    """Two tests agreeing on which deals they flagged is evidence in itself,
    whatever population each declared — the duplicates that matter most
    disagree about that."""
    left = _signature(
        "DAT-A", "SETTLEMENT_ID", {"DEAL_ID": {"D1", "D2"}}, row_count=2
    )
    right = _signature(
        "DAT-B", "CONFIRMATION_ID", {"DEAL_ID": {"D1", "D2"}}, row_count=2
    )

    found = data_test_redundancy.relate(left, right)

    assert found["key"] == "DEAL_ID"
    assert found["confidence"] == "confirmed"
