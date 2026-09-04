# Control Attribute Guidance

<!-- Read together with the RCM template, which governs everything else about a
row. This file governs the attributes alone, and is the only guidance the
attribute pass is shown. A workspace override of `rcm.md` that still contains
the attribute rules below is harmless: the rows pass is not asked for
attributes and ignores guidance about fields it does not write. -->

The rows are settled. Do not revise a risk, a control, a rating, a process, or
any other field: you are given them so the attributes can be written against
them, and changing one would silently reopen a judgment already made.

## Fields

- **control_attributes** — the distinct requirements of the asserted control.
  Each attribute has a unique stable `key`, one assertion from `Existence`,
  `Completeness`, `Accuracy`, `Authorization`, `Valuation`, `Cut-off`,
  `Compliance`, or `Operational`, a plain-language `requirement`, and one
  evidence strategy — and nothing else. Enumerate the requirements the control
  actually makes rather than collapsing every control to a single attribute; a
  three-way match before payment asserts the match, the receipt-before-payment
  order, and the amount agreement separately. Keep all attributes of one
  risk/control on the same RCM row.

  An attribute whose evidence strategy is `transaction_cycle` says so and stops
  there: which fields must agree is decided later, against this engagement's
  own documents, and is not written here. Deciding *that* a requirement needs
  linked source records is the judgment this matrix makes; naming the fields
  belongs to the turn that has read the documents, which this one has not.
- **evidence_kind** — where the evidence for that requirement lives, judged from
  the supplied material rather than the requirement's wording. Use
  `tabular_population` whenever the imported tables carry the fields named —
  uniqueness, missing values, thresholds, date ordering, status combinations, or
  one column compared with another — because that reaches the whole population.
  Reserve `transaction_cycle` for requirements that genuinely need several
  linked *documents* of different registered record kinds; it reaches only the
  transactions that have uploaded documents, so it can never support a
  population-level conclusion. `document_content` is for a fact one document
  states. `manual_inspection`, `inquiry`, and `mixed` are for requirements no
  imported evidence answers — never for something a supplied table can measure.
  Only `transaction_cycle` carries cycle vocabulary.

## Choosing the evidence strategy

You are shown the tables this engagement imported and the record kinds it
holds, by name only. That is deliberate: the strategy is a question about where
the answer lives, and it is answerable from what exists without reading a
single value.

- A row whose control field says "No control identified" still chooses an
  evidence strategy from the supplied material. Where the imported tables carry
  the fields the requirement names, that is `tabular_population` regardless of
  whether a control is asserted: testing the population is how the absence of
  the control is evidenced, and it is the only evidence there is.
- Reserve `inquiry` for a requirement no supplied table and no supplied record
  kind can answer. A requirement measurable from a column is never inquiry.
- Prefer `tabular_population` for population-level completeness, uniqueness,
  threshold, and status tests. For agreement between distinct source records,
  keep a `transaction_cycle` attribute where source-record vouching is what the
  requirement asks for, and add a separate `tabular_population` attribute where
  a table can also provide broader population assurance.

<!-- section: Every supplied row gets one entry, carrying its exact row_index. Enumerate the requirements; do not collapse a control to one attribute out of habit. -->
