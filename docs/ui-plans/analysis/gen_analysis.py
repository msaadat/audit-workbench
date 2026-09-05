#!/usr/bin/env python3
"""Generate the artboards for the Analysis redesign.

Analysis is the last page still on the pre-redesign chrome: a `UiPageHeader`
`h2`, a `SelectButton` for its two screens, five equal buttons in the header,
a rail of two-line cards with a tooltip for a status, and a detail pane whose
first control is a bare title input. Everything it needs already exists.

The two screens borrow from the two pages they most resemble:

  * **Summary** is a written work product with a provenance story, which is the
    audit planning memorandum. It takes `UiDocumentPage` — outline, document
    card on a measure, provenance rail — and `UiMarkdownDocument`.
  * **Procedures** is a register of things that ran and concluded, which is
    Data tests. It takes the 36 px page header, `UiReviewBar`, the 300 px list
    with a dot and a meta line, and `UiVerdictBar`.

The primitives are imported from the two generators that already drew those
pages rather than copied, so a chip here cannot drift from a chip there. The
shell is the 44 px bar from the header redesign.

Every value is a literal hex or px so the markup answers "what size is that"
by itself; the token map in the redesign document says what to write in the
components.

Run from this directory:  python3 gen_analysis.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


V = _load("gen_views", HERE.parent / "sources-planning-reporting" / "gen_views.py")
S = _load("gen_shell", HERE.parent / "shell-and-index" / "gen_shell.py")

# Two glyphs the fieldwork set never needed: this page runs things.
V.ICON.setdefault("play", '<path d="m7 5 12 7-12 7z"></path>')
V.ICON.setdefault("forward", '<path d="m4 6 8 6-8 6z"></path><path d="m13 6 8 6-8 6z"></path>')

C, MONO = V.C, V.MONO
ic, btn, kebab, chip, meter = V.ic, V.btn, V.kebab, V.chip, V.meter
dot, pill, tag_chip, link, sep = V.dot, V.pill, V.tag_chip, V.link, V.sep
eyebrow, mono, section, card = V.eyebrow, V.mono, V.section, V.card
list_row, list_panel, detail_panel = V.list_row, V.list_panel, V.detail_panel
outline, doc_card, doc_p, doc_section = V.outline, V.doc_card, V.doc_p, V.doc_section
side_card, kv_row, para, h3 = V.side_card, V.kv_row, V.para, V.h3
warn_text, bad_text, accent = V.warn_text, V.bad_text, V.accent


# --- The shell, on the 44 px bar -----------------------------------------------
def shell(content: str, height: int) -> str:
    header = S.header(
        [S.crumb_engagement("Procurement"), S.crumb_current("Analysis")],
        [S.hbtn("Assistant", "sparkles", "primary"), S.hkebab()],
    )
    page = (f'<div style="display: flex; flex-direction: column; gap: 12px; '
            f'padding: 16px 24px 24px; flex: 1; min-height: 0;">{content}</div>')
    return V.frame(header + page, height)


def bare(content: str, height: int) -> str:
    """An artboard of parts, with no shell — used for the state studies."""
    return V.frame(f'<div style="display: flex; flex-direction: column; gap: 26px; '
                   f'padding: 24px;">{content}</div>', height)


# --- The page header and its two tabs -------------------------------------------
# No count sentence: the review bar directly below states every count the page
# has, so a sentence beside the title restates numbers the reader is about to
# be shown. Data tests does not carry one either.
COUNT = ""


def page_head(primary: str) -> str:
    """One title, one count sentence, one primary.

    The built header carries five equal buttons — `Analyse with assistant`,
    `Run outstanding`, `Run all`, `Library`, `Code` — of which two run the same
    procedures and two open the same editor. `Run all` moves to the kebab: it
    re-executes work that is already current, which is the rarer half of the
    pair, and the primary already names how much is outstanding.
    """
    return V.page_header("Analysis", COUNT, [
        btn("New procedure", icon="plus", caret=True),
        btn("Analyse with assistant", icon="sparkles"),
        primary,
        kebab(),
    ])


def tab_row(active: str) -> str:
    return V.tabs([
        ("Summary", "", active == "summary"),
        ("Procedures", V.count_badge("23"), active == "procedures"),
    ])


# =============================================================================
# The procedures, as the workspace holds them on 5 September.
#
# Every one is `stale`: the definitions changed after the last run, so the
# built page collapses all 23 into one amber "Rerun required" and the sixteen
# that recorded exceptions vanish from the triage. `tone` here is what each one
# actually found; whether it is current is the separate axis the redesign adds.
# =============================================================================
TONE_OF = {"fail": "bad", "warn": "warn", "ok": "ok"}

PROCEDURES = [
    ("A-8BFCE4A3", "fail", "Invoiced amount exceeds the linked PO total",
     "invoice_data_po_data_joined", "3 of 52 failed"),
    ("A-A69AB394", "fail", "Invoiced amount exceeds the approved requisition estimate",
     "invoice_data_requisitions_joined", "3 of 52 failed"),
    ("A-8C5A30F5", "fail", "Invoice dated before the goods receipt date of its PO",
     "invoice_data_po_data_joined", "4 of 52 failed"),
    ("A-E1DE573C", "fail", "Payment dated before goods received",
     "invoice_data_po_data_joined", "3 of 52 failed"),
    ("A-BCEEDDA8", "fail", "DATE_RECEIVED is before GRN_DATE",
     "invoice_data_po_data_joined", "5 of 52 failed"),
    ("A-46EF1FB8", "fail", "Staff job titles reconcile to no approval master",
     "staff_details", "16 of 20 failed"),
    ("A-A153094C", "fail", "Payment made against an inactive vendor",
     "invoice_data_vendor_master_file_joined", "1 of 52 failed"),
    ("A-9D05207A", "fail", "Payment made against a vendor under review",
     "invoice_data_vendor_master_file_joined", "1 of 52 failed"),
    ("A-BC7CC379", "fail", "Invoice dated after it was received",
     "invoice_data", "2 of 52 failed"),
    ("A-3AD11C0D", "fail", "Invoice dated before the purchase order it references",
     "invoice_data_po_data_joined", "1 of 52 failed"),
    ("A-3E1E3246", "warn", "Vendor invoice number built unlike the governing format",
     "invoice_data", "1 value off-pattern"),
    ("A-B0BC38B1", "ok", "Invoice paid before it was supervisor-approved",
     "invoice_data", "no exceptions · 49 compared"),
    ("A-B8A820A7", "ok", "Vendor invoice number reused across distinct invoices",
     "invoice_data", "no exceptions"),
    ("A-9B094341", "ok", "PO dated before the requisition that authorises it",
     "po_data_requisitions_joined", "no exceptions · 52 compared"),
]


def procedure_row(tone: str, title: str, meta: str, stale: bool, active: bool) -> str:
    """The fieldwork list row, plus one marker.

    Data tests need only a dot and a meta line because a test result is either
    current or it is not run. A saved procedure has a third state — a result
    that stands but was recorded against a definition that has since changed —
    and it is orthogonal to what the result *said*. So the dot and the line
    carry the finding, exactly as next door, and staleness is a glyph at the
    end of the row rather than a word competing for the meta line, which the
    joined table names have already used up.
    """
    bg = (f"background: {C['teal_soft']}; border-left: 3px solid {C['teal']};" if active
          else f"border-left: 3px solid transparent; border-top: 1px solid {C['border']};")
    marker = (f'<span title="Rerun required" style="display: inline-flex; flex: 0 0 auto; '
              f'color: {C["warn"]};">{ic("refresh", 13)}</span>') if stale else ""
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 10px 12px; {bg}">'
            f'{dot(tone)}'
            f'<div style="display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1;">'
            f'<span class="ell" style="color: {C["ink_strong"] if active else C["ink"]}; '
            f'font-size: 13px; font-weight: {600 if active else 500};">{title}</span>'
            f'<span class="ell num" style="color: {C["muted"]}; font-size: 11.5px;">{meta}</span>'
            f'</div>{marker}</div>')


def procedure_rows(selected: str = "A-8BFCE4A3") -> str:
    """The list, in outcome order — what failed, then what wants a look."""
    out = []
    for pid, verdict, title, table, found in PROCEDURES:
        # The outcome leads: a joined frame's name is 38 characters and would
        # push the one fact that ranks the row off the end of the line.
        out.append(procedure_row(TONE_OF[verdict], title, f'{found} · {table}',
                                 stale=True, active=pid == selected))
    return "".join(out)


def procedures_list(selected: str = "A-8BFCE4A3") -> str:
    return list_panel(
        V.list_header("Search procedures and tables", [])
        + procedure_rows(selected)
    )


def review() -> str:
    chips = [
        chip(23, "All procedures", pressed=True),
        chip(16, "Exceptions", "bad"),
        chip(1, "Need review", "warn"),
        chip(6, "No exception", "ok"),
        chip(23, "Rerun required", "warn"),
    ]
    meters = [
        meter("Run", "23/23", [("ok", 100)]),
        # The meter that is missing today: everything has run, nothing is
        # current, and those are different questions.
        meter("Current", "0/23", []),
        # `promotion` is durable on every analysis — promoted to a test, or
        # declined with a reason — and never leaves the backend. In this
        # engagement it is not empty: seventeen procedures hold exceptions and
        # all seventeen were answered, eleven carried into tests and six
        # declined with recorded reasoning. None of it reaches the page.
        meter("Answered", "17/17", [("ok", 65), ("neutral", 35)]),
    ]
    # No settle button: running what is outstanding is the page's primary, and
    # the built page offers it twice already.
    return V.review_bar(chips, meters)


# --- The open procedure ---------------------------------------------------------
EXCEPTION_COLS = ["INVOICE_ID", "INVOICE_DATE", "VENDOR_ID", "PO_NUMBER_LINK",
                  "INVOICE_AMOUNT", "PO_TOTAL_AMOUNT"]
EXCEPTION_ROWS = [
    ["INV2024106", "2024-02-17", "V1004", "PO2024106", "43,200,000", "32,000,000"],
    ["INV2024122", "2024-05-19", "V1025", "PO2024122", "247,800", "210,000"],
    ["INV2024140", "2025-01-15", "V1004", "PO2024140", "2,256,000", "940,000"],
]


def result_table(columns: list[str], rows: list[list[str]], numeric_from: int = 4,
                 cols: str | None = None) -> str:
    head = "".join(
        f'<span style="padding: 7px 12px; color: {C["muted"]}; font-family: {MONO}; font-size: 10.5px; '
        f'font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; '
        f'text-align: {"right" if i >= numeric_from else "left"};">{col}</span>'
        for i, col in enumerate(columns))
    body = ""
    for row in rows:
        cells = "".join(
            f'<span class="num" style="padding: 8px 12px; border-top: 1px solid {C["border"]}; '
            f'color: {C["ink"] if i >= numeric_from else C["ink_strong"]}; font-size: 12.5px; '
            f'font-family: {MONO if i >= numeric_from or i == 0 else "inherit"}; '
            f'text-align: {"right" if i >= numeric_from else "left"};">{cell}</span>'
            for i, cell in enumerate(row))
        body += cells
    template = cols or (f"repeat({numeric_from}, auto) "
                        f"repeat({len(columns) - numeric_from}, minmax(0, 1fr))")
    return (f'<div style="display: grid; grid-template-columns: {template}; border: 1px solid {C["border"]}; border-radius: 8px; '
            f'background: {C["panel"]}; overflow: hidden;">{head}{body}</div>')


def definition_card() -> str:
    """What the procedure tests, stated rather than folded into an accordion."""
    body = (f'<div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">'
            f'{pill("compare_columns", "info")}'
            f'<span style="font-family: {MONO}; font-size: 12.8px; color: {C["ink_strong"]};">'
            f'INVOICE_AMOUNT &le; PO_TOTAL_AMOUNT</span>'
            f'<span style="color: {C["muted"]}; font-size: 12.8px;">over</span>'
            f'{tag_chip("invoice_data_po_data_joined", icon="table")}'
            f'<span style="flex: 1;"></span>{link("Edit definition", C["teal"], "settings")}</div>')
    return card(body, pad="10px 14px")


def verdict(tone: str, found: str, recorded: str, stale: str = "",
            actions: list[str] | None = None) -> str:
    return V.verdict_bar(tone, found, recorded,
                         actions if actions is not None else [
                             btn("Read as exceptions", caret=True),
                             btn("Run", "primary", icon="play"),
                         ],
                         stale=stale)


STALE_LINE = ("The definition or its source data changed after this result was recorded, "
              "so what is above is what the procedure found on 1 September, not what it "
              "would find now.")


def procedure_detail(narrow: bool = False) -> str:
    head = V.detail_header(
        f'{mono("A-8BFCE4A3", 11.5)} {sep()} Assistant test {sep()} '
        f'invoice_data_po_data_joined',
        "Invoiced amount exceeds the linked PO total",
        "Three invoices exceed the PO_TOTAL_AMOUNT of the PO they reference, "
        "indicating overbilling or a broken three-way match.",
        [btn("Export", icon="download"), kebab()],
    )
    bar = verdict(
        "bad",
        f'<span class="num">3 of 52 rows failed</span> '
        f'<span class="num" style="color: {C["danger"]};">5.8%</span> '
        f'<span class="num" style="color: {C["muted"]}; font-weight: 500;">'
        f'· run 1 Sep, 17:16 · 43,200,000 the largest breach</span>',
        f'Returned rows are read as <b>exceptions</b>. Recorded by an unattended run. '
        f'<span style="color: {C["warn_ink"]};">No auditor has read it.</span>',
        stale=STALE_LINE,
    )
    rows = section(
        "Exception rows · 3 of 52",
        result_table(EXCEPTION_COLS, EXCEPTION_ROWS)
        + f'<p style="margin: 6px 0 0; color: {C["muted"]}; font-size: 12px;">'
          f'Open a row for the other 19 fields of the record.</p>',
        right=link("Export the full result", C["teal"], "download"),
    )
    # The row that closes the record, as the data test's `Finding` row does.
    # An exploratory procedure is computed outside the audit graph: until it is
    # promoted to a test against an RCM row, what it found can support no
    # finding, and 43,200,000 expires where it was found.
    footer = V.footer_row([
        f'{eyebrow("Cited by")}{link("Analysis summary · 1. Invoices billed above approved amounts", C["teal"], "file")}',
        f'{eyebrow("Answered for")}'
        f'<span style="color: {C["warn_ink"]}; font-size: 12.8px;">Not yet.</span>'
        f'<span style="flex: 1;"></span>{btn("Promote to a test", icon="shield")}',
    ])
    return detail_panel(head + bar + section("What it tests", definition_card()) + rows + footer)


def artboard_procedures() -> str:
    body = page_head(btn("Run 23 outstanding", "primary", icon="play")) + tab_row("procedures") \
        + review() + V.master_detail(procedures_list(), procedure_detail())
    return shell(body, 1080)


# =============================================================================
# Summary — the memorandum treatment
# =============================================================================
MEMO_SECTIONS = [
    "Data received and its limitations",
    "What the analysis found",
    "1. Invoices billed above approved amounts",
    "2. Payments to vendors not eligible",
    "3. Three-way match sequence broken",
    "4. Staff job titles reconcile to no master",
    "5. Invoice dated before its PO",
    "6. Invoices dated after receipt",
    "7. Vendor invoice number off-pattern",
    "How far these results can be relied on",
    "Further work required",
]


def summary_outline() -> str:
    entries = []
    for i, text in enumerate(MEMO_SECTIONS):
        indent = text[0].isdigit()
        marker = dot("bad", 7) if text.startswith(("1.", "2.", "3.", "4.")) else \
            dot("warn", 7) if text.startswith(("5.", "6.", "7.")) else ""
        label = f'<span style="padding-left: {12 if indent else 0}px;">{text}</span>'
        entries.append((label, i == 2, marker))
    return (f'<div style="display: flex; flex-direction: column; gap: 2px; align-self: start; '
            f'position: sticky; top: 20px;">{outline("On this summary", entries)}</div>')


def source_table_rows() -> str:
    rows = [("financial_approval_matrix", "4", "2", "—"),
            ("invoice_data", "52", "15", "2024-01-27 to 2025-02-20"),
            ("po_data", "52", "11", "2024-01-18 to 2025-01-18"),
            ("requisitions", "55", "14", "2024-01-08 to 2024-12-28"),
            ("staff_details", "20", "4", "—"),
            ("vendor_master_file", "12", "3", "—")]
    return result_table(["Source table", "Records", "Columns", "Period covered"],
                        [list(r) for r in rows], numeric_from=1,
                        cols="minmax(0, 1fr) auto auto 13rem")


def cited_result(pid: str, title: str, caption: str) -> str:
    """An embedded result, drawn as a citation rather than as a loose table.

    The memo's `embed` fences put a bare chart or table between two paragraphs
    with nothing saying which procedure produced it. A citation carries its id,
    what it concluded, and the way back to the procedure that concluded it.
    """
    head = (f'<div style="display: flex; align-items: center; gap: 8px; padding: 8px 12px; '
            f'border-bottom: 1px solid {C["border"]}; background: {C["raised"]};">{dot("bad")}'
            f'{mono(pid, 11.5, C["ink_strong"])}'
            f'<span style="color: {C["ink"]}; font-size: 12.8px; font-weight: 600;">{title}</span>'
            f'<span style="flex: 1;"></span>'
            f'<span class="num" style="color: {C["danger_ink"]}; font-size: 12px;">{caption}</span>'
            f'{V.chevron_link("Open")}</div>')
    table = result_table(EXCEPTION_COLS, EXCEPTION_ROWS)
    return (f'<div style="border: 1px solid {C["border"]}; border-radius: 10px; overflow: hidden; '
            f'background: {C["panel"]};">{head}'
            f'<div style="padding: 10px 12px;">{table}</div></div>')


def summary_document() -> str:
    lead = doc_p(
        "The population is small — 52 invoices, 52 POs, 55 requisitions, 12 vendors — and the "
        "analysis found three paid invoices billed above both their PO total and their approved "
        "requisition estimate, the largest being INV2024106 at 43,200,000 against a 32,000,000 PO; "
        "two paid invoices against vendors whose master status is not Active; and repeated breaks "
        "in the goods-then-invoice-then-payment sequence. The most consequential control gap is "
        "that 16 of 20 staff job titles match no approval-authority master, so approval limits "
        "could not be tested. Segregation-of-duties and duplicate-invoice checks were clean.")
    limits = doc_section(
        "Data received and its limitations",
        doc_p("Six tables were received. Every join matched at 1.0 with no row multiplication, so "
              "per-row counts are not inflated. The vendor master and approval matrix are too small "
              "to carry a meaningful rate, and the approval matrix carries no procedure at all. "
              "Three invoices lack a supervisor approval, so the two supervisor-approval tests "
              "cover 49 of 52 rows."),
        source_table_rows())
    found = doc_section(
        "What the analysis found",
        f'<h4 style="margin: 10px 0 0; color: {C["ink_strong"]}; font-size: 14px; font-weight: 600;">'
        f'1. Invoices billed above approved amounts</h4>',
        doc_p("Three invoices exceed both the linked PO total and the approved requisition estimate, "
              "and the same three rows breach both tests. INV2024106 bills 43,200,000 against a "
              "32,000,000 PO and estimate; INV2024140 bills 2,256,000 against 940,000; INV2024122 "
              "bills 247,800 against 210,000. All three are marked Paid. This is the largest "
              "monetary exposure in the population and points to overbilling or a broken "
              "three-way match."),
        cited_result("A-8BFCE4A3", "Invoiced amount exceeds the linked PO total",
                     "3 of 52 breach INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT"),
        f'<h4 style="margin: 14px 0 0; color: {C["ink_strong"]}; font-size: 14px; font-weight: 600;">'
        f'2. Payments made to vendors not eligible for payment</h4>',
        doc_p("Two paid invoices were raised against vendors whose master status is not Active: "
              "INV2024126 (5,250,000) to RapidBuild Constructors, status Under Review, and "
              "INV2024150 (950,000) to Express Courier Services, status Inactive. These are the "
              "only two non-Active vendors in the 12-row master, and the same statuses recur "
              "across the PO and requisition frames."))
    # No title line: unlike the memorandum, the memo the backend writes opens on
    # its lead paragraph and its first heading is `## Data received`. The
    # eyebrow is the identity, as `UiMarkdownDocument` already draws it.
    return doc_card(eyebrow("Analysis summary · Procurement") + lead + limits + found)


def cited_row(pid: str, tone: str, title: str, found: str) -> str:
    return (f'<div style="display: flex; align-items: flex-start; gap: 8px; padding: 6px 0; '
            f'border-top: 1px solid {C["border"]};"><span style="padding-top: 4px;">{dot(tone, 7)}</span>'
            f'<div style="display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1;">'
            f'<span class="ell" style="color: {C["ink"]}; font-size: 12.5px; font-weight: 500;">{title}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 11px;">{mono(pid, 10.5)} · {found}</span>'
            f'</div>{V.chevron_link("")}</div>')


def summary_rail() -> str:
    cited = (cited_row("A-8BFCE4A3", "bad", "Invoiced amount exceeds the linked PO total", "3 of 52 failed")
             + cited_row("A-A69AB394", "bad", "Invoiced amount exceeds the requisition estimate", "3 of 52 failed")
             + cited_row("A-46EF1FB8", "bad", "Staff job titles reconcile to no master", "16 of 20 failed")
             + f'<div style="display: flex; flex-direction: column; gap: 4px; padding-top: 6px;">'
               f'{V.chevron_link("18 more cited results")}</div>'
             + f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px; padding-top: 6px; '
               f'border-top: 1px solid {C["border"]};">Over 6 source tables · 195 rows · '
               f'every join matched at 1.0</span>')
    uncited = (f'<div style="display: flex; flex-direction: column; gap: 6px;">'
               f'{cited_row("A-9B094341", "ok", "PO dated before the requisition that authorises it", "no exceptions")}'
               f'{cited_row("A-3374422C", "ok", "PO total exceeds the requisition estimate", "no exceptions")}'
               f'<span style="color: {C["muted"]}; font-size: 11.5px; line-height: 1.45;">'
               f'Both found nothing, so neither changes what the summary says — but a procedure '
               f'that found something and is not cited would.</span></div>')
    written = (kv_row("Written", "assistant · 1 Sep 17:17")
               + kv_row("Model", "deepseek-v4-flash-0731", mono_value=True)
               + kv_row("Calls", "1 · 21 results read")
               + kv_row("Committed", "revision 118", mono_value=True))
    feeds = (f'<div style="display: flex; flex-direction: column; gap: 6px;">'
             f'<a href="#" style="display: flex; align-items: center; gap: 8px; padding: 8px 10px; '
             f'border: 1px solid {C["border"]}; border-radius: 8px; background: {C["panel"]};">'
             f'{ic("flag", 13, C["teal"])}'
             f'<span style="display: flex; flex-direction: column; gap: 1px; flex: 1;">'
             f'<span style="color: {C["ink_strong"]}; font-size: 12.8px; font-weight: 600;">'
             f'18 findings in this engagement</span>'
             f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">'
             f'none cites a procedure; all 18 cite a data test</span></span>'
             f'{ic("chev_right", 13, C["muted"])}</a>'
             f'<span style="color: {C["muted"]}; font-size: 11.5px; line-height: 1.45;">'
             f'A finding can take a procedure as evidence. Where the analysis found what a '
             f'test would have found, the exception is written up twice or not at all.</span></div>')
    return (f'<div style="display: flex; flex-direction: column; gap: 12px;">'
            + side_card("Results it cites", cited, "21 of 23")
            + side_card("Not cited", uncited, "2")
            + side_card("Written", written)
            + side_card("What this feeds", feeds)
            + "</div>")


def artboard_summary() -> str:
    head = page_head(btn("Regenerate", "primary", icon="sparkles")) + tab_row("summary")
    bar = V.verdict_bar(
        "ok",
        f'Written by the assistant 1 Sep 17:17 '
        f'<span class="num" style="color: {C["muted"]}; font-size: 12.8px; font-weight: 500;">'
        f'· 1,363 words · 4 sections, 7 numbered findings · cites 21 of 23 procedures</span>',
        "It describes the results recorded on 1 September. Two procedures are not cited; both "
        "found nothing.",
        [btn("Export", icon="download"), btn("Edit", icon="pencil")],
        stale="All 23 procedures need a rerun. Run them, then regenerate, or this describes "
              "results that no longer stand.",
    )
    body = (f'<div style="display: grid; grid-template-columns: 220px minmax(0, 1fr) 300px; '
            f'gap: 28px; align-items: start; padding-top: 6px;">'
            f'{summary_outline()}{summary_document()}{summary_rail()}</div>')
    return shell(head + bar + body, 1470)


# =============================================================================
# The states of one procedure
# =============================================================================
def labelled(label: str, note: str, body: str) -> str:
    return (f'<div style="display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 24px; '
            f'align-items: start;">'
            f'<div style="display: flex; flex-direction: column; gap: 4px;">'
            f'<span style="color: {C["ink_strong"]}; font-size: 13px; font-weight: 700;">{label}</span>'
            f'<span style="color: {C["muted"]}; font-size: 12px; line-height: 1.5;">{note}</span></div>'
            f'<div style="display: flex; flex-direction: column; gap: 12px; min-width: 0;">{body}</div>'
            f'</div>')


def artboard_states() -> str:
    rows = [
        labelled(
            "Found exceptions, and current",
            "What the verdict bar says when the result stands: what it found, at what rate, "
            "and how the returned rows are read.",
            verdict("bad",
                    f'<span class="num">3 of 52 rows failed</span> '
                    f'<span class="num" style="color: {C["danger"]};">5.8%</span>'
                    f'<span class="num" style="color: {C["muted"]}; font-weight: 500;"> · run 5 Sep, 09:12</span>',
                    "Returned rows are read as <b>exceptions</b>. Recorded by an unattended run. "
                    "<span style=\"color: #8a4308;\">No auditor has read it.</span>")),
        labelled(
            "Found exceptions, rerun required",
            "The state all 23 procedures are actually in. Today this replaces the verdict with "
            "amber \"Rerun required\" and the three failures disappear from the page; here the "
            "result stands and staleness is the strip under it.",
            verdict("bad",
                    f'<span class="num">3 of 52 rows failed</span> '
                    f'<span class="num" style="color: {C["danger"]};">5.8%</span>'
                    f'<span class="num" style="color: {C["muted"]}; font-weight: 500;"> · run 1 Sep, 17:16</span>',
                    "Returned rows are read as <b>exceptions</b>. Recorded by an unattended run.",
                    stale=STALE_LINE)),
        labelled(
            "Found nothing",
            "A procedure that ran and cleared says what it covered, because \"no exceptions\" "
            "over 49 of 52 rows is a different statement from \"no exceptions\".",
            verdict("ok",
                    f'<span class="num">No exceptions · 49 of 52 rows compared</span>'
                    f'<span class="num" style="color: {C["muted"]}; font-weight: 500;"> · run 1 Sep, 17:16</span>',
                    "Three invoices carry no supervisor approval and were not comparable.",
                    stale=STALE_LINE)),
        labelled(
            "Worth a look, short of an exception",
            "The format anomaly. Returned rows are read as informational, so the row is amber "
            "rather than red and it never counts as coverage.",
            verdict("warn",
                    f'<span class="num">1 value in a minority format</span>'
                    f'<span class="num" style="color: {C["muted"]}; font-weight: 500;"> · '
                    f'dominant A{{4}}9{{3}}-9{{4}} at 98.1% · run 1 Sep, 17:16</span>',
                    "Returned rows are read as <b>informational</b>. Nothing is concluded from them.")),
        labelled(
            "Could not run",
            "The definition is broken. The error is the verdict, and the action is the one that "
            "fixes it — not a Run that will fail again.",
            V.verdict_bar("bad",
                          "Could not run · <span style=\"font-family: 'JetBrains Mono', monospace; "
                          "font-size: 12.8px;\">ColumnNotFoundError: GRN_DATE</span>",
                          "The last result that stands is from 28 August, before the column was renamed.",
                          [btn("Edit definition", "primary", icon="settings")])),
        labelled(
            "Never run",
            "No result, so nothing is claimed. The row is grey in the list and the primary is Run.",
            V.verdict_bar("neutral", "This procedure has not been run yet.",
                          "Nothing is recorded against it, so it counts towards no coverage.",
                          [btn("Run", "primary", icon="play")])),
    ]
    body = "".join(rows)
    return bare(body, 440)


# =============================================================================
# Empty
# =============================================================================
def empty_state(icon: str, title: str, description: str, actions: list[str]) -> str:
    return (f'<div style="display: flex; flex-direction: column; align-items: center; gap: 10px; '
            f'padding: 44px 24px; border: 1px solid {C["border"]}; border-radius: 12px; '
            f'background: {C["panel"]}; text-align: center;">'
            f'<span style="display: grid; place-items: center; width: 42px; height: 42px; '
            f'border-radius: 50%; background: {C["teal_soft"]}; color: {C["teal"]};">{ic(icon, 20)}</span>'
            f'<span style="color: {C["ink_strong"]}; font-size: 15.2px; font-weight: 700;">{title}</span>'
            f'<span style="max-width: 460px; color: {C["muted"]}; font-size: 13px; line-height: 1.55;">{description}</span>'
            f'<div style="display: flex; gap: 8px; padding-top: 6px;">{" ".join(actions)}</div></div>')


def artboard_empty() -> str:
    summary_empty = labelled(
        "Summary, before it is written",
        "The procedures have run and concluded; nothing has written them up.",
        page_head(btn("Write the summary", "primary", icon="sparkles")) + tab_row("summary")
        + empty_state(
            "file",
            "No analysis summary yet",
            "The procedures have run. The assistant can write up what they found — the "
            "population, the exceptions, and the work still outstanding — and cite each result "
            "where it uses it.",
            [btn("Write the summary", "primary", icon="sparkles"),
             btn("Open the procedures", icon="chart")]))
    nothing = labelled(
        "Before anything has run",
        "One empty state, not two: with no procedures there is nothing for a summary to "
        "summarise, so the tabs do not appear.",
        V.page_header("Analysis", "", [
            btn("New procedure", icon="plus", caret=True),
            btn("Analyse with assistant", "primary", icon="sparkles"),
            kebab(),
        ])
        + empty_state(
            "chart",
            "Analyse this engagement's data",
            "A saved procedure is a rerunnable spec: pick a predefined audit test, write Polars "
            "yourself, or let the assistant propose procedures for the imported tables.",
            [btn("Analyse with assistant", "primary", icon="sparkles"),
             btn("Library test", icon="book"), btn("Custom code", icon="code")]))
    return bare(summary_empty + nothing, 460)


ARTBOARDS = [
    ("Main.dc.html", "Summary — the memorandum treatment", artboard_summary, 0, 0, 1440, 1470),
    ("Procedures.dc.html", "Procedures — the fieldwork treatment", artboard_procedures, 1560, 0, 1440, 1080),
    ("ProcedureStates.dc.html", "What one procedure can say", artboard_states, 0, 1590, 1440, 440),
    ("Empty.dc.html", "Empty states", artboard_empty, 1560, 1200, 1440, 460),
]


def main() -> None:
    for file, _title, fn, *_ in ARTBOARDS:
        (HERE / file).write_text(fn(), encoding="utf-8")
    manifest = {
        "artboards": [
            {"file": f, "title": t, "x": x, "y": y, "w": w, "h": h}
            for f, t, _fn, x, y, w, h in ARTBOARDS
        ],
        "annotations": [
            {"id": "note-two-axes", "x": 3160, "y": 0, "w": 340,
             "text": "The one change that is not a restyle: what a procedure found and whether it is current are two axes, not one. All 23 procedures in this workspace are stale, so the built page shows a single amber \"Rerun required\" chip and the sixteen that recorded exceptions vanish. Here the dot, the chip and the verdict bar carry the finding; staleness is a separate chip, a Current meter, and the strip under the verdict — exactly as a data test carries its own."},
            {"id": "note-answered", "x": 3160, "y": 420, "w": 340,
             "text": "The second thing the page could not say. `promotion` — carried into a data test against an RCM row, or declined with a reason — has been durable on every record since the promotion capability landed, and never left the backend. In this engagement it is not empty: 17 procedures hold exceptions and all 17 were answered, 11 carried into tests and 6 declined with a paragraph of reasoning each. The Answered meter and the footer row are the first surfaces to show any of it."},
            {"id": "note-summary", "x": 0, "y": 1360, "w": 340,
             "text": "The summary is the memorandum's page, element for element: outline, document on a 96ch measure, provenance rail. The one addition is the citation card — the memo's embed fences currently drop a bare table between two paragraphs with nothing saying which procedure produced it."},
        ],
        "launch": {"view": "canvas"},
    }
    (HERE / "canvas.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(ARTBOARDS)} artboards and canvas.json to {HERE}")


if __name__ == "__main__":
    main()
