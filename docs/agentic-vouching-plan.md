# Agentic vouching: what's left

Phases 0, 4a and 4b.1 of this plan have shipped. The corrected contract and
the build record for all three now live in `docs/dynamic-cycle-contracts.md` —
Phase 0's correction is in that document's opening section, and *Phase 4a
notes* / *Phase 4b.1 notes* (after *Phase 11 notes*) carry what the builds
decided and corrected. This document keeps only what those phases made
possible and did not themselves do.

Two rules from `docs/dynamic-cycle-contracts.md`'s original draft were
withdrawn to get here:

| Withdrawn | Replaced by | State |
| --- | --- | --- |
| "An LLM never decides an item's outcome" | A model reader reaches verdicts; the auditor reviews and overrides | Verdicts shipped (Phase 10); the per-verdict override has not |
| "Evaluation stays code" | A model evaluator judges agreement on raw values | Shipped (Phase 10) |

Neither withdrawal is exercised by anything below — the verdict slot was
already filled before this document existed, by a change the first real
engagement forced. What remains of both as open work is the auditor's
per-verdict override (under *Judge*, below) and the direction they point the
rest of this document in: a search that binds documents and a reader that
judges what it found, in place of a declared key and an operator chosen
before the evidence was read.

## Scope

The read half — 4a, 4b.1 — landed against the pipeline
`docs/dynamic-cycle-contracts.md` already describes: approved join keys, code
resolution, the ruleset review screen and the staleness family, all retained
and all still doing their work. What remains of that half is the coined type
for uncatalogued evidence (4b.2) and the late-field sweep (4c).

**Neither withdrawal above is exercised by 4b.2 or 4c.** Nothing in them puts
a model in the verdict slot or replaces a join with a search — that is
assembly, deferred further down this document.

The reason 4b.2/4c and assembly are different tranches is that they fail for
different reasons. The 44-field `fx_contract` and the split `approved_by_id`
— both already repaired by 4b.1's accumulating master — were failures of a
vocabulary frozen before the evidence. The broker notes and the nostro
statements are failures of a join that cannot address the field the
reference was printed into, and no amount of schema work reaches them.
Assembly does, and it is deferred with the rest of this document.

The sequencing warning that assembly must wait on assembler statistics (see
*The thing that must not be lost*) does not bind 4b.2 or 4c. It is about
removing the schema, whose only compensating control is those statistics.
4b.2 and 4c keep the schema and keep join-key fan-out, which is the loud
failure signal that warning exists to protect.

## The evidence this rests on

One treasury engagement, 84 documents, 18 deal packs, with a published answer
key (`docs/sample-treasury/FACILITATOR_GUIDE.md`). The measurements that
motivated 4a and 4b.1 are recorded with those phases now
(`docs/dynamic-cycle-contracts.md`, *Phase 4a notes* / *Phase 4b.1 notes*).
What's below is what's still unaddressed.

**Identifier-only joins cannot reach the evidence.**

- Nostro statements print the reference on **18 of 18** pages —
  `PMT-2025-00074 Crescent Investment Bank - TD-2025-0094` — but into
  `transaction_narrative`, a `text` field. Join keys may only address
  `identifier` fields, so the linkage worker reached for `debit_account`
  instead: `fan_out_p95 = 6`, nine unmatched. An account number, not a
  transaction key.
- Broker notes print no deal reference at all (**0 of 9**). The proposed key
  `note_number = deal_reference` matched **0 of 9**.

**What worked, and must keep working.** Four join keys measured
`fan_out_p50 = p95 = max = 1` with zero unmatched, and the measurement
surfaced both bad keys above *before* anything was evaluated, in words:

> "Values of this key reach 6 records at the 95th percentile. A transaction
> key reaches about one; this looks like an entity identifier, and joining on
> it would fuse unrelated transactions."

That early warning is the single most valuable property of the current
design and the thing most at risk once assembly replaces the join.

**The late-field failure.** `second_approver` is stated on 3 of 18 payment
instructions, and seeded exception D5 is "payment instruction released under
a single signature above the dual-signature threshold": the absence of a
second approver *is* the exception. A field that enters a type's master late
— after some documents of that type were already read — leaves those earlier
readings silent on it, and that silence means either "the document does not
state this" or "nobody asked". In an audit the difference is the finding.
This is what 4c closes.

## Target architecture

```
intake      file suffix, no model                     route: table | document        built
read (1)    page 1 -> coarse class                    policy | minutes | background | evidence   built
read (2)    whole evidence document, text + images    records + citations, master schema per type   built
stamp       per type, once the type is read            the schema, and the readings stamped to it   built
assemble    one agentic pass per sampled item           role bindings + resolved operands            deferred
judge       model reader on raw values                 verdict + reason                             built
            the auditor overriding a verdict directly                                             deferred
```

Rows one through four are built (`docs/dynamic-cycle-contracts.md`, Phase 4a
and 4b.1 notes). Row five (assemble) is deferred, and until it lands the
graph is still built by approved join keys applied in code. Row six (judge)
is built except the override.

## 4b.2: evidence with no catalogued type

Today, evidence that fits no catalogued type is not read into records.
`save_schema` refuses `other` outright —

```python
if type_id == document_types.OTHER:
    raise WorkspaceError("The 'other' bucket cannot carry a schema; retype first.")
```

— `types_for_induction` (now the evidence-read equivalent) excludes it, so no
schema exists, so the document is read as prose instead of structured
records. `backend/app/agent/capabilities/documents.py`'s `_readable_evidence`
still excludes `other` explicitly today, with a comment naming this phase.
The absence of a catalogued *name* is not the absence of evidence, and in
this corpus the two documents no join could reach are exactly the kind that
land there.

So an evidence document the catalog cannot name should be read anyway, with
**the read coining its type**. `local.broker_note` would then carry a master
like any shipped type. The mechanism already exists for the auditor's own
retyping: `coin_local_type` takes `created_by`, defaulting to `"auditor"`,
and already accepts a discriminator, so model coining needs no new storage —
only a caller inside the read path, and a decision about who reviews it (see
*Open questions*).

The two alternatives are worse and worth naming. A shared `other` master
fuses the fields of unrelated documents, which is a bad join key by another
route. No master at all — open extraction, every document naming its own
fields — is precisely the drift 4b.1 removes, reintroduced in the bucket most
likely to need it: nine broker notes would produce nine vocabularies.

**The discriminator is required, not offered.** A coined type read by the RCM
on its name alone already put a one-document anomaly on the anchor side of
three population-wide comparisons, in a workspace where an auditor coined the
type. The mitigations are already built — coining captures a discriminator,
the catalog states document counts, and the master carries fill counts — and
model coining is safe only with all three in force.

`other` therefore stops being a terminal state and becomes a transient one: a
document is `other` until something reads it and names it. Retyping stays the
auditor's, and an auditor assignment still overrides a coined one under the
existing `assigned_by` rule.

## 4c: the late-field sweep

`document_masters.py` already carries the data this phase acts on: every
field records `introduced_at`, the index into `documents_read` at which it
first appeared, and `unread_for_field`/`late_fields` compute exactly which
prior readings predate a field's introduction. Neither function has a caller
today — the sweep they make possible does not run.

**The modification contract that produces this data is asymmetric**, because
additions are monotone and renames are not:

| Operation | When |
| --- | --- |
| Add a field | Freely. The document states something the master has no place for. This is the common case and cannot invalidate an earlier read. |
| Rename a field | Only where the master's name is *wrong* about what the field holds. A synonym is never grounds — that is the drift 4b.1 removes. |
| Split or merge | Not expressible today. A split composes from a rename plus an addition; a merge needs removal, which nothing in the read may do. |
| Remove a field | Never. The master only grows. |

Without the asymmetry, "the agent may revise the master" is the drift
mechanism relocated rather than removed: every document has some reason its
own phrasing reads better, and eighteen sequential renames would be the
result. The asymmetry is enforced by cost, not by judging the reason: a
rename is applied and *recorded* — in `renames`, with the document that asked
for it — and it must re-open every prior reading of the type on exactly the
terms a late-added field does, because prior readings used the old name and
their silence under the new name would be a lie. A rename costs what it
actually costs; the sweep is what makes ignoring the rule expensive rather
than free.

**What the sweep has to do.** Because the type is serialized and the master
only grows, the repair is bounded: re-read documents 1…N−1 of that type for
the added field (or renamed field) alone. Skip it and a late-discovered field
reads as absent on everything before it, silently — indistinguishable from
the document genuinely not stating it. That is the exact shape of D5 above.

**`documents_read` already appends on resume and resets only on
`revise_vocabulary`.** A run that resumes mid-type must append: the readings
already taken stand, and the indices they were assigned still describe what
they were asked, so renumbering them would falsify the sweep.
`revise_vocabulary` is the one case that rebuilds, and it rebuilds precisely
because it re-reads the type from the start — which is why 4c's sweep applies
to it exactly as it applies to the ordinary pass, with no second mechanism
needed.

What's missing is the trigger and the target: something that runs after a
reading adds a field or a rename, calls `unread_for_field`, and either
queues those documents for a narrow re-read or surfaces them to the auditor
as "read before this field existed" rather than leaving them silently
indistinguishable from "does not state this."

## Assemble: search replaces the join

*Deferred.*

Per sampled item, the agent searches the document index for the records
filling each role and returns bindings with citations. This is where the
join problems dissolve: the nostro statement is found by its narration, the
broker note by counterparty and amount and value date. No composite-join
mechanism is built, because there is no declared key to compose.

**The agent returns bindings and operand values. It does not return a
verdict.** That separation is what keeps the next stage reviewable.

## Judge: the per-verdict override

*Built except this.* `fieldwork.cycle_vouch` judges agreement on raw values
and returns a verdict with a reason (`docs/dynamic-cycle-contracts.md`, Phase
10 notes); `cycle_linking.evaluate_cycle_item` decides resolution in code and
takes the verdict from outside. What's deferred is narrower than that split
makes it look: the auditor's control today is the item disposition —
`confirmed` or `exception` — not a per-assertion override. A disposition
records what the auditor concluded about the item; an override would record
that the reader was wrong about one check, which is the thing a reviewer
wants as soon as they disagree with a single cell.

The rule this wants is the one `assigned_by` already enforces for
classification: an auditor verdict is final and is never overwritten by a
rerun. Nothing about the shipped shape blocks it — the verdict is a field on
the result, and the result already carries who and what produced it.

## Replay: freeze the resolution, not the vocabulary

*Deferred, and unnecessary until assembly lands: while join keys are declared
and applied by code, replay already runs off the stored ruleset.*

Late binding threatens replay, and replay is not negotiable. The answer is to
store what each item resolved to: which documents, which fields, which raw
values, which citations. Replay then runs over the stored binding rather than
a re-search. Non-determinism is paid once, at assembly, and captured —
instead of being re-incurred on every read.

This also makes `result_sha1` meaningful again: it hashes what was actually
compared, not the rule that hoped to compare it.

## The thing that must not be lost

**Neither 4b.2 nor 4c puts this at risk.** Join keys are still declared,
still measured, and still approved at rule level; fan-out still reports
before anything is evaluated. What follows is the obligation assembly takes
on when it lands.

Rule-level review is what makes an audit sign-off tractable: roughly ten
rules, thousands of links. Per-item assembly has no rules to review, and
reviewing every link does not scale — nor does it give consistency, since two
identical deals could be assembled by different reasoning.

The replacement is **measurement over what the assembler did**, computed
across items rather than declared in advance:

| Signal | Reads as |
| --- | --- |
| Roles found per item | `broker_confirmation` bound on 0 of 9 brokered deals |
| Ambiguity rate | The agent found two candidates and picked one |
| Empty results | Absence — which is a finding, not a gap |
| Operand resolution rate | The approver was read on 18 of 18, not 4 |
| Verdict distribution per assertion | One assertion failing everywhere is a rule fault, not 18 exceptions |

Today's fan-out check earned its place by catching two bad join keys before
evaluation. Its replacement has to be built in the same spirit and surfaced
on the same screen, or assembly trades a loud failure for a quiet one. **This
is the highest-risk item here and should be built alongside assembly, not
after it.**

Absence deserves particular care. Several seeded exceptions in the treasury
sample *are* absences — a missing broker note, a settled deal with no
confirmation. A join reports absence as a number. A search that finds
nothing may be correct, or may have missed it, or may surface a neighbouring
deal's document and use it. The assembler must distinguish "searched and
found nothing" from "did not search", and the working paper must carry
which.

## Consequences elsewhere

**RCM coverage stays selector-exact until assembly.** The case for
intent-level matching is that `required_comparisons` naming
`{document_type, field}` pairs cannot survive documents that no longer share
field names. Under 4b.1 they *do* share field names — one name per fact
across a type is the whole point of the master — so the selector holds.
Intent-level matching becomes necessary only when assembly resolves roles per
item and there is no declared field for a selector to name; it is deferred
with assembly.

**`local.` coined types keep their value and gain a duty.** A coined type
read by the RCM as the deal record, on its name alone, put a one-document
anomaly on the anchor side of three population-wide comparisons. Coining must
capture a discriminator, and the catalog must state document counts. Both are
already built; 4b.2 depends on them holding for model-coined types too.

**Measurement of the corpus stays.** `cycle_measurement` loses join-key
fan-out and gains assembler statistics once assembly lands. The parity
property — that what measurement reports equals what the engine reached — is
worth preserving in the new form.

## Migration

There is none, for the same reason 4a and 4b.1 needed none: existing
workspaces are regenerated rather than migrated. A workspace whose `other`
documents predate 4b.2 is re-read by the pass that ships it; nothing has to
sweep them retrospectively. `docs/dynamic-cycle-contracts.md` ruled out a
dual run for the same reason — keeping two vocabularies alive doubles the
validation surface for a transitional period that only ends when every
workspace is re-extracted.

## Sequencing

| Phase | Content | Gate |
| --- | --- | --- |
| 4b.2 | Evidence with no catalogued type read anyway; the read coins a `local.` type carrying a discriminator | Nine broker notes carry one vocabulary, not nine |
| 4c | Late-field re-sweep over the documents that preceded the addition | Absence means "not stated", never "not asked" |

**Deferred**, in the order the rest of this document gives them: assembly
against the 18 known packs, assembler statistics and the screen that shows
them, the auditor's per-verdict override, deletion of the staleness family,
and intent-level RCM selectors. Nothing here forecloses any of them, and
4b.1 is what makes assembly's diff meaningful — a search that binds documents
is then being compared against join keys over a vocabulary that no longer
drifts.

Assembly remains the whole bet and still costs about a day: the corpus, the
packs and the answer key already exist, and four join keys give a known-good
baseline to diff against. Where the assembler agrees with them, the approach
is safe. Where it disagrees — the nine broker notes and the eighteen nostro
statements, which no join can reach — the diff *is* the result.

The warning that assembly must wait on assembler statistics applies to
*removing* the schema, whose only compensating control is those statistics.
4b.2 and 4c keep the schema and keep join-key fan-out, so they do not wait.

## Open questions

Each needs a decision before its phase.

- **Whether a model may coin a type no auditor ever sees.** The mechanism
  (4b.2) allows it and the discriminator makes it safe to read, but a
  workspace whose vocabulary is entirely model-coined has had no human look
  at what its documents *are*. The `other` review surface exists; what is
  unsettled is whether coining should route through it.
- **Judged verdict stability.** Same values, same verdict. There is no
  property test for it, and the reader is already in the verdict slot on
  every two-sided assertion — so this is owed against the current code, not
  against a later phase. What makes it tractable is that the judged half is
  narrow: resolution is deterministic, and the only question asked of the
  model is whether two values state the same fact.
- **The per-verdict override.** The auditor disposes of the item; they
  cannot yet say the reader was wrong about one cell. Until they can,
  disagreeing with a single check means dispositioning the whole item around
  it.
- **Cost per item**, against the deferred phases rather than the next one.
  Vouching runs over sampled items, not the 1,000-row population — 18 here.
  At engagement scale this needs a number, not a guess. One call per item
  rather than per assertion is what makes this affordable at all, and it is
  the number that decides whether judging can ever widen beyond a sample.
- **Assembler determinism.** Two runs over the same corpus should bind the
  same documents. If they do not, the stored binding is the record and
  reruns need a diff, not a silent replacement.
- **Where the master still earns its keep once assembly lands.** As prior
  art it is cheap and probably useful. If it turns out to bias extraction
  toward its own field names, it should go entirely.
