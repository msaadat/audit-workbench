# Agentic vouching: collapsing the pipeline

This plan supersedes the pass structure in `docs/dynamic-cycle-contracts.md`.
The contracts there are sound; what this changes is *when* the vocabulary is
frozen. Today it is frozen before the evidence is read, and everything
downstream must conform to a guess made from two or three samples. The result is
a large body of machinery whose only job is to detect and repair the mismatch.

Two rules from that document are withdrawn by decision:

| Withdrawn | Replaced by |
| --- | --- |
| "An LLM never decides an item's outcome" | A model reader reaches verdicts; the auditor reviews and overrides |
| "Evaluation stays code" | A model evaluator judges agreement on raw values |

The second was already half-true in name only — see *What the code already
does*. Both are provisional: they are taken to see what the architecture looks
like without them, and are to be re-evaluated against a real engagement rather
than settled here.

## Scope of the first tranche

This plan is cut. The first tranche is the read half — phases 4a, 4b and 4c —
and it lands against the pipeline `docs/dynamic-cycle-contracts.md` already
describes: approved join keys, code evaluation, the ruleset review screen and
the staleness family, all retained and all still doing their work.

**Neither withdrawal above is exercised yet.** They stand as the plan's
direction, and as the reason the later phases are shaped the way they are, but
nothing here puts a model in the verdict slot or replaces a join with a search.
Phase 0 — correcting the doc-to-code divergence — is separable and should still
be done, because the divergence is a fact about the code today rather than a
consequence of anything proposed here.

The reason to cut at this line is that the two halves fail for different
reasons. The 44-field `fx_contract` and the split `approved_by_id` are failures
of a vocabulary frozen before the evidence, and re-timing the schema repairs
them. The broker notes and the nostro statements are failures of a join that
cannot address the field the reference was printed into, and no amount of schema
work reaches them. Assembly does; it is deferred with the rest.

The sequencing warning below — *do not start Phase 4 before Phase 2 reports* —
does not bind this tranche. It is about removing the schema, whose only
compensating control is assembler statistics. This tranche keeps the schema and
keeps join-key fan-out, which is the loud failure signal that warning exists to
protect.

## The evidence this rests on

One treasury engagement, 84 documents, 18 deal packs, with a published answer
key (`docs/sample-treasury/FACILITATOR_GUIDE.md`). Every number below was
measured, not estimated.

**The frozen schema cost more than it bought.**

- `fx_contract` induced **44 fields from three samples of one identical
  template** — `counterparty_swift` and `counterparty_swift_code`,
  `matched_by_date` and `matched_date` and `matcher_signature_date`,
  `date_of_issue` and `issue_date`. Union is by exact field name, and nothing
  reconciles two names for one fact.
- `payment_instruction` carries `approved_by_id` **and**
  `approved_by_employee_id`: same role, same `value_type`, same fact. No
  document fills both. 14 fill one, 4 fill the other. The conflict rule looks at
  `value_type` and `role` — the two axes on which they are identical.
- The RCM then wrote a segregation-of-duties assertion on `approved_by_id`. It
  evaluates on **4 of 18** deals and reports nothing wrong. The approver is
  printed on all 18.
- Re-deriving schemas mid-run bumped them v1 → v2 and **orphaned 65 completed
  extractions** as `stale_schema_reference`. Recovering them took three
  further runs.

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
`fan_out_p50 = p95 = max = 1` with zero unmatched, and the measurement surfaced
both bad keys *before* anything was evaluated, in words:

> "Values of this key reach 6 records at the 95th percentile. A transaction key
> reaches about one; this looks like an entity identifier, and joining on it
> would fuse unrelated transactions."

That early warning is the single most valuable property of the current design
and the thing most at risk below.

## What the code already does

`cycle_linking.evaluate_cycle_item` does not compute agreement. For a two-sided
assertion the verdict is `judgment.get("verdict")` — supplied from outside the
engine. The operators settle only *resolution* (`ambiguous`,
`missing_evidence`, `invalid_extraction`) and one-sided `present`. The result
already carries a `reason` for "why the reader reached this verdict, in its own
words", and `judgment_request` already sends **raw** values rather than
normalized ones, for a reason it states plainly:

> "presentation carries the difficulty — a currency prefix, a vendor code, a
> scanned date — and a reader handed the folded value would be answering an
> easier question than the one the documents pose."

That is the `Rs. 2000` vs `2000` argument, already in the code. So the second
withdrawal is smaller than it reads: the reader slot exists, receives raw
values, and records a rationale. What changes is who fills it. The doc claiming
"evaluation stays code" is a doc-to-code divergence, and correcting it is part
of this work.

## What an audit run does today

Two facts about the current path, measured against the code rather than
inferred. Both are repaired by the same edit, and both are reasons to make it.

**Two categories are in neither set, and one of them is the default.**

```
all      : background contract correspondence evidence minutes other policy prior_report regulation voucher
planning : background contract correspondence minutes policy prior_report regulation
voucher  : voucher
neither  : evidence, other
```

A document holding `other` or `evidence` is not planning material and is not
transaction evidence. In an audit run it gets no text extracted, no type, no
schema and no analysis. `other` is what intake assigns to anything its filename
rules cannot name — `_validated_decision` forces it, and the import writes
`document_category or "other"` — so a document whose *name* is uninformative is
invisible, for a reason that has nothing to do with what is in it.

**In an audit run, evidence never reaches classification at all.**
`routing.py` sets `document_scope_mode = "planning"` for the audit workflow;
`resolve_document_scope` then selects only `_planning_relevant` documents, which
is disjoint from the evidence category by construction. `_text_units` and
`_classified_units` both intersect with that scope. Run against a workspace
holding one policy and one voucher:

```
mode='planning'
  scoped ids     : ('b645c5ecf1',)          # the policy document only
  classify units : []
  ready          : satisfied
  types_for_induction: []
```

`documents.schemas_induced` reports **satisfied having induced nothing**, so
both Phase 8 edges into `planning.rcm_ready` are satisfied vacuously and the RCM
is written against a vocabulary that does not exist. The end-to-end induction
tests run the *documents* workflow, whose scope is `all`, so the audit path is
untested. It works in practice only where an auditor ran a standalone document
pass first, or named the evidence explicitly.

That is the failure the design it belongs to exists to remove: an engagement
reads its documents, generates no cycle evidence, and reports success. It is a
defect in the current code rather than a consequence of anything proposed here.
The tranche repairs it because the repair and the re-timing are the same edit.

## Target architecture

```
intake      file suffix, no model                     route: table | document
read (1)    page 1 -> coarse class                    policy | minutes | background | evidence   <- this tranche
read (2)    by class; evidence also gets a fine type  records + citations, master schema per type <- this tranche
assemble    one agentic pass per sampled item         role bindings + resolved operands           deferred
judge       model reader on raw values                verdict + reason, auditor overrides         deferred
```

Two model stages become one per document and one per item. The frozen artifacts
move from *before* the evidence to *after* it.

The first tranche builds the top two rows and leaves the bottom two as they are
today: approved join keys build the graph, and code decides each item. The
sections on assembly, judging and replay below describe the target rather than
what is being built now, and are kept because 4b's shape is chosen to make them
reachable.

### Intake: routing on file type, category on content

Intake keeps one decision and re-times another. **Route** — table, document,
unsupported, ignore — stays rule-based on file suffix, because a CSV cannot be
handed to a page-1 classifier and `loader.SUPPORTED_SUFFIXES` already answers it
without a model.

**`document_category` stays as a field and loses its derivation.** Every
consumer tests set membership — `category in PLANNING_DOCUMENT_CATEGORIES`,
`category in VOUCHER_DOCUMENT_CATEGORIES` — and none of them reads how the value
was arrived at. Keeping the name and moving the derivation from the filename to
page one leaves `_planning_relevant`, `analysis_profile`, `transaction_evidence`, the
planning and APM context selectors and the document listing untouched, and it
preserves the planning/evidence disjointness by construction rather than
re-establishing it on a new axis.

What the filename cannot support is the *value*, and the current prompt admits
as much — "Filenames can be suggestive but are not evidence of document
content." The deterministic rules behind it are token matches on the stem:

```python
elif any(token in label for token in ("policy", "procedure", "sop", ...)):
    document_category = "policy"
```

Content-blind intake therefore ends. Taken deliberately: in the code that
property is framed as an accuracy caveat rather than a policy, and reading page
one is strictly more accurate than guessing from a name.

**The domain collapses from ten values to four** — the same four the coarse
class reads, so there is no mapping layer between what the model answers and
what the field holds:

| value | holds |
| --- | --- |
| `policy` | policy, regulation, procedure, an authority matrix |
| `minutes` | minuted decisions of a governing body |
| `background` | every other planning document: contracts, prior reports, correspondence |
| `evidence` | transaction-level source material |

`evidence` is today's `voucher`, renamed to what it means. It is not a new
value — `evidence` is already in `DOCUMENT_CATEGORIES`, referenced by neither
set — so the new domain is a subset of the old one and the change is a value
merge rather than a schema change.

**There is no residual bucket.** A document is evidence or it is one of the
three planning values; nothing lands outside a set. That is what removes the
limbo above, and it is the property to test rather than the classifier's
accuracy on any single document.

**Intake stops filling the field in.** Today it defaults twice — the import
writes `document_category or "other"`, and `_validated_decision` forces `other`
for anything the rules could not name. Leaving it unset instead is what opens
the ordering below, because `_planning_relevant` already treats unset as in
scope while `other` is out of it.

### Read: one pass, two calls, a master schema per type

**Call one — coarse class, from page 1 (4,000 characters).** Four buckets, and
they *are* the category:

```
policy   minutes   background   evidence
```

The bucket decides which prompt runs next, and nothing else. Asking for a finer
choice would buy precision no consumer reads: nothing downstream distinguishes a
prior report from correspondence — they are the same set — so four values are
exactly the distinctions the pipeline acts on.

It must preserve the planning/evidence partition: the first three are planning
material, the fourth is transaction evidence. Every consumer tests set
membership (`category in PLANNING_DOCUMENT_CATEGORIES`) and never switches on an
individual value, so collapsing ten values into four is safe. The partition
itself is not — Phase 9 recorded what happens when policy material reaches the
structured profile and planning receives a record dump in place of narrative.

**This is a capability, between `documents.text_ready` and
`documents.types_classified`.** Placing it there resolves what would otherwise
be circular: the category gates document scope, and the category now needs page
one. `_planning_relevant` already treats an unset category as in scope —

```python
return not category or category in intake.PLANNING_DOCUMENT_CATEGORIES
```

— so a document intake leaves uncategorized is scoped, has its text extracted,
is read, and is categorized. The loop opens on its own once intake stops
defaulting to `other`. The text costs nothing extra: importing a document
already calls `documents.extract_document` synchronously, so the content is on
disk before any capability runs.

**Call two — by bucket.** Planning material is summarized as prose. Evidence is
read into structured records, and only evidence is given a fine document type.
Nothing consumes the fine type of a policy or a set of minutes: `document_type`
reaches the rest of the system solely through `transaction_evidence` and
`types_for_induction`, both evidence-only. That is already what the code does,
and it is retained rather than introduced.

**Evidence that fits no catalogued type is still read into records.** Today it
is not. `save_schema` refuses `other` outright —

```python
if type_id == document_types.OTHER:
    raise WorkspaceError("The 'other' bucket cannot carry a schema; retype first.")
```

— `types_for_induction` excludes it, so no schema exists, so `analysis_profile`
returns `standard` and the document is read as prose. The absence of a
catalogued *name* is not the absence of evidence, and in this corpus the two
documents no join could reach are exactly the kind that land there.

So an evidence document the catalog cannot name is read anyway, and **the read
coins its type**. `local.broker_note` then carries a master like any shipped
type. The mechanism exists: `coin_local_type` already takes `created_by`,
defaulting to `"auditor"`, and already accepts a discriminator, so model coining
needs no new storage.

The two alternatives are worse and worth naming. A shared `other` master fuses
the fields of unrelated documents, which is a bad join key by another route. No
master at all — open extraction, every document naming its own fields — is
precisely the drift 4b removes, reintroduced in the bucket most likely to need
it: nine broker notes would produce nine vocabularies.

**The discriminator is required, not offered.** A coined type read by the RCM on
its name alone put a one-document anomaly on the anchor side of three
population-wide comparisons. The mitigations are already built — coining
captures a discriminator, the catalog states document counts, and 4b requires
fill counts to travel with the master — and model coining is safe only with all
three in force.

`other` therefore stops being a terminal state and becomes a transient one: a
document is `other` until something reads it and names it. Retyping stays the
auditor's, and an auditor assignment still overrides a coined one under the
existing `assigned_by` rule.

**The master schema.** One per document type, held as working state for the run
and supplied to every document of that type as prior art. A document reads its
own content, sees what its predecessors settled on, and reuses those names. This
is what removes field drift, and it removes it at the point the fact is first
observed rather than by reconciling afterwards: `approved_by_id` and
`approved_by_employee_id` cannot both enter a master that already holds one of
them.

Per-document calls can only agree if they are not independent. Serializing them
per type and handing each the accumulated master is what makes agreement
possible at all.

**The two calls take the two barriers the scheduler already has.**

Call one is parallel. Nothing about a page-1 coarse class depends on any other
document, and it commits nothing that another unit reads, so it qualifies for
`all_settled_parallel` on exactly the grounds chunk analysis does today.

Call two is sequential — `all_settled_then_validate` — and that is the correct
barrier rather than a concession. It is what the serialized path already
provides:

```python
for unit in pending:
    self.set_unit(stage, unit, "running")
    self._run_one_pipeline_unit(execution, capability, stage, unit)
    # Serialized units can commit workspace state. Rebind the next
    # unit against that committed state instead of retaining a stale
    # stage-start subject and producing a false parent conflict.
    self._refresh()
```

Every document binding against state its predecessor committed *is* the master
accumulating. The parallel path cannot do it: it binds every unit before running
any of them, so a unit's input is resolved at stage start and can never see what
a sibling settled. That is the deeper incompatibility, and it is why "make the
read parallel and lock the master" is not an option — the reads would not be
wrong about the master, they would never have been shown it.

**Order the units by type, then document.** Units within a stage execute in
sorted **id** order, never declaration order, so a read unit keyed
`document_read:<document_type>:<document_id>` puts a type's documents in one
contiguous run for free — which is what makes "the end of a type" a moment the
stamp can happen at, and what bounds the late-field sweep below. This is the same
ordering fact that cost the induction pass a rebuild: `document_schema:x` sorts
before `document_schema_sample:x:y` because `:` precedes `_`, and the freeze ran
while its samples were still queued.

**The accepted cost is wall time on the read pass.** Fully serialized, the
treasury corpus is 84 steps against 21 for today's
`84 / max_llm_concurrency`. Type-lane concurrency would be 18 — the largest
type — but there is no barrier that expresses it: `barrier` is a field on
`Capability`, one flag for the whole capability, and the runner branches on it
once for the entire stage. Adding a `grouped_sequential` barrier is a
self-contained scheduling change that alters no stored data and no unit
contract, so it can land later on its own evidence. Deferring it keeps the first
change to one question — does a serialized master remove field drift — rather
than two.

Never re-introduce concurrent writes to the master. At engagement scale the
escape valve is batching several documents into one call, which keeps one writer
while cutting the step count; a batch that disagrees with itself needs a rule
for which reading wins.

**The modification contract is asymmetric**, because additions are monotone and
renames are not:

| Operation | When |
| --- | --- |
| Add a field | Freely. The document states something the master has no place for. This is the common case and cannot invalidate an earlier read. |
| Rename a field | Only where the master's name is *wrong* about what the field holds. A synonym is never grounds — that is the drift this design exists to remove. |
| Split or merge | Must name the earlier extractions affected. |

Without the asymmetry, "the agent may revise the master" is the drift mechanism
relocated rather than removed: every document has some reason its own phrasing
reads better, and eighteen sequential renames is the result.

**The version is stamped once, when the type's documents are done.** During the
run the master is mutable working state, so there is no mid-run bump and the
staleness family has nothing to detect. This makes the failure that cost this
engagement three recovery runs — a re-derivation orphaning 65 completed
extractions as `stale_schema_reference` — structurally impossible rather than
merely unlikely. The stamped version is provenance, not a contract: *this is
the vocabulary that emerged from reading these N documents.*

**A field added late re-opens the documents that preceded it.** This is the one
cost of deferring the version, and it is not optional. If the master gains a
field at document 15, documents 1–14 were read without it, and their silence
then means either "the document does not state this" or "nobody asked" — which
are different, and in an audit the difference is the finding. In this corpus
`second_approver` escaped the schema on 3 of 18 payment instructions, and D5 is
"payment instruction released under a single signature above the dual-signature
threshold": the absence of a second approver *is* the exception.

Because the type is serialized and the master only grows, the repair is
bounded — re-read documents 1…N−1 of that type for the added field alone. Skip
it and a late-discovered field reads as absent on everything before it, silently.

**Fill counts travel with the master.** An accumulating master carries fields
only one document ever stated — a single internally-produced confirmation would
contribute `printed_by_name`, `printed_on` and `status` to its type. Harmless as
a hint, dangerous as a selector: the RCM authoring turn chose
`fx_contract.received_date` at 0 of 11 precisely because it saw names without
frequencies. The counts added to `schema_catalog` stop being a refinement and
become what keeps an accumulating master usable.

**What goes, and what stays.** Deleted with the freeze: the reconcile call,
union-never-intersect, escape rate, `sample_for_induction`, and the
`schemas_sampled → schemas_induced` edge pair. All of it exists to police a
guess made from two or three samples, and there is no guess left to police.

Retained, against this plan's later phases: `schema_hash` provenance,
`schema_ref`, `is_current`, `stale_schema_reference`. These are the interlock
that stops values being reinterpreted under fields they were never read against
— `has_usable_analysis` and `cycle_measurement.structured_records` both depend
on them — and while evaluation still runs over declared join keys they are
load-bearing. What changes is that they go quiet: with the version stamped once
per type per run, nothing goes stale mid-run and the family has nothing to fire
on. They are deleted in Phase 5, after assembly replaces the join, not here.

`additional_fields` survives as a storage shape and stops being reached for. Its
purpose was to hold a fact the frozen vocabulary had no room for; under a master
that grows there is no such fact, because the master takes the field. Deleting
it is cleanup, not part of this tranche.

### Assemble: search replaces the join

*Deferred. Described here because 4b is shaped to make it reachable.*

Per sampled item, the agent searches the document index for the records filling
each role and returns bindings with citations. This is where the join problems
dissolve: the nostro statement is found by its narration, the broker note by
counterparty and amount and value date. No composite-join mechanism is built,
because there is no declared key to compose.

**The agent returns bindings and operand values. It does not return a verdict.**
That separation is what keeps the next stage reviewable.

### Judge: the reader slot, filled by a model

*Deferred.*

Raw values, one assertion at a time, verdict plus reason, through the existing
`judgment_request` shape. The auditor sees every judged item and can change any
verdict; an auditor verdict is final and is never overwritten by a rerun — the
same provenance rule `assigned_by` already enforces for classification.

### Replay: freeze the resolution, not the vocabulary

*Deferred, and unnecessary until assembly lands: while join keys are declared
and applied by code, replay already runs off the stored ruleset.*

Late binding threatens replay, and replay is not negotiable. The answer is to
store what each item resolved to: which documents, which fields, which raw
values, which citations. Replay then runs over the stored binding rather than a
re-search. Non-determinism is paid once, at assembly, and captured — instead of
being re-incurred on every read.

This also makes `result_sha1` meaningful again: it hashes what was actually
compared, not the rule that hoped to compare it.

## The thing that must not be lost

**This tranche does not put it at risk, which is most of the argument for
cutting here.** Join keys are still declared, still measured, and still approved
at rule level; fan-out still reports before anything is evaluated. What follows
is the obligation assembly takes on when it lands, and it is stated now because
4b changes what those measurements are computed over.

Rule-level review is what makes an audit sign-off tractable: roughly ten rules,
thousands of links. Per-item assembly has no rules to review, and reviewing
every link does not scale — nor does it give consistency, since two identical
deals could be assembled by different reasoning.

The replacement is **measurement over what the assembler did**, computed across
items rather than declared in advance:

| Signal | Reads as |
| --- | --- |
| Roles found per item | `broker_confirmation` bound on 0 of 9 brokered deals |
| Ambiguity rate | The agent found two candidates and picked one |
| Empty results | Absence — which is a finding, not a gap |
| Operand resolution rate | The approver was read on 18 of 18, not 4 |
| Verdict distribution per assertion | One assertion failing everywhere is a rule fault, not 18 exceptions |

Today's fan-out check earned its place by catching two bad join keys before
evaluation. Its replacement has to be built in the same spirit and surfaced on
the same screen, or this plan trades a loud failure for a quiet one. **This is
the highest-risk item here and should be built alongside assembly, not after
it.**

Absence deserves particular care. Several seeded exceptions in the treasury
sample *are* absences — a missing broker note, a settled deal with no
confirmation. A join reports absence as a number. A search that finds nothing
may be correct, or may have missed it, or may surface a neighbouring deal's
document and use it. The assembler must distinguish "searched and found
nothing" from "did not search", and the working paper must carry which.

## Consequences elsewhere

**RCM coverage stays selector-exact in this tranche.** The case for
intent-level matching is that `required_comparisons` naming
`{document_type, field}` pairs cannot survive documents that no longer share
field names. Under 4b they *do* share field names — one name per fact across a
type is the whole point of the master — so the selector holds and Phase 8's
withdrawal of intent-level matching stands.

The 4-of-18 assertion is repaired here by the vocabulary rather than by the
selector. With `approved_by_id` and `approved_by_employee_id` unable to coexist
in one master, the assertion the RCM writes evaluates on all 18. Intent-level
matching becomes necessary at Phase 1, where assembly resolves roles per item
and there is no declared field for a selector to name; it is deferred with it.

**The expensive pass moves ahead of the RCM.** Today
`planning.rcm_ready ← documents.schemas_induced` is affordable because induction
is two or three samples a type. An accumulating master is not final until every
document of its type has been read, and that read *is* the extraction pass, so
the edge now means "read the evidence first."

The alternatives are worse. Letting the RCM address a provisional master
reintroduces exactly what is being removed — the matrix names a field document
15 renames. Decoupling the RCM from field names is intent-level matching, which
belongs with assembly. What the engagement buys for the cost is a matrix written
against the complete vocabulary of its corpus rather than one guessed from three
samples, and the engagement *order* is unchanged: documents are still read
before planning reasons about them, which is what the graph already says.

**`local.` coined types keep their value and gain a duty.** A coined type read
by the RCM as the deal record, on its name alone, put a one-document anomaly on
the anchor side of three population-wide comparisons. Coining must capture a
discriminator, and the catalog must state document counts. Both are already
built; keep them.

**Measurement of the corpus stays.** `cycle_measurement` loses join-key fan-out
and gains assembler statistics. The parity property — that what measurement
reports equals what the engine reached — is worth preserving in the new form.

## Migration

There is none. Existing workspaces are regenerated rather than migrated:
classification, the coarse class and extraction all re-run, and the vocabulary a
workspace ends with is the one its own documents produced. This is a decision,
not an oversight, and it is what makes several of the choices above cheap.

- The `voucher` → `evidence` rename needs no alias-on-read and no stored-value
  rewrite, so the four-value domain can be enforced rather than tolerated
  alongside its predecessor.
- Documents sitting in the `other` and `evidence` limbo are re-categorized by
  the same pass that reads them, so nothing has to sweep them retrospectively.
- Extractions stamped against a sampled schema are replaced, not reconciled
  against a master. Nothing has to decide what an old `additional_fields` entry
  means under a vocabulary that would have taken the field.

`docs/dynamic-cycle-contracts.md` ruled out a dual run for the same reason:
keeping two vocabularies alive doubles the validation surface for a transitional
period that only ends when every workspace is re-extracted. It holds more
strongly here, because the two vocabularies would differ in *when* they were
fixed rather than in what they contained — which is not a difference any stored
record carries.

## Sequencing

Each phase leaves the tree working.

**This tranche.**

| Phase | Content | Gate |
| --- | --- | --- |
| 0 | Correct the doc-to-code divergence: `dynamic-cycle-contracts.md` claims evaluation is computed; it is judged | Doc matches code |
| 4a.1 | Category domain to four values; `voucher` → `evidence`; intake stops defaulting to `other` | No stored category outside the four; nothing lands in a set-less value |
| 4a.2 | Coarse class capability between `text_ready` and `types_classified`; category derived from page one; routing stays on file suffix | Planning/evidence partition holds; no policy document reaches the structured prompt |
| 4a.3 | `corpus_scope`: `text_ready`, `categorized` and `types_classified` run over every document, not the planning subset | An audit run classifies its evidence; `schemas_induced` cannot report satisfied having induced nothing |
| 4b.1 | Master schema, one writer: the read capability takes `all_settled_then_validate`, units keyed `<type>:<document>`; asymmetric modification contract; version stamped at end of type | Field drift gone: one name per fact across a type |
| 4b.2 | Evidence with no catalogued type read anyway; the read coins a `local.` type carrying a discriminator | Nine broker notes carry one vocabulary, not nine |
| 4c | Late-field re-sweep over the documents that preceded the addition | Absence means "not stated", never "not asked" |

4a.3 is a defect repair, is separable, and can land first on its own. It is
worth doing whether or not the rest proceeds.

No new barrier is built. The coarse class runs under the existing parallel
barrier and the read under the existing sequential one, which keeps the first
change to one question rather than two. Type-lane concurrency —
`grouped_sequential`, 18 steps against 84 — is deferred, and is a pure
scheduling change whenever the read pass is measured to be worth it.

**Deferred**, in the order the rest of this plan gives them: assembly against
the 18 known packs, assembler statistics and the screen that shows them, the
model reader in the verdict slot, deletion of the staleness family, and
intent-level RCM selectors. Nothing here forecloses any of them, and 4b is what
makes assembly's diff meaningful — a search that binds documents is then being
compared against join keys over a vocabulary that no longer drifts.

Phase 1 remains the whole bet and still costs about a day: the corpus, the packs
and the answer key already exist, and four join keys give a known-good baseline
to diff against. Where the assembler agrees with them, the approach is safe.
Where it disagrees — the nine broker notes and the eighteen nostro statements,
which no join can reach — the diff *is* the result.

The warning that Phase 4 must wait on Phase 2 applies to *removing* the schema,
whose only compensating control is the assembler statistics. This tranche keeps
the schema and keeps join-key fan-out, so it does not wait.

## Implementing 4a

4a stands alone. It changes where the category comes from and nothing about how
a schema is induced, so everything below it — sampling, the freeze, structured
extraction, join keys, evaluation — runs unaltered, and 4b may follow later or
not at all.

Two consequences to expect rather than discover:

- **An audit run gets more expensive.** Today it classifies and induces nothing.
  Afterwards it does both, over the whole corpus. That is the defect being
  repaired, and the cost is what the stage should always have cost.
- **More documents become evidence.** Filename tokens under-recognise and page
  one does not, so induction and extraction expand over what the corpus actually
  holds. The number will be larger than any current run reports.

### The category is written twice, and that decides the barrier

It cannot live only on the document entry. Capability readiness runs against
whatever workspace handle its caller holds, which is routinely several revisions
behind; a lazily hydrated collection read from one reported a document
unclassified moments after it had been classified, which is why type assignments
moved to `Documents/.types` sidecars. So the category joins the type there, one
record answering both questions about a document under one provenance rule, and
`assign`'s auditor-override semantics carry over unchanged.

It cannot live *only* in the sidecar either, and this is what building it
settled. Fifteen call sites read `document.get("category")` — planning document
ranking, planning and APM context selection, the artifact index, narration, the
document listing — and most hold a document dict with no workspace to reach a
sidecar with. Rewriting all of them would be a far wider change than the answer
moving, and several have no handle to rewrite *to*.

So the commit writes both, inside one `mutate()`: the sidecar is authoritative
and is what readiness and the evidence gate ask, the entry carries a mirror for
the readers that hold a dict. **That is what makes the capability sequential.**
The entry write lands on the shared `documents` collection, which is exactly what
`all_settled_parallel` asserts cannot happen — the same constraint, and the same
reason, as `documents.types_classified` next to it. Page-one categorization is
independent per document and still cannot run in parallel, because independence
of inputs is not independence of commits.

The cost is one sequential model pass where there was none, not one where there
was a parallel one. Making it parallel later is not a barrier change but a
storage change: it needs the fifteen readers off the entry first.

`category()` reads the sidecar and falls back to the entry. The fallback is what
lets an upload that names a category outright stand without waiting for a model
to agree with it, and it cannot go stale — a value written *during* a run goes to
the sidecar, which wins, and a value present only on the entry was there from the
revision the document arrived at.

### Modules

| Module | Change |
| --- | --- |
| `intake.py` | `DOCUMENT_CATEGORIES` to four values; `PLANNING_DOCUMENT_CATEGORIES` to three; `VOUCHER_DOCUMENT_CATEGORIES` → `EVIDENCE_DOCUMENT_CATEGORIES = {"evidence"}`; delete the category branch of `deterministic_classification` and `TRANSACTION_EVIDENCE_FILENAME`; stop defaulting in `_validated_decision` and at import |
| `documents.py` | `CATEGORIES` is a second copy of the same list and moves with it; `add_document(category="other")` loses its default; `_validate_upload` accepts unset |
| `document_classification.py` | the sidecar gains `category` and `category_assigned_by`; `transaction_evidence` reads it instead of the document entry; `categorized_ids` / `uncategorized_ids` alongside the existing pair |
| `agent/workers/documents.py` | new `documents.category` worker |
| `agent/capabilities/documents.py` | new capability; `_planning_relevant` and `analysis_profile` re-pointed at the sidecar; the scope fix |
| `agent/workflows/documents.py`, `workflows/audit.py` | one capability, one edge, in both graphs |
| `agent/workers/intake.py` | deleted, with the batch-refinement path that calls it |
| `frontend/.../ImportDialog.vue` | the review step and the assistant refinement go |
| `frontend/.../capabilityLabels.ts` | one entry |

### The capability

```
documents.text_ready  ->  documents.categorized  ->  documents.types_classified
```

Declared once in `workflows/documents.py` and reused by the audit graph, the way
the existing document capabilities already are.

**Readiness.** Every scoped document with usable text carries a category. Unlike
`types_classified` there is no truthful "none of these fits" answer — four
buckets are exhaustive by construction — so a document without one is a gap and
reports as one.

**Units.** One per uncategorized document, input `{document_id, title, text}`,
where `text` is the same page-one slice `classification_text` already produces.
Reusing it keeps one definition of what "page one" means for both calls.

**Barrier.** `all_settled_parallel`, on the sidecar decision above.

**Order.** `documents.categorized` sorts before `documents.types_classified`
lexically as well as by edge, which is incidental but worth not disturbing:
units within a stage run in sorted id order, and that ordering has already cost
this design one rebuild.

### The worker

Page-one text to one of four values, plus a rationale. No document type, no
fields, no records — the bucket decides which prompt runs next and nothing else,
and a worker asked for more would invite the model to answer the next question
early.

The prompt states the partition rather than the labels, because the labels are
the part a reader can guess and the partition is the part that carries the
failure: policy, minutes and background describe how the entity should operate;
evidence is a record of one transaction. The existing intake prompt's category
paragraph is the text to lift, minus the six values that no longer exist.

### Intake

`route` stays deterministic on file suffix. What goes with the category is the
model call over filenames: with `document_category` removed, the intake
classification worker's remaining outputs are `route` (the suffix),
`proposed_name` (the slug), `proposed_action` (duplicate detection) and
`table_role` (referenced nowhere but `types.ts`). Nothing judgemental is left,
and a worker whose every answer is already computed is a turn spent to agree
with arithmetic. Confirm `table_role` is genuinely unconsumed before deleting
it; the rest is safe on inspection.

### The import dialog

Four steps become three: **Add files → Upload → Complete**. The review step goes
entirely, and with it the classification editor, the attention filter, the
`document_category` picker, "Refine with assistant", and the permission-mode
approval batch.

What that step was for, and where each part lands:

| Was reviewed | Now |
| --- | --- |
| `document_category` | Read from page one, after import, in the spine |
| `route` | Deterministic on suffix; shown in the completion summary, not decided there |
| `proposed_name` | The slug, renameable afterwards where documents are listed |
| `proposed_action` | Duplicates are already ignored automatically |
| A file the local parser could not read | A line in the completion summary — the one thing the review step surfaced that nothing else does |

That last row is the only real loss and it must not be dropped silently: an
unreadable file that vanishes into a count is exactly the failure this plan
objects to elsewhere. The summary already carries `imported`, `unchanged`,
`ignored` and `ambiguous`; unreadable files need naming, not counting.

Import therefore completes on upload. The auditor's next decision is not "is
this a policy" — which they should not have been asked from a filename — but
the `other` bucket review that already exists, once documents have been read.

### The spine

`documents.categorized` becomes a row in the Record spine like any other
capability, titled **Document classification**. Its `Capability.title` is the
label and `capabilityLabels.ts` carries the same string verbatim —
`test_plan_spine_capability_labels.py` fails if the two drift. It sits directly
above *Document types*, which reads correctly: the two rows answer the two
questions about one document, in the order they are asked.

### What building it forced

**The intake worker survives; only its category goes.** Deleting it outright was
the tidier plan and the wrong scope. `IntakeRunner` is retained by an explicit
decision record (`docs/agent-protocol-runner-decisions.md`), carries a context
preset, an adapter and an approval path, and still refines the route and the
proposed name. Removing `document_category` from what it may merge is the change
4a actually needs; retiring the runner is a separate question that should be
asked on its own evidence.

**Four intake tests were retired, not repaired.** They pinned what a filename
stem should be read as — `Procurement SOP Extracts` policy, a dealing ticket
voucher rather than the `other` a P2P-shaped vocabulary once made of a whole
treasury sample. They were defending an answer to a question intake should not
have been answering, and the vocabulary they defended (`TRANSACTION_EVIDENCE_FILENAME`,
~60 lines of derived terms and abbreviations with a hand-kept unsafe list) went
with them.

**The upload route defaulted to a category that no longer exists.** `Form("other")`
became a 400 the moment the domain shrank. It defaults to unset now, and an
uploader that does know what a document is may still say so.

**Importing evidence now re-opens the planning chain.** This is 4a.3 working, and
it is worth stating because it looks like a regression. Before, a voucher was
outside every capability's scope, so importing one committed nothing and a
workspace's existing planning state was simply reused. Now the document is read
and typed, which publishes a revision, which invalidates
`planning.context_ready` and everything after it.

That is the correct answer — the corpus changed, and planning rests on it — but
it means an engagement that imports evidence into a planned workspace will
re-synthesize its planning material. Two consequences follow: a workspace
carrying *only* evidence has no planning material to re-synthesize from and its
context capability fails rather than holding what it had, and a re-opened APM
lands in `review_required` rather than flowing through to fieldwork. Neither is
introduced here; both were unreachable while evidence was invisible. Whether
planning should hold its ground when nothing planning-relevant changed is a real
question, and it belongs to the planning capability rather than to this tranche.

### Tests

- The partition is exhaustive: every value in `DOCUMENT_CATEGORIES` is in
  exactly one of the planning and evidence sets. This is the test that would
  have caught the `other`/`evidence` limbo, and it is a set-arithmetic assertion
  with no fixture behind it.
- An audit run over a workspace holding one policy and one item of evidence
  classifies both and induces a schema for the evidence. This is the case
  `test_workflow_schema_induction.py` does not cover, because it runs the
  documents workflow; the audit path needs its own.
- A document intake left uncategorized is in scope for `text_ready`, and is
  categorized before `types_classified` expands.
- The category is never assigned by a model over an auditor's, which is the
  existing `assigned_by` guarantee extended to a second field and worth
  asserting rather than assuming.
- An unreadable file is named in the completion summary rather than counted.

## Open questions

Each needs a decision before its phase:

- **What a failed read does mid-type.** The type is serialized, so a failure at
  document 9 leaves a master built from eight. Stamping it anyway records a
  vocabulary as complete when it is not; refusing loses eight reads. The
  existing scheduler answer — a stage with any failed unit folds to `failed` —
  is stricter than the binder underneath it, and `docs/dynamic-cycle-contracts.md`
  already flags that mismatch as deliberately left open.
- **Whether a model may coin a type no auditor ever sees.** The mechanism allows
  it and the discriminator makes it safe to read, but a workspace whose
  vocabulary is entirely model-coined has had no human look at what its
  documents *are*. The `other` review surface exists; what is unsettled is
  whether coining should route through it.
- **What the first document of a type anchors.** Serialization plus an
  add-mostly master makes order matter far less, but document one still
  contributes its names with nothing to check them against. Seeding each type
  from a stratified pick would remove the last of it, at the cost of keeping a
  sampling step this plan otherwise deletes. Worth measuring before deciding:
  process a type in two different orders and diff the masters.
- **Incremental arrival.** A document imported after its type was stamped
  either appends to the master and re-stamps, or re-opens the type. Appending is
  cheap and consistent with a master that only grows; it needs the same
  late-field sweep when the new document contributes a field.
- **When the read pass needs concurrency back.** Serialized, the treasury corpus
  is 84 steps against 21 today. That is an acceptable trade for one engagement
  and an obvious one to revisit; what is unsettled is the threshold. Two answers
  exist and neither is foreclosed: a `grouped_sequential` barrier, which is a
  scheduling change touching no stored data and no unit contract; or batching
  several documents into one call, which keeps one writer while cutting the step
  count but needs a rule for a batch that disagrees with itself.

Open against the deferred phases rather than this tranche:

- **Cost per item.** Vouching runs over sampled items, not the 1,000-row
  population — 18 here. At engagement scale this needs a number, not a guess.
- **Assembler determinism.** Two runs over the same corpus should bind the same
  documents. If they do not, the stored binding is the record and reruns need a
  diff, not a silent replacement.
- **Judged verdict stability.** Same values, same verdict. Worth a property
  test before the reader is trusted on anything but sampled items.
- **Where the master still earns its keep once assembly lands.** As prior art it
  is cheap and probably useful. If it turns out to bias extraction toward its own
  field names, it should go entirely.
