"""Built-in procure-to-pay transaction-evidence pack."""

from __future__ import annotations

from ..models import (
    CyclePackDefinition,
    FieldAttributeDefinition,
    FieldKindDefinition,
    IdentifierKindDefinition,
    RecordKindDefinition,
)

PACK_ID = "procure_to_pay"

IDENTIFIER_KINDS = (
    IdentifierKindDefinition(
        "procure_to_pay.requisition_number",
        "Purchase requisition number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "procure_to_pay.purchase_order_number",
        "Purchase order number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "procure_to_pay.goods_receipt_number",
        "Goods receipt number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "procure_to_pay.vendor_invoice_number",
        "Vendor invoice number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "procure_to_pay.internal_invoice_id",
        "Internal invoice identifier",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
    IdentifierKindDefinition(
        "procure_to_pay.payment_voucher_number",
        "Payment voucher number",
        "transaction",
        "identifier",
        "common.conservative_identifier",
    ),
)

FIELD_KINDS = (
    FieldKindDefinition(
        "procure_to_pay.date.receipt",
        "Receipt date",
        "dates",
        "receipt_date",
        (
            FieldAttributeDefinition("value", "date"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "procure_to_pay.date.payment",
        "Payment date",
        "dates",
        "payment_date",
        (
            FieldAttributeDefinition("value", "date"),
            FieldAttributeDefinition("raw_value", "text"),
        ),
    ),
    FieldKindDefinition(
        "procure_to_pay.approval.request",
        "Purchase request approval",
        "approvals",
        "request_approval",
        (
            FieldAttributeDefinition("approver", "text"),
            FieldAttributeDefinition("decision", "text"),
            FieldAttributeDefinition("date", "date"),
        ),
    ),
)

RECORD_KINDS = (
    RecordKindDefinition(
        "procure_to_pay.purchase_requisition",
        "Purchase requisition",
        ("procure_to_pay.requisition_number",),
        ("procure_to_pay.approval.request",),
    ),
    RecordKindDefinition(
        "procure_to_pay.purchase_order",
        "Purchase order",
        ("procure_to_pay.purchase_order_number",),
        ("common.amount.total",),
    ),
    RecordKindDefinition(
        "procure_to_pay.goods_receipt",
        "Goods receipt",
        ("procure_to_pay.goods_receipt_number",),
        ("procure_to_pay.date.receipt",),
    ),
    RecordKindDefinition(
        "procure_to_pay.vendor_invoice",
        "Vendor invoice",
        (
            "procure_to_pay.vendor_invoice_number",
            "procure_to_pay.internal_invoice_id",
        ),
        ("common.amount.total",),
    ),
    RecordKindDefinition(
        "procure_to_pay.payment_voucher",
        "Payment voucher",
        ("procure_to_pay.payment_voucher_number",),
        ("common.amount.total", "procure_to_pay.date.payment"),
    ),
)

PACK = CyclePackDefinition(
    id=PACK_ID,
    label="Procure to pay",
    version=1,
    normalizer_ids=("common.conservative_identifier",),
    identifier_kind_ids=(
        *(definition.id for definition in IDENTIFIER_KINDS),
        "common.vendor_id",
        "common.buyer_id",
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
