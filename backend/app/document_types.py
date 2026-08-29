"""The closed, global catalog of document types.

One list serves every engagement area. A document type belongs to the
*document*, not to a cycle: an air waybill is procurement, logistics, and
trade-finance evidence at once, and an employment contract is payroll evidence
and contract evidence both. Ids are therefore flat and globally unique, and the
``area`` on each entry exists only to group the catalog for a prompt or a
picker. Nothing keys off it.

Grain: a type is distinct when an auditor would expect a different set of fields
on it, or would give it a different role in a cycle. Finer than that fragments a
corpus across near-synonyms; coarser than that makes roles indistinguishable.
Jurisdictional variants are schema, not type — a VAT tax invoice is a
``vendor_invoice`` whose induced schema happens to carry tax-registration
fields, because splitting it out would demand a discrimination the classifier
cannot reliably make.

Ids are permanent. Extractions and approved rulesets store them, so an id is
never renamed or reused; ``active=False`` withdraws an entry from new
classification without rewriting what already refers to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

#: Auditor-coined types live in their own namespace. This is *provenance*, not
#: domain — it records who authored the id, so a workspace's coined
#: ``letter_of_indemnity`` can never be silently conflated with a global entry
#: of the same name added in a later release. It is not the area prefix this
#: module deliberately avoids: that one would have encoded a cycle.
LOCAL_PREFIX = "local."

#: Assigned when no listed type fits. Carries free text and cannot fill a role
#: until an auditor retypes it.
OTHER = "other"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_LOCAL_ID_RE = re.compile(r"^local\.[a-z][a-z0-9_]*$")


class DocumentTypeError(ValueError):
    """A document type reference is unknown, malformed, or withdrawn."""


@dataclass(frozen=True)
class DocumentTypeDefinition:
    id: str
    label: str
    area: str
    #: What separates this type from its neighbours. Prompt text — this is what
    #: actually drives classification accuracy, far more than the label does.
    discriminator: str
    #: Titles the document may genuinely carry. Without these a "GRN" or a
    #: "Goods Received Note" fragments away from ``goods_receipt`` or falls to
    #: ``other``.
    aliases: tuple[str, ...] = ()
    active: bool = True

    def identity(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "area": self.area,
            "discriminator": self.discriminator,
            "aliases": list(self.aliases),
            "active": self.active,
        }


def _t(
    id: str, label: str, area: str, discriminator: str, *aliases: str
) -> DocumentTypeDefinition:
    return DocumentTypeDefinition(id, label, area, discriminator, tuple(aliases))


AREAS: tuple[tuple[str, str], ...] = (
    ("procure_to_pay", "Procure to pay"),
    ("order_to_cash", "Order to cash"),
    ("payroll_hr", "Payroll and HR"),
    ("treasury_banking", "Treasury and banking"),
    ("inventory_logistics", "Inventory and logistics"),
    ("fixed_assets", "Fixed assets"),
    ("financial_reporting", "Financial reporting and general ledger"),
    ("tax_statutory", "Tax and statutory"),
    ("governance", "Governance and cross-cutting"),
)

DEFINITIONS: tuple[DocumentTypeDefinition, ...] = (
    # ---- procure to pay -----------------------------------------------------
    _t("purchase_requisition", "Purchase requisition", "procure_to_pay",
       "Internal request to buy, before a supplier is committed",
       "PR", "purchase request", "requisition"),
    _t("request_for_quotation", "Request for quotation", "procure_to_pay",
       "Solicitation issued to suppliers",
       "RFQ", "tender invitation", "ITT", "invitation to tender"),
    _t("vendor_quotation", "Vendor quotation", "procure_to_pay",
       "Supplier's priced offer, not yet an order",
       "quote", "quotation", "bid", "proposal", "estimate"),
    _t("purchase_order", "Purchase order", "procure_to_pay",
       "Entity's committed order to a supplier",
       "PO", "order confirmation", "purchase contract"),
    _t("goods_receipt", "Goods receipt", "procure_to_pay",
       "Internal record that goods were received and accepted",
       "GRN", "goods received note", "receiving report", "goods receipt note"),
    _t("delivery_note", "Delivery note", "procure_to_pay",
       "Supplier's document accompanying a shipment",
       "despatch note", "dispatch note", "delivery challan", "DN"),
    _t("service_acceptance", "Service acceptance", "procure_to_pay",
       "Confirmation that a service was performed and accepted",
       "service entry sheet", "SES", "completion certificate", "work completion"),
    _t("vendor_invoice", "Vendor invoice", "procure_to_pay",
       "Supplier's demand for payment addressed to the entity",
       "supplier invoice", "bill", "tax invoice", "purchase invoice"),
    _t("vendor_credit_note", "Vendor credit note", "procure_to_pay",
       "Supplier's reduction of an amount previously invoiced",
       "credit memo", "supplier credit note"),
    _t("vendor_debit_note", "Vendor debit note", "procure_to_pay",
       "Entity's charge back to a supplier",
       "debit memo", "debit note"),
    _t("payment_voucher", "Payment voucher", "procure_to_pay",
       "Internal authorization to disburse against invoices",
       "disbursement voucher", "payment request", "cash payment voucher"),
    _t("remittance_advice", "Remittance advice", "procure_to_pay",
       "Notification of what a payment settles",
       "payment advice", "remittance"),
    _t("vendor_statement", "Vendor statement", "procure_to_pay",
       "Supplier's periodic list of open items",
       "supplier statement", "statement of account", "AP statement"),
    _t("vendor_master_form", "Vendor master change form", "procure_to_pay",
       "Request to create or amend supplier standing data",
       "vendor onboarding form", "supplier setup", "vendor creation form"),
    # ---- order to cash ------------------------------------------------------
    _t("sales_order", "Sales order", "order_to_cash",
       "Customer's accepted order on the entity",
       "customer order", "SO", "order acknowledgement"),
    _t("proforma_invoice", "Proforma invoice", "order_to_cash",
       "Advance invoice issued before supply, not a demand for payment",
       "pro-forma", "pro forma invoice"),
    _t("sales_invoice", "Sales invoice", "order_to_cash",
       "Entity's demand for payment issued to a customer",
       "customer invoice", "output invoice", "revenue invoice"),
    _t("sales_credit_note", "Sales credit note", "order_to_cash",
       "Entity's reduction of an amount previously billed",
       "customer credit memo", "credit note issued"),
    _t("customer_receipt", "Customer receipt", "order_to_cash",
       "Acknowledgement of cash received from a customer",
       "official receipt", "cash receipt", "OR"),
    _t("customer_statement", "Customer statement", "order_to_cash",
       "Entity's periodic list of open customer items",
       "AR statement", "debtor statement"),
    _t("customer_master_form", "Customer master change form", "order_to_cash",
       "Request to create or amend customer standing data",
       "customer onboarding form", "customer setup"),
    _t("credit_approval", "Credit approval", "order_to_cash",
       "Decision granting or amending a customer credit limit",
       "credit application", "credit limit approval"),
    # ---- payroll and HR -----------------------------------------------------
    _t("employment_contract", "Employment contract", "payroll_hr",
       "Agreement establishing employment terms",
       "offer letter", "appointment letter", "contract of employment"),
    _t("employee_master_form", "Employee master change form", "payroll_hr",
       "Request to create or amend employee standing data",
       "personnel action form", "HR change form", "employee setup"),
    _t("timesheet", "Timesheet", "payroll_hr",
       "Record of hours worked by period",
       "time record", "attendance sheet", "clock report", "time card"),
    _t("leave_record", "Leave record", "payroll_hr",
       "Record of absence taken or approved",
       "leave application", "absence record", "vacation request"),
    _t("payslip", "Payslip", "payroll_hr",
       "Individual statement of pay for one employee and period",
       "pay stub", "salary slip", "wage slip", "pay advice"),
    _t("payroll_register", "Payroll register", "payroll_hr",
       "Entity-wide list of pay for one run",
       "payroll summary", "payroll journal", "salary register"),
    _t("payroll_bank_file", "Payroll bank file", "payroll_hr",
       "Consolidated instruction to pay a payroll run",
       "salary transfer list", "bank upload file", "payroll disbursement list"),
    _t("withholding_certificate", "Withholding tax certificate", "payroll_hr",
       "Certificate of tax withheld from a payee",
       "tax deduction certificate", "Form 16", "1099", "TDS certificate"),
    _t("social_security_filing", "Social security filing", "payroll_hr",
       "Statutory contribution return or receipt",
       "pension filing", "EPF return", "ESI return", "payroll tax return"),
    _t("expense_claim", "Expense claim", "payroll_hr",
       "Employee's request for reimbursement",
       "expense report", "reimbursement claim", "claim form"),
    _t("travel_authorization", "Travel authorization", "payroll_hr",
       "Approval of travel before it is incurred",
       "travel request", "trip approval", "travel requisition"),
    # ---- treasury and banking -----------------------------------------------
    _t("bank_statement", "Bank statement", "treasury_banking",
       "Bank's periodic record of account movements",
       "account statement", "passbook", "bank account statement"),
    _t("bank_confirmation", "Bank confirmation", "treasury_banking",
       "Bank's direct reply confirming balances or facilities",
       "bank letter", "standard confirmation", "bank certificate"),
    _t("payment_instruction", "Payment instruction", "treasury_banking",
       "Entity's instruction to a bank to transfer funds",
       "transfer request", "wire instruction", "RTGS request", "SWIFT request"),
    _t("cheque", "Cheque", "treasury_banking",
       "Negotiable instrument drawn on a bank account",
       "check", "demand draft", "banker's cheque"),
    _t("bank_reconciliation", "Bank reconciliation", "treasury_banking",
       "Working reconciling ledger to bank balance",
       "bank rec", "bank reconciliation statement"),
    _t("petty_cash_voucher", "Petty cash voucher", "treasury_banking",
       "Record of a small cash disbursement",
       "cash voucher", "IOU", "petty cash slip"),
    _t("loan_agreement", "Loan agreement", "treasury_banking",
       "Contract establishing borrowing terms",
       "facility agreement", "credit agreement", "loan contract"),
    _t("loan_statement", "Loan statement", "treasury_banking",
       "Lender's record of drawdowns, interest, and repayments",
       "amortization schedule", "facility statement", "repayment schedule"),
    _t("fx_contract", "FX contract", "treasury_banking",
       "Confirmation of a foreign-exchange deal",
       "forward contract", "FX deal ticket", "foreign exchange confirmation"),
    _t("investment_confirmation", "Investment confirmation", "treasury_banking",
       "Confirmation of a securities or deposit transaction",
       "trade confirmation", "deposit advice", "contract note", "fixed deposit receipt"),
    _t("letter_of_credit", "Letter of credit", "treasury_banking",
       "Bank undertaking to pay on documentary presentation",
       "LC", "documentary credit"),
    _t("bank_guarantee", "Bank guarantee", "treasury_banking",
       "Bank undertaking to pay on demand or default",
       "performance bond", "standby LC", "guarantee"),
    _t("treasury_deal_ticket", "Treasury deal ticket", "treasury_banking",
       "Internal record authorizing a treasury transaction",
       "deal slip", "dealing ticket"),
    # ---- inventory and logistics --------------------------------------------
    _t("material_requisition", "Material requisition", "inventory_logistics",
       "Internal request to issue stock",
       "stores requisition", "MR", "stock requisition"),
    _t("goods_issue_note", "Goods issue note", "inventory_logistics",
       "Record that stock left the store",
       "GIN", "issue slip", "material issue note"),
    _t("stock_transfer_note", "Stock transfer note", "inventory_logistics",
       "Record of movement between locations",
       "transfer note", "STN", "inter-branch transfer"),
    _t("stock_count_sheet", "Stock count sheet", "inventory_logistics",
       "Record of a physical inventory count",
       "count tag", "inventory sheet", "stock take sheet"),
    _t("packing_list", "Packing list", "inventory_logistics",
       "Itemization of a shipment's contents",
       "packing slip", "packing note"),
    _t("air_waybill", "Air waybill", "inventory_logistics",
       "Air carrier's contract and receipt for goods",
       "AWB", "airway bill"),
    _t("bill_of_lading", "Bill of lading", "inventory_logistics",
       "Sea or land carrier's contract and receipt for goods",
       "BOL", "B/L", "consignment note", "CMR", "lorry receipt"),
    _t("customs_declaration", "Customs declaration", "inventory_logistics",
       "Filing to customs on import or export",
       "bill of entry", "SAD", "shipping bill", "customs entry"),
    _t("inspection_certificate", "Inspection certificate", "inventory_logistics",
       "Third-party attestation of quality or quantity",
       "QC certificate", "certificate of analysis", "survey report"),
    # ---- fixed assets -------------------------------------------------------
    _t("capitalization_form", "Capitalization form", "fixed_assets",
       "Record placing an asset into service",
       "asset capitalization", "WIP settlement", "asset commissioning"),
    _t("asset_register_extract", "Asset register extract", "fixed_assets",
       "Listing of assets and carrying values",
       "FAR extract", "asset listing", "fixed asset register"),
    _t("asset_disposal_form", "Asset disposal form", "fixed_assets",
       "Authorization and record of retirement or sale",
       "disposal note", "retirement form", "scrapping note"),
    _t("depreciation_schedule", "Depreciation schedule", "fixed_assets",
       "Computation of periodic depreciation",
       "depreciation run report", "depreciation working"),
    _t("asset_verification_sheet", "Asset verification sheet", "fixed_assets",
       "Record of a physical asset inspection",
       "asset count sheet", "asset verification report"),
    # ---- financial reporting and general ledger -----------------------------
    _t("journal_entry", "Journal entry", "financial_reporting",
       "Manual or adjusting posting with supporting narrative",
       "JV", "journal voucher", "adjusting entry", "GL entry"),
    _t("account_reconciliation", "Account reconciliation", "financial_reporting",
       "Working reconciling a ledger account to support",
       "balance sheet rec", "GL rec", "account reconciliation statement"),
    _t("trial_balance", "Trial balance", "financial_reporting",
       "Listing of ledger balances at a date",
       "TB", "trial balance report"),
    _t("general_ledger_extract", "General ledger extract", "financial_reporting",
       "Transaction-level ledger listing",
       "GL detail", "account activity", "ledger dump"),
    _t("accrual_schedule", "Accrual schedule", "financial_reporting",
       "Computation supporting an accrual or provision",
       "provision schedule", "accrual listing", "provision working"),
    _t("financial_statements", "Financial statements", "financial_reporting",
       "Prepared primary statements and notes",
       "FS", "annual accounts", "management accounts"),
    _t("intercompany_confirmation", "Intercompany confirmation", "financial_reporting",
       "Agreement of balances between group entities",
       "IC reconciliation", "IC confirmation", "intercompany balance confirmation"),
    # ---- tax and statutory --------------------------------------------------
    _t("tax_return", "Tax return", "tax_statutory",
       "Filing submitted to a tax authority",
       "VAT return", "GST return", "income tax return", "sales tax return"),
    _t("tax_assessment", "Tax assessment", "tax_statutory",
       "Authority's determination of tax due",
       "assessment order", "notice of assessment", "tax demand"),
    _t("tax_payment_receipt", "Tax payment receipt", "tax_statutory",
       "Evidence of tax remitted",
       "challan", "tax payment confirmation", "tax deposit slip"),
    _t("statutory_filing", "Statutory filing", "tax_statutory",
       "Non-tax regulatory submission",
       "annual return", "regulatory return", "statutory return"),
    # ---- governance and cross-cutting ---------------------------------------
    _t("contract", "Contract", "governance",
       "Agreement not covered by a more specific type",
       "agreement", "MOU", "SLA", "service agreement"),
    _t("approval_form", "Approval form", "governance",
       "Standalone authorization not embedded in another record",
       "authorization form", "sign-off sheet", "approval memo"),
    _t("delegation_of_authority", "Delegation of authority", "governance",
       "Schedule of who may approve what, to what limit",
       "DOA", "authority matrix", "signature schedule", "approval matrix"),
    _t("board_minutes", "Board or committee minutes", "governance",
       "Minuted decisions of a governing body",
       "minutes", "resolution", "committee minutes"),
    _t("insurance_policy", "Insurance policy", "governance",
       "Cover note or policy schedule",
       "policy schedule", "cover note", "insurance certificate"),
    _t("receipt", "Receipt", "governance",
       "Generic acknowledgement of payment received",
       "cash receipt", "till receipt", "payment receipt"),
    _t("certificate", "Certificate", "governance",
       "Attestation not covered by a more specific type",
       "licence", "license", "registration certificate"),
    _t("correspondence", "Correspondence", "governance",
       "Letter, email, or memo used as evidence",
       "email", "letter", "memo", "note"),
    _t(OTHER, "Other", "governance",
       "None of the above; requires free-text document_type_other"),
)


def _validated() -> Mapping[str, DocumentTypeDefinition]:
    known_areas = {area for area, _ in AREAS}
    output: dict[str, DocumentTypeDefinition] = {}
    seen_labels: set[str] = set()
    for definition in DEFINITIONS:
        if not _ID_RE.fullmatch(definition.id):
            raise DocumentTypeError(f"Document type id '{definition.id}' is invalid.")
        if definition.id in output:
            raise DocumentTypeError(f"Duplicate document type id '{definition.id}'.")
        if definition.area not in known_areas:
            raise DocumentTypeError(
                f"Document type '{definition.id}' names unknown area '{definition.area}'."
            )
        if not definition.discriminator.strip():
            raise DocumentTypeError(
                f"Document type '{definition.id}' needs a discriminator."
            )
        # Two entries reading the same in a picker is a classification hazard,
        # not merely untidy: the model is choosing from labels.
        label_key = definition.label.strip().casefold()
        if label_key in seen_labels:
            raise DocumentTypeError(f"Duplicate document type label '{definition.label}'.")
        seen_labels.add(label_key)
        output[definition.id] = definition
    if OTHER not in output:
        raise DocumentTypeError("The catalog must include the 'other' fallback.")
    return MappingProxyType(output)


BY_ID: Mapping[str, DocumentTypeDefinition] = _validated()

#: Ids a classifier may return. ``other`` is included; withdrawn entries are not.
SELECTABLE_IDS: tuple[str, ...] = tuple(
    definition.id for definition in DEFINITIONS if definition.active
)


def is_local(document_type: object) -> bool:
    return str(document_type or "").startswith(LOCAL_PREFIX)


def local_id(name: object) -> str:
    """Coin a workspace-local type id from an auditor's free text.

    Raises rather than silently sanitizing an unusable name: the auditor is
    naming a type that will appear in stored extractions and approved rules, and
    a name that survives only by mangling is one they should be shown.
    """

    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not slug or not _ID_RE.fullmatch(slug):
        raise DocumentTypeError(f"'{name}' is not a usable document type name.")
    candidate = f"{LOCAL_PREFIX}{slug}"
    if slug in BY_ID:
        raise DocumentTypeError(
            f"'{slug}' is already a global document type; use it directly rather "
            "than coining a local copy."
        )
    return candidate


def validate(document_type: object, *, local_types: Iterable[str] = ()) -> str:
    """Return the id if it is selectable here, else raise.

    ``local_types`` is the workspace's own coined vocabulary. A ``local.`` id the
    workspace has not coined fails closed rather than being accepted on the
    strength of its prefix.
    """

    value = str(document_type or "")
    if is_local(value):
        if not _LOCAL_ID_RE.fullmatch(value):
            raise DocumentTypeError(f"Local document type '{value}' is malformed.")
        if value not in {str(item) for item in local_types}:
            raise DocumentTypeError(
                f"Local document type '{value}' is not defined in this workspace."
            )
        return value
    definition = BY_ID.get(value)
    if definition is None:
        raise DocumentTypeError(f"Unknown document type '{value}'.")
    if not definition.active:
        raise DocumentTypeError(f"Document type '{value}' is withdrawn.")
    return value


def label(document_type: object, *, local_types: Mapping[str, str] | None = None) -> str:
    value = str(document_type or "")
    if is_local(value):
        return (local_types or {}).get(value) or value[len(LOCAL_PREFIX):].replace("_", " ").title()
    definition = BY_ID.get(value)
    return definition.label if definition else value


def catalog() -> list[dict]:
    return [definition.identity() for definition in DEFINITIONS if definition.active]


def prompt_catalog(*, local_types: Iterable[Mapping[str, str]] = ()) -> str:
    """The catalog as classification prompt text, grouped by area.

    The discriminator carries the weight here — a label alone leaves the model
    guessing at neighbours such as ``delivery_note`` against ``goods_receipt``.
    Aliases are listed because documents are titled what they are titled, and a
    "GRN" that finds no alias falls to ``other``.
    """

    by_area: dict[str, list[DocumentTypeDefinition]] = {}
    for definition in DEFINITIONS:
        if definition.active and definition.id != OTHER:
            by_area.setdefault(definition.area, []).append(definition)
    lines: list[str] = []
    for area, area_label in AREAS:
        entries = by_area.get(area) or []
        if not entries:
            continue
        lines.append(f"{area_label}:")
        for definition in entries:
            line = f"  {definition.id} — {definition.discriminator}"
            if definition.aliases:
                line += f" (also titled: {', '.join(definition.aliases)})"
            lines.append(line)
    coined = [dict(item) for item in local_types]
    if coined:
        lines.append("Defined for this engagement:")
        for item in coined:
            identifier = str(item.get("id") or "")
            description = str(item.get("discriminator") or item.get("label") or "")
            lines.append(f"  {identifier} — {description}" if description else f"  {identifier}")
    lines.append(
        f"  {OTHER} — none of the above; supply document_type_other with a short name"
    )
    return "\n".join(lines)


def metadata() -> dict:
    return {
        "areas": [{"id": area, "label": area_label} for area, area_label in AREAS],
        "types": catalog(),
        "local_prefix": LOCAL_PREFIX,
        "other": OTHER,
    }
