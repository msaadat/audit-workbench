<!-- The `##` headings below define what a finding is. Completeness is checked
against them: every heading must carry text before an auditor can confirm the
finding for formal reporting. Rename, add, or remove headings to match your
firm's methodology and the gate follows. The one exception is the root-cause
section, which may carry the deferral note in place of a cause when the finding
is marked "root cause pending auditor follow-up".

The finding is written as Markdown, not as a JSON object, so tables, paragraphs
and lists in the sections below are ordinary Markdown and travel into the report
as written. The `#` title line and the severity line are the finding's typed
spine: they are read off the draft and stored as fields, and do not form part of
the narrative that is copied into the report.

Write the narrative so it can be lifted into the report unchanged. It is copied
verbatim into "Findings and recommendations" under the finding's title, so the
prose must read as final report text, not as a working note. That means:

- No first person, no addressing the reader, no "we tested" or "the auditor
  observed". State what is, in the engagement's past tense.
- No references to the workbench: no test ids, run ids, table names, column
  names, file names, or workspace mechanics. The finding's linked test and
  evidence anchors already carry that traceability; the narrative carries the
  audit point.
- No commentary about the drafting itself ("further work is needed to
  determine..."), no hedging that a reviewer would have to delete.
- Quantify the condition from the recorded result. A number stated here must be
  the number the linked execution result holds; the report's arithmetic checks
  compare them.
- Do not restate the same fact in more than one section. Condition is what
  happened, Root Cause is why, Risk is what it exposes. A sentence that fits two
  sections belongs in one.
-->

# {{title}}

<!-- title: The audit point in one line, as it will head the finding in the
report. Name what is wrong, not what was tested.

    Write: "Payments approved by the officer who created them"
    Not:   "Segregation of duties test — 37 exceptions"
    Not:   "DAT-14 exception review" -->

**Severity:** {{severity}}

<!-- severity: Exactly one of critical, high, medium, low, info, judged on the
exposure the condition creates for the entity — not on the number of exceptions.
This line is stored as the finding's severity field; it orders the report,
counts into the severity summary, and constrains the overall conclusion, so it
is a judgement rather than a label. -->

## Condition

<!-- section: What was found, quantified, in the past tense. State the population
examined, the number of exceptions, and what distinguishes them. This is the
only section that carries detail; it should stand on its own as the factual
record of the exception.

    Write: "Of the 1,284 payments released in the period, 37 payments totalling
            AED 2.1 million were approved by the same officer who created the
            payment instruction."
    Not:   "DAT-14 returned 37 exception rows where CREATED_BY = APPROVED_BY."

**Identify the exceptions; do not merely count them.** A finding that says "1
exception was identified" gives management nothing to act on.

- Where the finding rests on a document, name the document.

      Write: "The Procurement Standard Operating Procedure extract does not
              define how vendor documentation is retained."
      Not:   "The supplied documentation did not establish the requirement."

- Where the exceptions are few, set them out as a Markdown table directly under
  this prose. Use only the columns that evidence the exception — the record
  identifier and the fields the test compared — and give each a readable
  heading rather than the underlying column name. The table travels into the
  report with the rest of this section.

      | Invoice | Invoice date | Payment date | Amount (AED) |
      | --- | --- | --- | --- |
      | INV2024008 | 20 Dec 2024 | 29 Nov 2024 | 24,939,790 |

- Where the exceptions are many, describe the pattern, quantify it, and name a
  few examples by identifier rather than tabling all of them.
- Where only part of the exception population was available, say so and give
  the full count. A table showing the first rows of a larger population must
  never read as though it were the whole of it.

Do not conclude here. Whether the control failed belongs to the Risk section and
to the test's conclusion; this section records what was observed. -->

## Criteria

<!-- section: The requirement the condition is measured against, and where it
comes from. Cite the specific policy clause, delegation matrix, contract term,
SOP section, or regulation. Where the engagement's planning basis names no
written criterion, say which control management asserted and note that no
written criterion was located — an unstated criterion is a question for
management, never an invented citation.

    Write: "Section 6.3 of the Payments SOP requires payment creation and
            payment approval to be performed by different officers."
    Not:   "Good practice requires segregation of duties." -->

## Root Cause

<!-- section: Why it happened, in one short phrase. Not a paragraph, not a
narrative — the underlying weakness, named.

    Write: "Lack of segregation of duties in the payment release workflow."
    Write: "No system limit enforcing the delegation of authority matrix."
    Not:   "It appears that because the department was short-staffed during the
            period under review, and the SOP was not updated after the 2023
            reorganisation, officers were required to..."

Where the cause is not established by the evidence, do not guess. Write the
deferral note and nothing else:

    _Root cause pending auditor follow-up._

That note marks the finding's cause as pending, and is the only accepted stand-in
for a cause. An asserted cause the fieldwork does not support is the most common
reason a finding is challenged; a section left blank is not a deferral, it is an
omission, and it is rejected as one. -->

## Risk

<!-- section: What the condition exposes the entity to, in one short phrase.
The consequence, not a restatement of the condition.

    Write: "Financial loss due to unauthorized payments."
    Write: "Understated liabilities in the reported period."
    Not:   "There is a risk that payments may be approved by the same person who
            created them, which is a risk because segregation of duties is a
            fundamental control..."

Do not quantify a loss the evidence does not support. The exposure is the point;
an invented figure is not. -->

## Recommendation

<!-- section: What management should do, addressed to management, specific
enough to be actionable and to be verified on follow-up. One recommendation per
finding; where the remedy has genuinely distinct parts, use a short list.

    Write: "Configure the payment system to reject an approval submitted by the
            officer who created the payment instruction, and reperform a review
            of payments released since the start of the period."
    Not:   "Management should strengthen controls over payments."

Recommend the control, not the tool. Do not name a vendor, product, or module
unless the entity already operates it. -->
