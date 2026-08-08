"""Built-in payroll pack used to prove the core is not procurement-shaped."""

from __future__ import annotations

from ..common import BASE_FIELD_KIND_IDS
from ..models import (
    CyclePackDefinition,
    DateLifecycleStage,
    FieldAttributeDefinition,
    FieldKindDefinition,
    IdentifierKindDefinition,
    RecordKindDefinition,
)

PACK_ID = "payroll"

IDENTIFIER_KINDS = (
    IdentifierKindDefinition(
        "payroll.employment_contract_number",
        "Employment contract number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "payroll.time_record_number",
        "Time record number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "payroll.payroll_run_number",
        "Payroll run number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "payroll.payslip_number",
        "Payslip number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "payroll.bank_payment_number",
        "Payroll bank payment number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
)

FIELD_KINDS = (
    FieldKindDefinition(
        "payroll.amount.gross_pay",
        "Gross pay",
        "amounts",
        "gross_pay",
        (
            FieldAttributeDefinition("value", "number"),
            FieldAttributeDefinition("raw_value", "text"),
            FieldAttributeDefinition("currency", "text"),
        ),
    ),
    FieldKindDefinition(
        "payroll.amount.net_pay",
        "Net pay",
        "amounts",
        "net_pay",
        (
            FieldAttributeDefinition("value", "number"),
            FieldAttributeDefinition("raw_value", "text"),
            FieldAttributeDefinition("currency", "text"),
        ),
    ),
    FieldKindDefinition(
        "payroll.date.period_end",
        "Pay-period end date",
        "dates",
        "pay_period_end",
        (
            FieldAttributeDefinition("value", "date"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "payroll.hours.regular",
        "Regular hours",
        "quantities",
        "regular_hours",
        (
            FieldAttributeDefinition("value", "number"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
)

_PAY_AMOUNTS = ("payroll.amount.gross_pay", "payroll.amount.net_pay")

RECORD_KINDS = (
    RecordKindDefinition(
        "payroll.employment_contract",
        "Employment contract",
        ("payroll.employment_contract_number",),
        BASE_FIELD_KIND_IDS,
    ),
    RecordKindDefinition(
        "payroll.time_record",
        "Time record",
        ("payroll.time_record_number",),
        (*BASE_FIELD_KIND_IDS, "payroll.hours.regular", "payroll.date.period_end"),
    ),
    RecordKindDefinition(
        "payroll.payroll_register",
        "Payroll register",
        ("payroll.payroll_run_number",),
        (*BASE_FIELD_KIND_IDS, *_PAY_AMOUNTS, "payroll.date.period_end"),
    ),
    RecordKindDefinition(
        "payroll.payslip",
        "Payslip",
        ("payroll.payslip_number",),
        (*BASE_FIELD_KIND_IDS, *_PAY_AMOUNTS, "payroll.date.period_end"),
    ),
    RecordKindDefinition(
        "payroll.bank_payment",
        "Payroll bank payment",
        ("payroll.bank_payment_number",),
        BASE_FIELD_KIND_IDS,
    ),
)

# The payroll chronology. Every record that states the pay period shares one
# rank: the period end is the same date wherever it is printed, so a comparison
# between two of them is an agreement test, not an ordering test.
DATE_LIFECYCLE = (
    DateLifecycleStage("payroll.employment_contract", "dates", "document_date", 10),
    DateLifecycleStage("payroll.time_record", "dates", "pay_period_end", 20),
    DateLifecycleStage("payroll.payroll_register", "dates", "pay_period_end", 20),
    DateLifecycleStage("payroll.payslip", "dates", "pay_period_end", 20),
    DateLifecycleStage("payroll.time_record", "dates", "document_date", 25),
    DateLifecycleStage("payroll.time_record", "approvals", "approval", 25),
    DateLifecycleStage("payroll.payroll_register", "dates", "document_date", 30),
    DateLifecycleStage("payroll.payroll_register", "approvals", "approval", 30),
    DateLifecycleStage("payroll.payslip", "dates", "document_date", 40),
    DateLifecycleStage("payroll.bank_payment", "dates", "document_date", 50),
    DateLifecycleStage("payroll.bank_payment", "approvals", "approval", 50),
)

PACK = CyclePackDefinition(
    id=PACK_ID,
    label="Payroll",
    version=4,
    normalizer_ids=("common.conservative_identifier",),
    identifier_kind_ids=(
        *(definition.id for definition in IDENTIFIER_KINDS),
        "common.employee_id",
        "common.department_id",
        "common.account_number",
    ),
    field_kind_ids=(
        *BASE_FIELD_KIND_IDS,
        *(definition.id for definition in FIELD_KINDS),
    ),
    record_kind_ids=(
        *(definition.id for definition in RECORD_KINDS),
        "common.other",
    ),
    date_lifecycle=DATE_LIFECYCLE,
)

