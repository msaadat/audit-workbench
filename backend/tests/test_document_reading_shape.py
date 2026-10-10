"""What a record is, when a document carries a header and several lines.

Measured on nineteen identically laid-out nostro statements, all read by the
same worker: twelve came back as one record per line, five folded the three
lines into one record as parallel lists, and two made the header a record of its
own. The contract offered one bucket for two kinds of fact — what the statement
says once (account, currency, opening balance) and what each line says — so
each reading invented somewhere to put the first kind.

And six of their fifty-three amounts were read onto the wrong side, every one
from text in which the column could not be seen: the plain extraction puts each
cell on its own line and drops the empty ones.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import document_schemas, documents, workspaces
from app.agent.capabilities import documents as document_capabilities
from app.agent.workers.model import WorkerResponseValidationError
from app.agent.workers.documents import (
    _read_response_schema,
    _read_submission_tool,
    validate_read_proposal,
)

STATEMENT_FIELDS = [
    {"name": "account_name", "role": "identifier", "value_type": "identifier",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Account", "scope": "document"},
    {"name": "transaction_date", "role": "attribute", "value_type": "date",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Date"},
    {"name": "debit", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Debit"},
    {"name": "credit", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Credit"},
    {"name": "balance", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high",
     "label": "Balance"},
]

CITATIONS = [
    {"id": "h", "page": 1, "excerpt": "NOSTRO-USD-CITI-3610044821"},
    {"id": "l1", "page": 1, "excerpt": "2025-02-05  279,000.00  95,959,000.00"},
    {"id": "l2", "page": 1, "excerpt": "2025-02-06  900,000.00  95,059,000.00"},
    {"id": "l3", "page": 1, "excerpt": "2025-02-07  108,000.00  95,167,000.00"},
]


def _stated(name: str, value: str, citation: str, entry: int = 1) -> dict:
    return {"name": name, "entry": entry, "value": value, "citation": citation}


def _line(date: str, side: str, amount: str, balance: str, citation: str) -> dict:
    return {
        "fields": [
            _stated("transaction_date", date, citation),
            _stated(side, amount, citation),
            _stated("balance", balance, citation),
        ]
    }


def _validate(payload: dict, master_fields=STATEMENT_FIELDS):
    request = SimpleNamespace(
        unit_input={"document_type": "bank_statement", "master_fields": master_fields}
    )
    return validate_read_proposal(_read_response_schema(json.dumps(payload)), request)


def _reading(*, records, document_fields=(), new_fields=()) -> dict:
    return {
        "records": list(records),
        "document_fields": list(document_fields),
        "new_fields": list(new_fields),
        "renames": [],
        "audit_notes": [],
        "citations": CITATIONS,
    }


# --------------------------------------------------------------------------- #
# the shape a statement is read in
# --------------------------------------------------------------------------- #
def test_a_statement_is_its_header_once_and_one_record_per_line():
    reading = _reading(
        document_fields=[_stated("account_name", "NOSTRO-USD-CITI-3610044821", "h")],
        records=[
            _line("2025-02-05", "credit", "279,000.00", "95,959,000.00", "l1"),
            _line("2025-02-06", "debit", "900,000.00", "95,059,000.00", "l2"),
            _line("2025-02-07", "credit", "108,000.00", "95,167,000.00", "l3"),
        ],
    )
    proposal = _validate(reading)
    assert [field["name"] for field in proposal["document_fields"]] == ["account_name"]
    assert len(proposal["records"]) == 3


def test_lines_folded_into_one_record_are_refused():
    """The treasury reading, exactly: three dates and balances, two debits, one
    credit. ``entry`` counts within a field, so the blank Debit cell on line 1
    made 900,000.00 debit *entry 2* by accident and 108,000.00 credit entry 1 —
    nothing in the record can say which amount was on which line."""

    folded = {
        "fields": [
            _stated("transaction_date", "2025-02-05", "l1", 1),
            _stated("transaction_date", "2025-02-06", "l1", 2),
            _stated("transaction_date", "2025-02-07", "l1", 3),
            _stated("debit", "279,000.00", "l1", 1),
            _stated("debit", "900,000.00", "l1", 2),
            _stated("credit", "108,000.00", "l1", 1),
            _stated("balance", "95,959,000.00", "l1", 1),
            _stated("balance", "95,059,000.00", "l1", 2),
            _stated("balance", "95,167,000.00", "l1", 3),
        ]
    }
    with pytest.raises(WorkerResponseValidationError, match="its own record"):
        _validate(
            _reading(
                document_fields=[
                    _stated("account_name", "NOSTRO-USD-CITI-3610044821", "h")
                ],
                records=[folded],
            )
        )


def test_the_same_fold_is_refused_on_a_first_of_type_read():
    """A first read reports everything through ``new_fields``, so the fold has to
    be caught there too — that is where all five folded statements began."""

    def declared(name, value_type, values, scope="record"):
        return {
            "name": name, "role": "attribute", "value_type": value_type,
            "cardinality": "many", "scope": scope, "verbatim": True,
            "confidence": "high", "label": name, "reason": "Stated.",
            "values": values,
        }

    reading = _reading(
        records=[{"fields": []}],
        new_fields=[
            declared("transaction_date", "date", [
                {"record": 1, "entry": entry, "value": value, "citation": "l1"}
                for entry, value in enumerate(["2025-02-05", "2025-02-06", "2025-02-07"], 1)
            ]),
            declared("debit", "number", [
                {"record": 1, "entry": entry, "value": value, "citation": "l1"}
                for entry, value in enumerate(["279,000.00", "900,000.00"], 1)
            ]),
        ],
    )
    with pytest.raises(WorkerResponseValidationError, match="cannot be paired"):
        _validate(reading, master_fields=[])


def test_a_header_made_into_its_own_record_is_refused():
    """Two of nineteen statements did this, and a population then counted the
    header as a transaction with no date and no amount."""

    reading = _reading(
        document_fields=[_stated("account_name", "NOSTRO-USD-CITI-3610044821", "h")],
        records=[
            {"fields": []},
            _line("2025-02-05", "credit", "279,000.00", "95,959,000.00", "l1"),
            _line("2025-02-06", "debit", "900,000.00", "95,059,000.00", "l2"),
        ],
    )
    with pytest.raises(WorkerResponseValidationError, match="states nothing of its own"):
        _validate(reading)


def test_a_document_field_cannot_be_stated_on_one_line():
    """The account on line 1 only — the third wrong shape — leaves lines 2 and 3
    without it. The enum withholds the name from records; this is the same
    rule for a provider that does not enforce the enum."""

    line = _line("2025-02-05", "credit", "279,000.00", "95,959,000.00", "l1")
    line["fields"].append(_stated("account_name", "NOSTRO-USD-CITI-3610044821", "h"))
    with pytest.raises(WorkerResponseValidationError, match="under document_fields"):
        _validate(_reading(records=[line]))


def test_one_fact_cannot_be_both_the_documents_and_a_lines():
    line = _line("2025-02-05", "credit", "279,000.00", "95,959,000.00", "l1")
    with pytest.raises(WorkerResponseValidationError, match="both under document_fields"):
        _validate(
            _reading(
                document_fields=[_stated("balance", "95,959,000.00", "l1")],
                records=[line],
            )
        )


def test_document_fields_need_a_record_to_belong_to():
    with pytest.raises(WorkerResponseValidationError, match="no record"):
        _validate(
            _reading(
                document_fields=[_stated("account_name", "NOSTRO-USD-CITI-3610044821", "h")],
                records=[],
            )
        )


def test_a_single_transaction_may_put_every_fact_on_the_document():
    """Folding makes this the same stored record as putting them on the record,
    so it is not worth a repair turn to insist on one or the other."""

    proposal = _validate(
        _reading(
            document_fields=[
                _stated("account_name", "NOSTRO-USD-CITI-3610044821", "h"),
                _stated("transaction_date", "2025-02-06", "l2"),
                _stated("debit", "900,000.00", "l2"),
            ],
            records=[{"fields": []}],
        )
    )
    assert len(proposal["records"]) == 1


def test_a_swap_states_two_dated_legs_on_one_record():
    """A repeated group inside one transaction is not a folded table: the two
    legs of an FX swap state a value date and an amount each, and pair by
    entry because both fields carry both entries."""

    swap_fields = [
        {"name": "value_date", "role": "attribute", "value_type": "date",
         "cardinality": "many", "verbatim": True, "confidence": "high", "label": "Value date"},
        {"name": "amount", "role": "attribute", "value_type": "number",
         "cardinality": "many", "verbatim": True, "confidence": "high", "label": "Amount"},
    ]
    record = {
        "fields": [
            _stated("value_date", "2025-02-06", "l1", 1),
            _stated("value_date", "2025-05-06", "l2", 2),
            _stated("amount", "900,000.00", "l1", 1),
            _stated("amount", "900,000.00", "l2", 2),
        ]
    }
    proposal = _validate(_reading(records=[record]), master_fields=swap_fields)
    assert len(proposal["records"]) == 1


def test_a_document_scope_value_names_no_record():
    reading = _reading(
        records=[{"fields": []}, {"fields": []}],
        new_fields=[
            {
                "name": "account_name", "role": "identifier",
                "value_type": "identifier", "cardinality": "one",
                "scope": "document", "verbatim": True, "confidence": "high",
                "label": "Account", "reason": "The statement's account.",
                "values": [{"entry": 1, "value": "NOSTRO-USD-CITI-3610044821",
                            "citation": "h"}],
            },
            {
                "name": "transaction_date", "role": "attribute",
                "value_type": "date", "cardinality": "one", "scope": "record",
                "verbatim": True, "confidence": "high", "label": "Date",
                "reason": "Each line's date.",
                "values": [
                    {"record": 1, "entry": 1, "value": "2025-02-05", "citation": "l1"},
                    {"record": 2, "entry": 1, "value": "2025-02-06", "citation": "l2"},
                ],
            },
        ],
    )
    proposal = _validate(reading, master_fields=[])
    account = proposal["new_fields"][0]
    assert account["scope"] == "document"
    assert account["values"][0]["record"] is None


# --------------------------------------------------------------------------- #
# what the model is offered
# --------------------------------------------------------------------------- #
def test_a_record_is_never_offered_a_document_field():
    tool = _read_submission_tool(
        ["transaction_date", "debit"], ["account_name", "transaction_date", "debit"]
    )
    properties = tool["function"]["parameters"]["properties"]
    record_names = properties["records"]["items"]["properties"]["fields"]["items"][
        "properties"
    ]["name"]["enum"]
    document_names = properties["document_fields"]["items"]["properties"]["name"]["enum"]
    assert "account_name" not in record_names
    assert document_names == ["account_name", "transaction_date", "debit"]
    declared = properties["new_fields"]["items"]
    assert declared["properties"]["scope"]["enum"] == ["record", "document"]
    assert "scope" in declared["required"]
    assert "record" not in declared["properties"]["values"]["items"]["required"]


def test_a_first_read_is_offered_neither_array():
    properties = _read_submission_tool([])["function"]["parameters"]["properties"]
    assert properties["document_fields"]["maxItems"] == 0
    assert properties["records"]["items"]["properties"]["fields"]["maxItems"] == 0


# --------------------------------------------------------------------------- #
# scope in the stored vocabulary
# --------------------------------------------------------------------------- #
def test_only_document_scope_is_written_so_old_hashes_hold():
    """Every field stamped before scope existed is a record's. Writing
    ``scope: record`` onto them would move every schema hash, and with it make
    every stamped reading stale, for a distinction they already observed."""

    record_field = dict(STATEMENT_FIELDS[1])
    assert "scope" not in document_schemas.validate_fields([record_field])[0]
    assert "scope" not in document_schemas.validate_fields(
        [{**record_field, "scope": "record"}]
    )[0]
    stored = document_schemas.validate_fields([STATEMENT_FIELDS[0]])[0]
    assert stored["scope"] == "document"
    with pytest.raises(workspaces.WorkspaceError, match="scope"):
        document_schemas.validate_fields([{**record_field, "scope": "page"}])


# --------------------------------------------------------------------------- #
# the layout view
# --------------------------------------------------------------------------- #
def _pdf(lines: list[tuple[float, float, str]]) -> bytes:
    """A one-page PDF placing each string at an exact position."""

    content = "BT /F1 9 Tf\n" + "".join(
        f"1 0 0 1 {x} {y} Tm ({text}) Tj\n" for x, y, text in lines
    ) + "ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    body = "%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n{obj}\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    body += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    )
    return body.encode("latin-1")


STATEMENT_PDF = [
    (60, 740, "MERIDIAN BANK LIMITED - Account statement extract"),
    (60, 700, "Date"), (130, 700, "Narrative"), (340, 700, "Debit"),
    (420, 700, "Credit"), (500, 700, "Balance"),
    (60, 680, "2025-02-05"), (130, 680, "Interbank clearing - net"),
    (420, 680, "279,000.00"), (500, 680, "95,959,000.00"),
    (60, 665, "2025-02-06"), (130, 665, "PMT-2025-00133 Northgate Bank"),
    (340, 665, "900,000.00"), (500, 665, "95,059,000.00"),
]


def _column(layout: str, value: str, heading: str) -> bool:
    """Whether ``value`` sits nearer ``heading`` than any other amount column.

    Layout spacing is computed from glyph widths, so a cell and its heading
    drift a few characters apart; which heading is nearest is what survives,
    and it is all a reader needs."""

    lines = layout.splitlines()
    header = next(line for line in lines if "Debit" in line and "Balance" in line)
    row = next(line for line in lines if value in line)
    position = row.index(value)
    nearest = min(
        ("Debit", "Credit", "Balance"),
        key=lambda name: abs(header.index(name) - position),
    )
    return nearest == heading


def test_a_statement_pdf_keeps_its_columns_for_the_reader():
    ws = workspaces.create_workspace("Layout view")
    document = documents.add_document(
        ws, "statement.pdf", _pdf(STATEMENT_PDF), category="evidence"
    )
    extracted = documents.extract_document(ws, document["id"])
    page = extracted["pages"][0]
    assert extracted["layout_version"] == documents.LAYOUT_VERSION
    # The amount sits under the heading it was printed under, which is the one
    # thing that says whether it is a debit or a credit.
    assert _column(page["layout_text"], "279,000.00", "Credit")
    assert _column(page["layout_text"], "900,000.00", "Debit")
    # And the reader is given that view, not the cell-per-line text.
    reader_text = document_capabilities.evidence_read_text(ws, document["id"])
    assert reader_text == page["layout_text"]


def test_an_older_extraction_gains_its_layout_without_moving_its_text_hash():
    """Re-extracting would reset the document's status and queue a search
    reindex for text that has not changed. The backfill writes the layout and
    nothing else, so every hash keyed to the plain text stays where it was."""

    ws = workspaces.create_workspace("Layout backfill")
    document = documents.add_document(
        ws, "statement.pdf", _pdf(STATEMENT_PDF), category="evidence"
    )
    extracted = documents.extract_document(ws, document["id"])
    legacy = {
        **{key: value for key, value in extracted.items() if key != "layout_version"},
        "pages": [
            {key: value for key, value in page.items() if key != "layout_text"}
            for page in extracted["pages"]
        ],
    }
    workspaces.write_json_atomic(documents.cache_path(ws, document["id"]), legacy)
    assert "layout_text" not in documents.cached_extraction(ws, document["id"])["pages"][0]

    upgraded = documents.ensure_layout(ws, document["id"])
    assert upgraded["extracted_text_sha1"] == extracted["extracted_text_sha1"]
    assert upgraded["pages"][0]["text"] == extracted["pages"][0]["text"]
    assert upgraded["pages"][0]["layout_text"] == extracted["pages"][0]["layout_text"]
    assert documents.cached_extraction(ws, document["id"])["layout_version"] == 1


def test_a_document_the_layout_pushes_over_the_window_is_read_as_plain_text(
    monkeypatch,
):
    ws = workspaces.create_workspace("Layout window")
    document = documents.add_document(
        ws, "statement.pdf", _pdf(STATEMENT_PDF), category="evidence"
    )
    page = documents.extract_document(ws, document["id"])["pages"][0]
    assert len(page["layout_text"]) > len(page["text"])
    monkeypatch.setattr(
        document_capabilities.presets,
        "EVIDENCE_READ_CHARACTERS",
        len(page["text"]),
    )
    assert document_capabilities.evidence_read_text(ws, document["id"]) == page["text"]
