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
        "common.party.name",
        "Party name",
        "parties",
        "name",
        (FieldAttributeDefinition("name", "text"),),
    ),
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
