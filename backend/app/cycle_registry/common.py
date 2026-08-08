"""Domain-neutral definitions shared by transaction-evidence packs."""

from __future__ import annotations

import unicodedata

from .models import (
    EvidenceKindDefinition,
    FieldAttributeDefinition,
    FieldKindDefinition,
    IdentifierKindDefinition,
    NormalizerDefinition,
    RecordKindDefinition,
)


def conservative_identifier(value: object) -> str:
    """Normalize presentation only; preserve punctuation and alphanumerics."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.strip().split()).casefold()


NORMALIZERS = (
    NormalizerDefinition(
        "common.conservative_identifier",
        "Conservative identifier normalization",
        "nfkc-trim-collapse-casefold-v1",
        conservative_identifier,
    ),
)

IDENTIFIER_KINDS = (
    IdentifierKindDefinition(
        "common.vendor_id",
        "Vendor identifier",
        "non_linking",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "common.customer_id",
        "Customer identifier",
        "non_linking",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "common.buyer_id",
        "Buyer identifier",
        "non_linking",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "common.employee_id",
        "Employee identifier",
        "non_linking",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "common.department_id",
        "Department identifier",
        "non_linking",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "common.account_number",
        "Account number",
        "non_linking",
        "identifier",
        "common.conservative_identifier",
    ),
)

# Every pack shares these. A transaction record states far more than its own
# primary identity — who it names, what it is for, when it was raised, what was
# approved, what it encloses — and a field kind a pack declares but no record
# kind makes available is a fact the extraction worker can see, cite, and then
# has nowhere to put. So each of these is registered here *and* offered on every
# record kind through ``BASE_FIELD_KIND_IDS`` below.
FIELD_KINDS = (
    FieldKindDefinition(
        "common.amount.total",
        "Total amount",
        "amounts",
        "total",
        (
            FieldAttributeDefinition("value", "number"),
            FieldAttributeDefinition("raw_value", "text"),
            FieldAttributeDefinition("currency", "text"),
        ),
    ),
    FieldKindDefinition(
        "common.amount.unit_price",
        "Unit price",
        "amounts",
        "unit_price",
        (
            FieldAttributeDefinition("value", "number"),
            FieldAttributeDefinition("raw_value", "text"),
            FieldAttributeDefinition("currency", "text"),
        ),
    ),
    FieldKindDefinition(
        # ``role`` is what makes one field kind enough for every party a record
        # names: vendor, buyer, employee, preparer, bank. One entry per party,
        # distinguished by the fact's ``entry`` ordinal.
        "common.party.name",
        "Party name",
        "parties",
        "name",
        (
            FieldAttributeDefinition("name", "text"),
            FieldAttributeDefinition("role", "text", verbatim=False),
        ),
    ),
    FieldKindDefinition(
        "common.party.address",
        "Party address",
        "parties",
        "address",
        (
            FieldAttributeDefinition("value", "text"),
            FieldAttributeDefinition("raw_value", "text"),
            FieldAttributeDefinition("role", "text", verbatim=False),
        ),
    ),
    FieldKindDefinition(
        "common.quantity.total",
        "Total quantity",
        "quantities",
        "total",
        (
            FieldAttributeDefinition("value", "number"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "common.status",
        "Record status",
        "statuses",
        "status",
        (
            FieldAttributeDefinition("value", "text"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        # The date the record itself bears. Cycle-specific dates that describe a
        # *different* event — goods received, payment made — stay in their pack.
        "common.date.document",
        "Document date",
        "dates",
        "document_date",
        (
            FieldAttributeDefinition("value", "date"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "common.date.due",
        "Due date",
        "dates",
        "due_date",
        (
            FieldAttributeDefinition("value", "date"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "common.description",
        "Description",
        "descriptions",
        "description",
        (
            FieldAttributeDefinition("value", "text"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "common.note",
        "Note or comment",
        "notes",
        "note",
        (
            FieldAttributeDefinition("value", "text"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        # One approval, signature, verification, or authorization the record
        # carries. ``role`` keeps the stage the record printed ("Financial
        # approval", "Verified by") instead of flattening it away, and the
        # fact's ``entry`` ordinal keeps approver and date paired.
        "common.approval",
        "Approval",
        "approvals",
        "approval",
        (
            FieldAttributeDefinition("approver", "text", control_evidence=True),
            FieldAttributeDefinition(
                "role", "text", verbatim=False, control_evidence=True
            ),
            FieldAttributeDefinition(
                "decision", "text", verbatim=False, control_evidence=True
            ),
            FieldAttributeDefinition("date", "date", control_evidence=True),
        ),
    ),
    FieldKindDefinition(
        "common.attachment",
        "Attachment",
        "attachments",
        "attachment",
        (
            FieldAttributeDefinition(
                "kind", "text", verbatim=False, control_evidence=True
            ),
            FieldAttributeDefinition("reference", "text", control_evidence=True),
            FieldAttributeDefinition(
                "present", "boolean", verbatim=False, control_evidence=True
            ),
        ),
    ),
)

# Offered on every registered record kind of every pack. A record cannot state a
# party, a date, an amount, an approval, or an attachment that the contract has
# no room for, which is what forced the worker to either drop the fact or
# relabel it as an unrelated registered field.
BASE_FIELD_KIND_IDS: tuple[str, ...] = tuple(
    definition.id for definition in FIELD_KINDS
)

RECORD_KINDS = (
    RecordKindDefinition(
        "common.other",
        "Other or unresolved transaction evidence",
        (),
        (),
        bindable=False,
    ),
)

EVIDENCE_KINDS = (
    EvidenceKindDefinition("transaction_cycle", "Transaction cycle", "required"),
    EvidenceKindDefinition("tabular_population", "Tabular population", "forbidden"),
    EvidenceKindDefinition("document_content", "Document content", "forbidden"),
    EvidenceKindDefinition("manual_inspection", "Manual inspection", "forbidden"),
    EvidenceKindDefinition("inquiry", "Inquiry", "forbidden"),
    EvidenceKindDefinition("mixed", "Mixed evidence", "forbidden"),
)
