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

## Scope

This plan is cut. The read half — 4a, 4b, 4c — lands against the pipeline
`docs/dynamic-cycle-contracts.md` already describes: approved join keys, code
evaluation, the ruleset review screen and the staleness family, all retained and
all still doing their work. **4a is built**; what remains is the master schema
and its late-field sweep.

**Neither withdrawal above is exercised by any of it.** They stand as the plan's
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

## Target architecture

```
intake      file suffix, no model                     route: table | document
read (1)    page 1 -> coarse class                    policy | minutes | background | evidence   built
read (2)    by class; evidence also gets a fine type  records + citations, master schema per type 4b
assemble    one agentic pass per sampled item         role bindings + resolved operands           deferred
judge       model reader on raw values                verdict + reason, auditor overrides         deferred
```

Two model stages become one per document and one per item. The frozen artifacts
move from *before* the evidence to *after* it.

Row one is built. Row two is 4b, and the bottom two stay as they are today:
approved join keys build the graph, and code decides each item. The sections on
assembly, judging and replay below describe the target rather than anything
being built now, and are kept because 4b's shape is chosen to make them
reachable.

### Intake: routing on file type, category on content

Intake keeps one decision and re-times another. **Route** — table, document,
unsupported, ignore — stays rule-based on file suffix, because a CSV cannot be
handed to a page-1 classifier and `loader.SUPPORTED_SUFFIXES` already answers it
without a model.

**`document_category` keeps its name and loses its derivation.** Every consumer
tests set membership and none reads how the value was arrived at, so moving the
derivation from the filename to page one leaves `_planning_relevant`,
`analysis_profile`, `transaction_evidence`, the planning and APM context
selectors and the document listing untouched, and preserves the
planning/evidence disjointness by construction. Content-blind intake ends, which
is taken deliberately: in the code that property was framed as an accuracy
caveat rather than a policy, and reading page one is strictly more accurate than
guessing from a name.

**The domain is four values**, the same four the coarse class reads, so nothing
maps between what the model answers and what the field holds:

| value | holds |
| --- | --- |
| `policy` | policy, regulation, procedure, an authority matrix |
| `minutes` | minuted decisions of a governing body |
| `background` | every other planning document: contracts, prior reports, correspondence |
| `evidence` | transaction-level source material |

**There is no residual bucket.** A document is evidence or it is one of the
three planning values; nothing lands outside a set. That is the property to
test, rather than the classifier's accuracy on any single document — two
categories used to sit in neither, and one of them was intake's default, so a
document whose filename was uninformative was invisible to an audit run
entirely.

Intake proposes no category at all. A document arrives uncategorized, which is
in scope for text extraction where a wrong guess would not have been.


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

| Phase | Content | Gate |
| --- | --- | --- |
| 0 | Correct the doc-to-code divergence: `dynamic-cycle-contracts.md` claims evaluation is computed; it is judged | Doc matches code |
| 4a ✅ | Category domain to four values read from page one; `documents.categorized`; `corpus_scope` | Partition holds; an audit run classifies its evidence |
| 4b.1 | Master schema, one writer: the read capability takes `all_settled_then_validate`, units keyed `<type>:<document>`; asymmetric modification contract; version stamped at end of type | Field drift gone: one name per fact across a type |
| 4b.2 | Evidence with no catalogued type read anyway; the read coins a `local.` type carrying a discriminator | Nine broker notes carry one vocabulary, not nine |
| 4c | Late-field re-sweep over the documents that preceded the addition | Absence means "not stated", never "not asked" |

No new barrier is built. The coarse class runs under the existing parallel
barrier and the read under the existing sequential one, which keeps 4b to one
question rather than two. Type-lane concurrency — `grouped_sequential`, 18 steps
against 84 — is deferred, and is a pure scheduling change whenever the read pass
is measured to be worth it.

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


## What 4a shipped

Built, with the suite and one live engagement behind it. The code is the
authority on shape; what is recorded here is what the build decided and what it
corrected, because neither is recoverable from a diff.

**Storage settles the barrier.** The
category could not live only on the document entry — capability readiness runs
against a workspace handle that is routinely several revisions behind, which is
why type assignments moved to `Documents/.types` sidecars. It could not live
only in the sidecar either: fifteen call sites read `document.get("category")`,
and most hold a document dict with no workspace to reach a sidecar with. So the
commit writes both inside one `mutate()` — sidecar authoritative for readiness
and the evidence gate, entry mirrored for the readers that hold a dict.

**That mirror is why the capability is sequential.** The entry write lands on
the shared `documents` collection, which is exactly what `all_settled_parallel`
asserts cannot happen. Page-one categorization is independent per document and
still cannot run in parallel: independence of inputs is not independence of
commits. Making it parallel later is a storage change, not a barrier change —
it needs the fifteen readers off the entry first.

`category()` reads the sidecar and falls back to the entry, so an upload that
names a category outright stands without waiting for a model to agree with it.
The fallback cannot go stale: a value written during a run goes to the sidecar,
which wins.

**A forced regeneration does not re-ask the category.** Every other document
stage widens under `force`. This one must not: re-categorizing moves a document
across the planning/evidence partition mid-run, taking its type, its schema and
its extraction with it, so a refresh pointed at one document would invalidate
the vocabulary the rest of the corpus was read under.

**Two axes, two labels, one screen.** The documents tab had the same confusion
the import dialog did — a control labelled *Type* that set the category. The
header now carries both: **Held as** is what the engagement does with the
document and is the auditor's, editable, and their answer stands against any
rerun; **Read as** is what the document is, shown only for evidence, because
that is the only material a type is asked of. The rail nests evidence by type
for the same reason, and nothing else nests.

**The intake runner stays.** Only `document_category`
leaves it — from the response schema, the prompt, and the merge. Its remaining
outputs are all already computed, so a worker for them is a turn spent agreeing
with arithmetic; but it is retained by an explicit decision record
(`docs/agent-protocol-runner-decisions.md`) and carries a preset, an adapter and
an approval path. Retiring it is a question to ask on its own evidence.

**Four intake tests were retired rather than repaired.** They pinned what a
filename stem should be read as — `Procurement SOP Extracts` policy, a dealing
ticket voucher rather than the `other` a P2P-shaped vocabulary once made of a
whole treasury sample. They defended an answer to a question intake should not
have been answering, and `TRANSACTION_EVIDENCE_FILENAME` went with them. One
test replaces them: intake proposes nothing.

### The defect it repaired

Measured before the change, and the reason the scope fix was not optional. In an audit
run, `document_scope_mode` is `planning`, `resolve_document_scope` selects only
`_planning_relevant` documents, and that set is disjoint from the evidence
category by construction. Text extraction and classification were both bounded
by it:

```
mode='planning'
  scoped ids     : ('b645c5ecf1',)          # the policy document only
  classify units : []
  ready          : satisfied
  types_for_induction: []
```

`documents.schemas_induced` reported **satisfied having induced nothing**, so
both Phase 8 edges into `planning.rcm_ready` were satisfied by an empty
vocabulary and the RCM was written against fields no document stated. The
end-to-end induction tests run the *documents* workflow, whose scope is `all`,
so the audit path was untested; it worked only where an auditor had run a
standalone document pass first. `corpus_scope` now runs text, categorization and
classification over every document. What a document *is* is not a planning
question.

The cost that follows is real and is the open question below: reading a document
commits a revision, so importing evidence re-opens the planning chain.

### What the live run showed

`docs/sample-treasury-min`, the policy and one deal pack, on a configured model:

```
01_Treasury_and_Investment_Policy.docx        policy     -                       standard    0 records
CNF-2025-0166_Counterparty_Confirmation.pdf   evidence   fx_contract             structured  1 record
PMT-2025-00133_Payment_Instruction.pdf        evidence   payment_instruction     structured  1 record
STL-2025-0133_Nostro_Account_Statement.pdf    evidence   bank_statement          structured  1 record
TD-2025-0166_Dealing_Ticket.pdf               evidence   treasury_deal_ticket    structured  1 record
```

The gate holds where Phase 9 found it broken: the policy read as prose, the four
vouchers under their own type's fields, both axes answering yes.

Two things it says about the phase after this one. Every schema came back
`low_confidence` — a minimal pack carries one document per type, so the
two-sample check cannot run, which is the documented allowance doing its job.
And `fx_contract` induced **29 fields from one document**, against 44 from three
samples of one identical template in the full corpus. Same shape at a fifth the
size: what stops one document's phrasing becoming the type's vocabulary is a
master that accumulates, not a wider sample.


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
- **Whether planning should hold its ground.** Reading a document now commits a
  revision, which re-opens the planning chain. Correct — the corpus changed —
  but a workspace carrying only evidence has no planning material to
  re-synthesize from, and its context capability fails rather than keeping what
  it had. The question belongs to the planning capability rather than here.

Open against the deferred phases rather than the next one:

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
