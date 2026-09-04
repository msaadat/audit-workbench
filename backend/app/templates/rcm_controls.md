# Control Guidance

<!-- Read together with the RCM template, which governs the risks these
controls answer. This file governs the control alone, and is the only guidance
the control pass is shown. A workspace override of `rcm.md` that still carries
the control rules below is harmless: the risk pass is not asked for a control
and ignores guidance about fields it does not write. -->

The risks are settled. Do not revise, restate or re-rate one, and do not return
any field of a row other than its `row_index` and the control fields below: the
risk, its rating and its process are judgments already made against the
memorandum, and a row you were not asked about is not yours to change.

## Writing the control

Record the control management asserts is in place, as it currently operates.

- Where the planning basis shows no control for a risk, write "No control
  identified" and rate the risk accordingly. Never phrase a recommendation as
  though it were an operating control ("A formal exception procedure defines
  ..."): a control that does not exist cannot be tested, and the design gap is
  a finding, not a row.
- Describe the control's mechanics — who performs it, over what population, how
  often, and what happens when it detects an error. Do not describe how it will
  be tested.

**Never assert a system behaviour the planning basis does not state.** Do not
write that a system enforces, prevents, blocks, validates, restricts, or makes
something impossible unless the planning basis says so in terms. A field
existing in a table is evidence that a value is *recorded*, never evidence that
it is *controlled*, *validated*, or *required* — those are different claims and
a schema cannot support them. Where the basis names a control but not its
mechanism, describe what it is asserted to do and add "mechanism not confirmed
in the planning basis". An unconfirmed mechanism is a testable question; an
invented one leads the engagement to place reliance on a control that may not
exist.

    Write: "The SOP requires the requisitioning department to verify a
            requisition before approval; the requisition record captures a
            verifier and an approver. Whether the system prevents the same
            person from performing both is not confirmed in the planning basis."
    Not:   "ERP workflow requires separate VERIFIED_BY_ID and APPROVED_BY_ID
            fields; system prevents the same user ID from populating both."

**The wording rules for risks apply to the control field too.** No percentages,
null rates, counts, column names, or table names. Do not append a deficiency
clause ("...; but GRN links are missing in 18.64% of invoices", "...; the null
rate suggests gaps"). Whether the control operates is what fieldwork
establishes — a quantified condition in this field pre-concludes it, and a
statistic read from a profile is not a fact about the population. Describe the
control; leave the exceptions to the tests.


## Fields

- **control_type** — exactly `preventive` or `detective`, and nothing else:
  `preventive` where the control stops the error before it occurs, `detective`
  where it identifies the error afterwards. Where the row's control is "No
  control identified", leave this empty — there is no control to classify, and
  a kind named for one that does not exist asserts mechanics the basis never
  described. Validation,
  mandatory-field, and blocking rules are preventive *where the planning basis
  establishes that they operate*; reconciliations, exception reports, and
  after-the-fact reviews are detective. Judge the control's own mechanics, not
  the severity of the risk. Classifying a control as preventive is not a licence
  to assert a system mechanism the basis does not state.
- **criteria** — the clause the control is measured against, **quoted verbatim
  from the supplied basis**, at most about 300 characters. Copy the sentence;
  do not paraphrase it, summarise it, or name the document it came from. Where
  no supplied clause states a criterion for this control, leave the field
  empty — an empty field is the correct answer whenever the basis does not
  supply one, and is never a reason to reach for a nearby sentence.

  Where a `[C…]` marker sits beside the sentence you quoted, you may give it as
  `criteria_hint`. It is a hint and nothing more: the quote is what is matched
  back to its document, so a wrong or absent hint costs nothing and a quote
  that is not verbatim costs the citation.
- **control_owner** — the role accountable for operating the control. **Name a
  role only if that role appears verbatim in the planning basis. If the basis
  names no owner for this control, leave the field empty.** An empty owner is a
  question to put to the client; an invented one is a false attribution that
  survives into the working paper. Never infer an owner from the nature of the
  control — a system-enforced control does not imply an IT or systems
  administration owner unless the basis names one.

`criteria` and `control_owner` are both optional. Leaving either empty is the
correct answer whenever the planning basis does not supply it, and is never a
reason to guess.

The control field is not optional. Where the basis shows no control for the
risk, "No control identified" *is* the answer — but read the basis for one
before concluding it. A control the basis plainly describes and the matrix
reports as absent understates the entity's control environment, points
fieldwork at the wrong thing, and is the harder error for a reviewer to catch.


## Reading supplied table profiles

Table profiles are value-free shape statistics: row counts, distinct counts,
null percentages, minima, and maxima. They do not show what the table contains.

- A null percentage is not an exception rate. Nulls routinely reflect legitimate
  workflow states — a record not yet at that stage, a field that does not apply,
  or a deliberate "no limit" marker.
- A maximum is the largest value present, not a policy ceiling.
- Never state a fact about the population, and never conclude that a control has
  failed, from a profile statistic. Profiles show which processes exist and
  which fields carry them; they are not evidence.


<!-- section: One control per row, against the risk supplied with it. Describe what management asserts; never assert a system mechanism the basis does not state. -->
