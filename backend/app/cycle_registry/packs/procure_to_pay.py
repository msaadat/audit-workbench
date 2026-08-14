"""Built-in procure-to-pay transaction-evidence pack."""

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
)

# Procurement records routinely print the dates of *other* records in the cycle:
# a purchase order carries the receipt date of the goods it covers, an invoice
# carries the date it was received. Those are cross-references the record
# genuinely states, and an audit reads them, so they are available wherever the
# record kind can print them rather than only on the record they describe.
_CYCLE_DATES = ("procure_to_pay.date.receipt", "procure_to_pay.date.payment")

RECORD_KINDS = (
    RecordKindDefinition(
        "procure_to_pay.purchase_requisition",
        "Purchase requisition",
        ("procure_to_pay.requisition_number",),
        BASE_FIELD_KIND_IDS,
    ),
    RecordKindDefinition(
        "procure_to_pay.purchase_order",
        "Purchase order",
        ("procure_to_pay.purchase_order_number",),
        (*BASE_FIELD_KIND_IDS, "procure_to_pay.date.receipt"),
    ),
    RecordKindDefinition(
        "procure_to_pay.goods_receipt",
        "Goods receipt",
        ("procure_to_pay.goods_receipt_number",),
        (*BASE_FIELD_KIND_IDS, "procure_to_pay.date.receipt"),
    ),
    RecordKindDefinition(
        "procure_to_pay.vendor_invoice",
        "Vendor invoice",
        (
            "procure_to_pay.vendor_invoice_number",
            "procure_to_pay.internal_invoice_id",
        ),
        (*BASE_FIELD_KIND_IDS, *_CYCLE_DATES),
    ),
    RecordKindDefinition(
        "procure_to_pay.payment_voucher",
        "Payment voucher",
        (
            "procure_to_pay.payment_voucher_number",
            "procure_to_pay.vendor_invoice_number",
            "procure_to_pay.internal_invoice_id",
        ),
        (*BASE_FIELD_KIND_IDS, "procure_to_pay.date.payment"),
    ),
)

# The procure-to-pay chronology, used only to reject a date comparison stated
# backwards. Ranks are sparse so a later stage can be inserted between two of
# them without renumbering, and shared where two records state one event: a
# purchase order's receipt date is its copy of the delivery the goods receipt
# records, so neither precedes the other. A vendor invoice's receipt date is
# deliberately absent — on that record it may mean the goods or the invoice —
# and an omitted field is simply never direction-checked.
DATE_LIFECYCLE = (
    DateLifecycleStage("procure_to_pay.purchase_requisition", "dates", "document_date", 10),
    DateLifecycleStage("procure_to_pay.purchase_requisition", "approvals", "approval", 20),
    DateLifecycleStage("procure_to_pay.purchase_order", "dates", "document_date", 30),
    DateLifecycleStage("procure_to_pay.purchase_order", "approvals", "approval", 30),
    DateLifecycleStage("procure_to_pay.goods_receipt", "dates", "document_date", 40),
    DateLifecycleStage("procure_to_pay.goods_receipt", "dates", "receipt_date", 40),
    DateLifecycleStage("procure_to_pay.goods_receipt", "approvals", "approval", 40),
    DateLifecycleStage("procure_to_pay.purchase_order", "dates", "receipt_date", 40),
    DateLifecycleStage("procure_to_pay.vendor_invoice", "dates", "document_date", 50),
    DateLifecycleStage("procure_to_pay.vendor_invoice", "approvals", "approval", 55),
    DateLifecycleStage("procure_to_pay.payment_voucher", "dates", "document_date", 60),
    DateLifecycleStage("procure_to_pay.payment_voucher", "dates", "payment_date", 60),
    DateLifecycleStage("procure_to_pay.payment_voucher", "approvals", "approval", 60),
    DateLifecycleStage("procure_to_pay.vendor_invoice", "dates", "due_date", 70),
)

PACK = CyclePackDefinition(
    id=PACK_ID,
    label="Procure to pay",
    version=7,
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
        *BASE_FIELD_KIND_IDS,
        *(definition.id for definition in FIELD_KINDS),
    ),
    record_kind_ids=(
        *(definition.id for definition in RECORD_KINDS),
        "common.other",
    ),
    date_lifecycle=DATE_LIFECYCLE,
)
