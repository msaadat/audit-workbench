"""Built-in payroll pack used to prove the core is not procurement-shaped."""

from __future__ import annotations

from ..models import (
    CyclePackDefinition,
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

RECORD_KINDS = (
    RecordKindDefinition(
        "payroll.employment_contract",
        "Employment contract",
        ("payroll.employment_contract_number",),
        (),
    ),
    RecordKindDefinition(
        "payroll.time_record",
        "Time record",
        ("payroll.time_record_number",),
        ("payroll.hours.regular", "payroll.date.period_end"),
    ),
    RecordKindDefinition(
        "payroll.payroll_register",
        "Payroll register",
        ("payroll.payroll_run_number",),
        (
            "payroll.amount.gross_pay",
            "payroll.amount.net_pay",
            "payroll.date.period_end",
        ),
    ),
    RecordKindDefinition(
        "payroll.payslip",
        "Payslip",
        ("payroll.payslip_number",),
        ("payroll.amount.gross_pay", "payroll.amount.net_pay"),
    ),
    RecordKindDefinition(
        "payroll.bank_payment",
        "Payroll bank payment",
        ("payroll.bank_payment_number",),
        ("common.amount.total",),
    ),
)

PACK = CyclePackDefinition(
    id=PACK_ID,
    label="Payroll",
    version=1,
    normalizer_ids=("common.conservative_identifier",),
    identifier_kind_ids=(
        *(definition.id for definition in IDENTIFIER_KINDS),
        "common.employee_id",
        "common.department_id",
        "common.account_number",
    ),
    field_kind_ids=(
        "common.amount.total",
        "common.party.name",
        *(definition.id for definition in FIELD_KINDS),
    ),
    record_kind_ids=(
        *(definition.id for definition in RECORD_KINDS),
        "common.other",
    ),
)

