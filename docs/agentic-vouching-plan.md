# Agentic vouching: collapsing the pipeline

This plan supersedes the pass structure in `docs/dynamic-cycle-contracts.md`.
The contracts there are sound; what this changes is *when* the vocabulary is
frozen. Today it is frozen before the evidence is read, and everything
downstream must conform to a guess made from two or three samples. The result is
a large body of machinery whose only job is to detect and repair the mismatch.

Two rules from that document are withdrawn:

| Withdrawn | Replaced by | State |
| --- | --- | --- |
| "An LLM never decides an item's outcome" | A model reader reaches verdicts; the auditor reviews and overrides | Verdicts shipped; the per-verdict override has not |
| "Evaluation stays code" | A model evaluator judges agreement on raw values | Shipped |

Both are already what the code does — see *What the code already does*. Neither
was withdrawn to see what the architecture would look like without it: the first
real engagement withdrew them, by showing that a comparison operator chosen when
the matrix was written reports a currency prefix as an exception. They remain
provisional in the sense that matters — they are to be re-evaluated against a
real engagement rather than settled here — and the open questions at the foot of
this document say what would settle them.

## Scope

This plan is cut. The read half — 4a, 4b, 4c — lands against the pipeline
`docs/dynamic-cycle-contracts.md` already describes: approved join keys, code
resolution, the ruleset review screen and the staleness family, all retained and
all still doing their work. **4a is built**; what remains is the master schema
and its late-field sweep.

**Neither withdrawal above is exercised by any of it.** Nothing in 4a–4c puts a
model in the verdict slot or replaces a join with a search — the verdict slot was
already filled before this plan was written, by a change the engagement forced
rather than one proposed here. What the withdrawals contribute to this tranche is
direction: they are the reason the later phases are shaped the way they are.
Phase 0 — correcting the doc-to-code divergence — was separable and is done.

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
engine by `fieldwork.cycle_vouch`. What the engine settles is *resolution*
(`ambiguous`, `missing_evidence`, `invalid_extraction`), deterministically and
with no model call, and a one-sided assertion is settled entirely there: that a
field was stated is answered by reading it. The six comparison operators and the
per-assertion tolerance are gone — deleted, not carried over — and an assertion
now states a `requirement` in words instead. The result carries a `reason` for
"why the reader reached this verdict, in its own words", and `judgment_request`
sends **raw** values rather than normalized ones, for a reason it states
plainly:

> "presentation carries the difficulty — a currency prefix, a vendor code, a
> scanned date — and a reader handed the folded value would be answering an
> easier question than the one the documents pose."

That is the `Rs. 2000` vs `2000` argument, already in the code.

**So the second withdrawal is not a proposal at all — it already happened.** It
was not taken to see what the architecture would look like without the rule; it
was forced by the first real engagement, where the operator was wrong more often
than right and every presentation difference was filed as an exception. The
first withdrawal went with it, since a judged verdict *is* a model reaching an
item's outcome. What is left of both as proposals is the auditor's per-verdict
override, and the direction they point the later phases in.

The doc claiming "evaluation stays code" is therefore a doc-to-code divergence
of some standing. Correcting it is Phase 0, and it is done:
`docs/dynamic-cycle-contracts.md` now records the split it actually implements
and carries a *Phase 10 notes* section for the change that forced it.

## Target architecture

```
intake      file suffix, no model                     route: table | document
read (1)    page 1 -> coarse class                    policy | minutes | background | evidence   built
read (2)    whole evidence document, text + images    records + citations, master schema per type 4b
stamp       per type, once the type is read          the schema, and the readings stamped to it  4b
assemble    one agentic pass per sampled item         role bindings + resolved operands           deferred
judge       model reader on raw values                verdict + reason                            built
            the auditor overriding a verdict directly                                            deferred
```

Two model stages become one per document and one per item. The frozen artifacts
move from *before* the evidence to *after* it.

Row one is built. Row two is 4b. Row three is deferred, and until it lands the
graph is still built by approved join keys applied in code.

**Row four is already built, which is the one place this table describes the
present rather than a target.** `fieldwork.cycle_vouch` judges agreement on raw
values and returns a verdict with a reason; `cycle_linking.evaluate_cycle_item`
decides resolution in code and takes the verdict from outside. What is deferred
there is narrower than the row makes it look: the auditor's control is currently
the item disposition — `confirmed` or `exception` — and not a per-assertion
override. That is a real difference. A disposition records what the auditor
concluded about the item; an override would record that the reader was wrong
about one check, which is the thing a reviewer will want as soon as they
disagree with a single cell.

The sections on assembly and replay below describe the target rather than
anything being built now, and are kept because 4b's shape is chosen to make them
reachable. The judging section describes what shipped.

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

**The two buckets take different paths, and the routing question gets simpler.**
Planning material keeps the existing chunked prose analysis, unchanged and still
parallel. Evidence leaves that path entirely for a whole-document read.

Today `analysis_profile` asks two questions — is this document's category
transaction evidence, and does its type have an induced schema — and routes to
the structured profile only when both say yes. The second question disappears.
A schema is now an *output* of reading the evidence rather than an input to it,
so there is nothing to ask at routing time and the category alone decides which
path a document takes. That also removes the circularity Phase 9 had to work
around: no gate in front of induction, because there is no induction to gate.

**One unit per document; the read does not chunk.** Evidence documents are
short — a dealing ticket, a payment instruction, a confirmation — and the
existing chunk window is 24,000 characters, so chunking an evidence document
almost never splits it and buys a per-chunk unit for nothing. Reading the whole
document in one call is also what makes the master coherent: a master that moved
between chunk 1 and chunk 2 would produce one document whose records were read
under two vocabularies, which is the drift this design removes, relocated inside
a single document.

The window is bounded and the bound is loud. A citation binds to text the worker
saw, so an evidence document exceeding the read window is reported as
over-window rather than silently truncated — the same rule the chunk budgets
already keep. `induction_text` is not the accessor for this: it is a 3-page,
12,000-character sample window, and a read that produces citations across a
document needs the document.

**The read takes both modalities, because a scanned page is where the control
signature lives.** This is the one place "one unit per document" costs something
real, and paying it is not optional.

Page routing is independent of the text profile. `_visual_page` sends a page to
the image worker when the source is a standalone image, when the page is
`image_only`, when a PDF page has `no_usable_text_no_image`, or when the auditor
asked for full coverage — and `chunk_specs` then *excludes* those pages from the
text chunks. So a scanned page contributes no text at all. An evidence document
today emits structured text units and visual page units side by side.

A text-only read would therefore fail in two ways. A fully scanned confirmation
yields no text, contributes nothing to the master, and produces no records —
silently. And the commoner case is worse: a mostly-digital PDF with one scanned
signature or stamp page is read for everything except the page the control is
on.

That second case is the late-field failure arriving by a different route. The
master's value rests entirely on absence meaning *the document does not state
this*. If a page was never read, absence means *nobody looked at that page*, and
`second_approver` on 3 of 18 payment instructions — seeded exception D5 — is
exactly the kind of field that lives in a signature block. A design with 4c in
it cannot leave the same hole open by modality.

So the read unit's context declares two sources — `raw_pages` for the text and
`page_image` for the document's visually-routed pages — with
`allow_document_text` and `allow_document_images` both set, the way
`documents.analysis_visual_page` already does. A preset is a tuple of
independent sources with their own representations, so this is a declaration
rather than new machinery. `document_visual_page_analysis` stops expanding for
evidence; the `.docx` image-only case still reports
`document_visual_source_unsupported`, because an unreadable page must be stated
rather than skipped.

**The visual bound is part of the read's one bound.** Media is the expensive
half: the per-page visual budget is 4 items and roughly 16k image tokens, and
`MAX_VISUAL_PAGES` is 20 — so a document at that cap would carry 80 images into
a single call, which is not a call anyone should make. The read therefore takes
a much lower per-document visual cap than the page-at-a-time path needs, and a
document exceeding **either** bound — characters or visual pages — is over-window
and reported. One rule covers both, which is the point: the read either saw the
document or it says it did not.

**Unit identity has to cover the prepared sets.** A visual unit today carries
`prepared_set_identity` — a hash of source, page, frame and preparation policy —
so re-preparing a page re-runs its analysis. The read unit carries the ordered
list of those hashes in its input, which keeps the same interlock: a preparation
policy change moves `input_sha1` and the document is read again rather than
being reduced under images it never saw.

**And it removes a paraphrase nobody wanted.** The reduction's local
concatenation path requires *every* proposal to be `analysis_profile:
"structured"`; a visual proposal is not, so an evidence document with one
scanned page currently loses the fast path and spends a model turn rewording
records that were already exact. With one unit per document there is no mixed
set to break the check.

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

**The master schema.** One per document type, supplied to every document of that
type as prior art. A document reads its own content, sees what its predecessors
settled on, and reuses those names. This is what removes field drift, and it
removes it at the point the fact is first observed rather than by reconciling
afterwards: `approved_by_id` and `approved_by_employee_id` cannot both enter a
master that already holds one of them.

Per-document calls can only agree if they are not independent. Serializing them
per type and handing each the accumulated master is what makes agreement
possible at all.

**It is persisted, and it is not a schema.** An earlier draft called it "working
state for the run", which does not survive contact with the mechanism it depends
on: a serialized unit sees its predecessor's work by rebinding against
*committed workspace state*, so state that never lands on disk is state the next
document cannot be shown. The master therefore has its own store —
`DocumentMasters/<document_type>.json` — beside `DocumentSchemas/` and written
the same way the other side stores are.

Keeping it a *separate artifact* from the schema is what resolves the
contradiction the first draft carried. `save_schema` bumps `schema_version` on
any change of meaning, and that bump is the entire staleness mechanism:
`is_current` compares version and hash, and `schema_ref` stamps are what
`has_usable_analysis` and `cycle_measurement.structured_records` read. A master
mutating in place under a fixed version would either fire staleness on every
document or, worse, hold a version steady while its content moved — which is not
staleness, it is a stamp that lies. So the master accumulates in its own file
under its own `master_ref` content hash, and `DocumentSchemas/` is written
exactly once per type per run, by the stamp. `save_schema` needs no new mode and
the staleness family is untouched.

```json
{
  "document_type": "payment_instruction",
  "master_ref": "sha256:...",
  "documents_read": ["doc-0f21", "doc-13c8", "..."],
  "fields": [
    {"name": "approved_by_id", "role": "control", "value_type": "identifier",
     "cardinality": "one", "verbatim": true,
     "fill_count": 14, "introduced_at": 0}
  ],
  "renames": []
}
```

**A reading carries `master_ref` until the type is stamped, then `schema_ref`.**
The read commits the same structured analysis artifact the corpus already
consumes — so the documents tab, the citation catalogue and
`document_analysis.inventory` need nothing new — but it cannot carry a
`schema_ref` at read time, because the version it would name does not exist
until the type is done. It carries the master's content hash instead. The stamp
adds the `schema_ref`, and until it does the reading is a reading and not yet
evidence.

`introduced_at` is the index into `documents_read` at which a field first
appeared. That one integer is what makes the late-field sweep computable
without hash archaeology: the fields document *i* was never asked about are
exactly those with `introduced_at > i`.

**Both calls run sequentially, and only one of them does so for a reason this
plan cares about.**

Call one was drafted as parallel — nothing about a page-1 coarse class depends
on any other document, so it looked like it qualified for
`all_settled_parallel` on the grounds chunk analysis does. It does not, and 4a
established why: the commit mirrors the category onto the shared `documents`
collection so the fifteen call sites holding a document dict keep working, and
two units landing at once would race on it. **Independence of inputs is not
independence of commits.** `documents.categorized` therefore ships sequential
(`capabilities/documents.py`), and making it parallel later is a storage change
— getting those readers off the entry — not a barrier change. See *What 4a
shipped*.

The distinction is worth keeping straight, because the two calls are sequential
for unrelated reasons and only one of them is load-bearing here: call one's is
incidental and removable, call two's is the mechanism.

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
`evidence_read:<document_type>:<document_id>` puts a type's documents in one
contiguous run for free — which is what bounds the late-field sweep below. This
is the same ordering fact that cost the induction pass a rebuild:
`document_schema:x` sorts before `document_schema_sample:x:y` because `:`
precedes `_`, and the freeze ran while its samples were still queued.

The contiguous run is a convenience, not the stamp's correctness condition. The
stamp is a dependent capability, so it runs after the read stage has settled
whatever the sort produced — the ordering makes the master accumulate in a
sensible order, and the dependency edge is what makes it complete.

**The accepted cost is wall time on the read pass.** Fully serialized, the
treasury corpus is 84 steps against 21 for today's
`84 / max_llm_concurrency`.

Across both calls the corpus is 168 sequential steps, because 4a's
categorization is serialized too. Half of that is already being paid — 4a
shipped — and it is the half that can be bought back cheaply, since its
serialization is a storage artifact rather than a semantic one. If the read pass
turns out to need wall time back, the categorization pass is the first place to
look and the cheaper of the two to fix.

Type-lane concurrency would be 18 — the largest
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
| Split or merge | Not expressible in 4b.1. A split composes from a rename plus an addition; a merge needs removal, which nothing in the read may do. See *The response contract*. |
| Remove a field | Never. The master only grows. |

Without the asymmetry, "the agent may revise the master" is the drift mechanism
relocated rather than removed: every document has some reason its own phrasing
reads better, and eighteen sequential renames is the result.

**The asymmetry is enforced by cost, not by judging the reason.** Whether a
proposed rename is a genuine correction or a preferred synonym is not a question
code can settle, and a validator that tried would either wave everything through
or refuse corrections that were right. So a rename is applied and *recorded* —
in `renames`, with the document that asked for it — and it re-opens every prior
reading of the type on exactly the terms a late-added field does, because prior
readings used the old name and their silence under the new one would be a lie.
A rename therefore costs what it actually costs. The rule above stays as the
prompt's instruction; the sweep is what makes ignoring it expensive rather than
free.

### The response contract

The read's submission tool is a **merge of two shapes that already exist**, not
new machinery. `_structured_submission_tool` constrains `name` to an enum of the
type's fields, so naming a field the type does not carry stops being something
the model can do rather than something a validator catches. `_schema_field`
validates a full field descriptor — role, value type, cardinality, verbatim,
confidence. The extraction worker has the first; the sample worker has the
second; the read needs both in one call, because reading a document and learning
what the type carries are now the same act.

```
submit_document_reading
  records[]        fields[]  {name: enum(master), entry, value, citation}
  new_fields[]     {name, role, value_type, cardinality, verbatim,
                    confidence, label, reason,
                    entry, value, citation}
  renames[]        {from: enum(master), to, reason}
  citations[]      as today
  audit_notes[]    as today
```

**`additional_fields` leaves the read's response, and this is the load-bearing
decision.** It exists to hold a fact a frozen vocabulary has no room for, and
under a master there is no such fact — the master takes the field. Keeping both
channels would be worse than not having the master at all: one asks for a full
descriptor and a reason, the other asks for a name and a value, and a model
offered both will reach for the cheap one every time. That is the drift this
design removes, reintroduced as a shortcut.

**A field enters the master only by being filled in the document that
introduces it.** That is why `new_fields` carries `entry`, `value` and
`citation` alongside the descriptor rather than declaring a field in the
abstract. The invariant it buys is worth stating plainly: **the master never
holds a field no document ever stated.** Today's induction has no such guarantee
and the gap is measured — the RCM authoring turn chose `fx_contract.received_date`
at 0 of 11, a field the samples proposed and the corpus did not carry. Under
this contract a zero-fill field cannot exist, and a new field's `fill_count`
starts at 1 by construction.

**A renamed field's value travels under the old name.** The enum is fixed when
the call is made, so a field being renamed in this same response cannot appear
in `records[].fields[]` under its new name. `renames` names the master field;
the value goes in `fields[]` under the name the enum offers; the commit applies
the rename to the master and to the incoming record in that order. This keeps
the enum static, which is the property that makes it enforceable at all.

**Split and merge are not expressible in 4b.1**, and the modification table
above promises more than this contract delivers until they are. A split is a
rename plus a new field and could be composed today; a merge requires *removing*
a master field, and removal is not in the contract at all. Nothing in the read
may delete a field: document 18 erasing what document 1 read would leave every
earlier reading holding a value under a field the vocabulary no longer explains.
A field one document got wrong is a correction for the auditor, not something
the next document may quietly retract.

**Validation is the existing pair, in the existing order.** Declared
descriptors go through `_schema_field` at the worker boundary — same role, value
type and cardinality vocabularies, same errors — and the resulting master goes
through `document_schemas.validate_fields` at commit, which is what enforces
unique names and a non-empty field list. No third validator, and no new
vocabulary: `FIELD_ROLES`, `VALUE_TYPES` and `CARDINALITIES` are unchanged.

**`role` is required and never defaulted, because `identifier` is the expensive
one.** A join key may only address an identifier field, so a field declared
`identifier` changes what the corpus can be joined on, and one declared
`attribute` by omission is a join the engagement can never make. This is the one
place in the descriptor where a silent default would cost an assertion.

**Two of today's inputs go.** `sole_chunk` is always true when the unit is the
document, and `schema_sampled_this_document` has no referent once there is no
sample. Both exist to make an empty extraction a contradiction rather than a
quiet page; that job now belongs to the read being whole-document by
construction. An empty `records` array stays a complete answer — a transaction
document carrying no record is a truthful reading and must not be forced to
invent one.

**A value read from a page image cites its page.** The read sees both
modalities, so a citation may point at a page that contributed no text. The
excerpt is then what the worker transcribed from the image, which is the same
rule as ever — a citation binds to what the worker actually saw — and the visual
worker already returns citations in this shape.

**The version is stamped once, when the type's documents are done, by its own
capability.** During the read there is no version at all — nothing to bump, so
the staleness family has nothing to detect. This makes the failure that cost
this engagement three recovery runs — a re-derivation orphaning 65 completed
extractions as `stale_schema_reference` — structurally impossible rather than
merely unlikely. The stamped version is provenance, not a contract: *this is
the vocabulary that emerged from reading these N documents.*

The stamp is a separate capability rather than the last unit of the read, and
the reason is the one Phase 3 already paid for. Units within a stage execute in
sorted id order, so a capability holding both the readings and the freeze binds
the freeze *first* — `document_schema:x` sorts before
`document_schema_sample:x:y` because `:` precedes `_` — and reads back nothing.
Making the stamp a dependent capability is what makes the ordering something the
scheduler honours rather than something a sort order has to be trusted for.

The stamp takes no model turn. It reads the finished master, calls
`save_schema` once, and back-stamps the type's readings with the resulting
`schema_ref`, all through `commit_local` — the same shape the freeze binder
already uses when its samples agree.

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

**Fill counts travel with the master.** The response contract removes the
zero-fill field — nothing enters the master without being stated by the document
that introduced it — but it cannot remove the *one*-fill field, and that is the
one that stays dangerous. A single internally-produced confirmation genuinely
contributes `printed_by_name`, `printed_on` and `status` to its type: harmless
as a hint, misleading as a selector. `fx_contract.received_date` at 0 of 11
became a comparison because the authoring turn saw names without frequencies,
and 1 of 18 would have read exactly the same to it. So the counts added to
`schema_catalog` stop being a refinement and become what keeps an accumulating
master usable.

**What goes, and what stays.** Deleted with the freeze: the reconcile call,
union-never-intersect, escape rate, and `sample_for_induction`. All of it exists
to police a guess made from two or three samples, and there is no guess left to
police.

The `schemas_sampled → schemas_induced` **edge pair survives**, which an earlier
draft had deleting. Its shape is already the shape this needs — a fan-out that
reads, then a dependent per-type step that settles — and it is the shape Phase 3
arrived at by getting it wrong once. Three capabilities depend on the second
node (`analysis_chunks_ready`, `planning.rcm_ready`,
`tests.cycle_ruleset_proposed`), so keeping the pair means the graph is rewired
in one place rather than four. What changes is the content of both nodes, and
their names, because "sampled" on a pass that reads every document is the
divergence class this plan exists to remove:

| Today | Becomes |
| --- | --- |
| `documents.schemas_sampled` — 2–3 sampled documents per type, parallel, reading fields only | `documents.evidence_read` — **every** evidence document, serialized, keyed `<type>:<document>`, reading records *and* contributing to the master |
| `documents.schemas_induced` — unions the samples, reconciles conflicts, freezes | `documents.schemas_stamped` — one unit per type, no model turn: `save_schema` on the finished master, then back-stamp the type's readings |

### The graph after 4b.1

```
documents.text_ready
  └─ documents.categorized          page 1 -> four-value category        sequential   4a ✅
      └─ documents.types_classified fine type, evidence only             sequential   built
          └─ documents.evidence_read one unit per evidence document      SEQUENTIAL   4b.1
              └─ documents.schemas_stamped  one unit per type            sequential   4b.1
                  ├─ documents.analysis_chunks_ready  planning prose     parallel     built
                  │   └─ documents.analysis_generated                                 built
                  ├─ planning.rcm_ready
                  └─ tests.cycle_ruleset_proposed
```

Two edges move. `analysis_chunks_ready` **loses** its schema dependency and
keeps only `text_ready`: under 4b.1 it carries planning prose, which needs no
vocabulary, and leaving the edge would make every policy summary wait on the
whole evidence read for nothing. Its unit generation excludes transaction
evidence entirely — both modalities, text chunks and visual pages alike — which
now has its own pass. Planning material keeps the visual path unchanged.

`planning.rcm_ready` and `tests.cycle_ruleset_proposed` keep their edges and
those edges get more expensive — this is the plan's stated cost, that the matrix
is written against the complete vocabulary of the corpus rather than one guessed
from three samples.

**A failed read leaves the type with no vocabulary and no evidence, loudly.**
This settles an open question the plan had left standing. A stage with any
failed unit folds to `failed`, so a read that dies at document 9 never reaches
`schemas_stamped`; no `save_schema` runs, the type has no current schema, and
its readings keep their `master_ref` and are never stamped into evidence. That
is the right answer and it falls out of the structure rather than needing a
rule: a master built from eight of eighteen documents is not the type's
vocabulary, and the eight readings are not lost — they are on disk, and the
re-run resumes against them.

**Where the step count actually lands.** Fully serialized the treasury corpus is
84 categorizations and 84 reads plus one stamp per type. Measured against today
rather than against nothing, the trade is narrower than "84 against 21": the
sample pass and its reconcile calls disappear, and the per-chunk structured
extraction they fed disappears with them. Total model calls go slightly *down*;
what is spent is wall-clock, because the reads no longer overlap.

### Regenerating: two actions, not one

`force` has to split, because under a master the button that exists today is
being asked two different questions and can only answer one of them.

**Why the current compromise stops working.** `_pending_types` scopes forced
re-derivation to the targeted documents' own types, plus any type with no schema
at all. That scoping was bought expensively — force used to re-derive every
schema in the workspace, so a one-document refresh spent its whole allowance
re-sampling schemas it was never pointed at, failed on the turn limit, and
bumped every schema a version, orphaning 68 completed extractions. The promise
it makes is *a one-document refresh does what its button says*.

That promise rests on a schema coming from a **sample**, which makes re-deriving
one type a couple of documents' work. A master comes from every document of the
type, in order, so "re-read this document" and "possibly move the vocabulary"
are no longer separable and there is nothing left for the scoping trick to
scope. Any force that may move the master is type-scoped work by nature.

**So name the two questions and give each its own action.** This is the
distinction the Phase 2a note already draws between retyping a document and
coining a type — two operations that read alike and are opposites, one changing
what a *document* is and the other changing the *vocabulary*.

| Action | Scope | The master |
| --- | --- | --- |
| `refresh` — *re-read this document* | the targeted documents | **frozen**. A field the read wants to add is reported, not applied |
| `revise_vocabulary` — *re-read this type* | every document of the targeted documents' types | **rebuilt** from the pass, in order, and re-stamped |

**`refresh` stays cheap, and stays cheap by construction.** The document is
re-read under exactly the vocabulary its siblings were read under, so nothing
about them is disturbed. The stamp still runs and costs nothing:
`save_schema` returns the prior record unchanged when the meaning has not moved,
which is the same no-op rule that keeps re-inducing an identical schema from
bumping its version.

**Its refusal is the useful part.** A refresh is asked for because something
looks wrong, and one thing that can be wrong is that the master has no place for
what this document states — which a frozen-master re-read cannot fix and would
otherwise fail at silently, reading the document a second time under the same
blind spot. So the addition is reported rather than dropped, and the report
names `revise_vocabulary` as the action that would take it. The cheap action's
job is to do the common repair and to recognize the case it cannot handle.

**`revise_vocabulary` is an ordinary read pass over a narrower corpus.** It
rebuilds rather than appends: reading the type from the start in order is what
keeps `introduced_at` meaningful and the sweep bounded, where appending to an
existing master would leave indices that no longer describe what any document
was asked. Because it is a normal pass, 4c's late-field sweep applies to it
exactly as it applies to the first one — there is no second mechanism.

The scope widening reuses machinery that exists. `_pending_types` already
computes the targeted documents' types; `revise_vocabulary` feeds that set back
into the *document* scope — every document of those types — instead of using it
to scope schema derivation alone.

**What this costs the auditor is honesty about price.** Re-reading eighteen
payment instructions to fix one document's vocabulary is expensive and it is
what the repair actually costs; the failure to avoid is a small button quietly
doing it, which is the shape of the defect `_pending_types` was written to
remove. Neither action surprises anyone: one is a document, one is a type, and
the expensive one is only ever reached deliberately.

Retained, against this plan's later phases: `schema_hash` provenance,
`schema_ref`, `is_current`, `stale_schema_reference`. These are the interlock
that stops values being reinterpreted under fields they were never read against
— `has_usable_analysis` and `cycle_measurement.structured_records` both depend
on them — and while evaluation still runs over declared join keys they are
load-bearing. What changes is that they go quiet: with the version stamped once
per type per run, nothing goes stale mid-run and the family has nothing to fire
on. They are deleted in Phase 5, after assembly replaces the join, not here.

`additional_fields` **leaves the read's response contract in 4b.1** and survives
only as a storage shape, for records already written under it. Its purpose was
to hold a fact the frozen vocabulary had no room for; under a master that grows
there is no such fact, because the master takes the field. Removing it from the
response is not cleanup and cannot be deferred — leaving both channels open lets
a model take the one that asks for a name and a value over the one that asks for
a descriptor and a reason. Deleting the *storage* shape is cleanup, and is not
part of this tranche.

### Implementation notes

Smaller than the decisions above and each with a failure mode behind it. They
are here because every one of them is the kind of thing that gets rediscovered
one at a time, at cost.

**The read needs its own skip predicate.** `has_usable_analysis` asks two
questions — does the stored `schema_ref` still match the live schema, and does
the type stamped on it still match the assignment — and an unstamped reading has
no `schema_ref` to answer either with. So it is not "usable" by that test, and a
read whose units are generated from it would re-read every document on each
re-expansion within the same run. It gains a third state: a reading exists, it
is not yet evidence, and the read is done with it. The two existing questions
keep their meaning for stamped readings, and the reason they were added holds
unchanged — readiness that does not ask them reuses a capability whole before
any unit runs, which is how an engagement once completed with no usable cycle
evidence at all.

**`preparation_model_turns` has to be recounted.** It is
`classifications + samples + freezes`, sized from `sample_for_induction`, and it
exists because classification and schema turns were model-backed stages sitting
entirely outside the document budget's arithmetic. Under 4b.1 the terms change
to classifications plus *every evidence document* plus one stamp per type — a
much larger number. Leaving it stale reproduces the failure it was written for:
a run that spends its allowance before reaching the analysis it was asked for.

**One `mutate()` per read.** The reading commits three things — the analysis
artifact, the master update, and the `documents_read` append that assigns this
document's `introduced_at` — and they have to land together. A partial write
leaves indices that no longer describe what any document was asked, which does
not fail; it makes 4c sweep the wrong set, silently.

**`analysis_profile` becomes dead code, not a demoted one.** It has two callers.
`analysis_unit_specs` uses it to pick the text kind, and that routing question
disappears; `_warn_unstructured_vouchers` reports evidence that fell to prose,
and has no referent once evidence has its own pass and its own readiness. Both
go, and the category question moves into `evidence_read`'s unit generation,
where it is the only question asked. The Phase 9 lesson survives the deletion —
type says what a document *is*, category says whether the engagement holds it as
transaction evidence — but only one of the two gates is still needed, because
the schema half is now an output.

**`types_awaiting_schema` and `types_for_induction` need their counterparts.**
Three readiness and unit functions call them today. `evidence_read` expands over
evidence *documents* rather than types; `schemas_stamped` expands over types
carrying a master with no current schema. Same shape, different predicate, and
`types_present` keeps its job unchanged — reporting every type the corpus
carries is a classification fact and is not the same as extracting against it.

**`documents_read` appends on resume and resets only on `revise_vocabulary`.**
A run that resumes mid-type must append: the readings already taken stand, and
the indices they were assigned still describe what they were asked, so
renumbering them would falsify the sweep. `revise_vocabulary` is the one case
that rebuilds, and it rebuilds precisely because it is re-reading from the start.

**`low_confidence` survives with a different reason.** `save_schema` takes the
flag and a one-document type still deserves it, but not because a two-sample
agreement check could not run — there is no such check. It means one document's
phrasing is this type's entire vocabulary, which is the same warning the live
4a run produced when `fx_contract` induced 29 fields from a single document.

**`escape_rate` is already dead and its deletion is free.** It is defined in
`document_schemas.py`, carries 7 tests, and is called from nowhere in
`backend/app/` — no route serves it and no component renders it. The Phase 6
plan in `docs/dynamic-cycle-contracts.md` said the documents tab would show
escape rate per type; it never did. So removing it re-points nothing, and the
documents tab has no per-type vocabulary surface at all today — which is where
fill counts should go, since they are what makes an accumulating master
readable by the person who has to trust it.

**`capabilityLabels.ts` carries the two renamed rows**, alongside
`documents.categorized` and `documents.types_classified` which keep theirs.

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

*Built, except the override. It landed ahead of this plan, forced by the first
real engagement rather than chosen here — the operators were reporting
presentation differences as exceptions. `docs/dynamic-cycle-contracts.md`,*
Phase 10 notes *records it.*

Raw values through the existing `judgment_request` shape, verdict plus reason.
Two things differ from what this section originally proposed, and both are
improvements worth keeping:

**One call per item, not one per assertion.** The reader needs the other
documents in front of it to tell a presentation difference from a real one, and
asking cell by cell spends a call to answer each half of a comparison.

**Three verdicts, not two.** `cannot_determine` is a real answer and the prompt
defends it against being guessed away: a scanning ambiguity between `P02024004`
and `PO2024004` may be one reference or two, and an audit recording an untested
check as passed is worse than one recording it as untested.

**The override is what remains.** Today the auditor's control is the item
disposition, which is a conclusion about the item rather than a correction to
one cell. The rule this plan wants is the one `assigned_by` already enforces for
classification: an auditor verdict is final and is never overwritten by a rerun.
Nothing about the shipped shape blocks it — the verdict is a field on the
result, and the result already carries who and what produced it.

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
| 0 ✅ | Correct the doc-to-code divergence: `dynamic-cycle-contracts.md` claimed evaluation is computed; it is judged. The operators and tolerances it listed as retained were deleted; assertions state a `requirement` instead | Doc matches code |
| 4a ✅ | Category domain to four values read from page one; `documents.categorized`; `corpus_scope` | Partition holds; an audit run classifies its evidence |
| 4b.1 | `documents.evidence_read` — one sequential unit per evidence document, text and page images in one call, no chunking, keyed `<type>:<document>`, accumulating `DocumentMasters/<type>.json`; `submit_document_reading` merges value-against-enum with field-descriptor and drops `additional_fields`; `documents.schemas_stamped` calls `save_schema` once per type and back-stamps the readings; `refresh` and `revise_vocabulary` split | Field drift gone: one name per fact across a type; no field the corpus never stated; a scanned approval page is read, not skipped; a one-document refresh does not silently re-read eighteen |
| 4b.2 | Evidence with no catalogued type read anyway; the read coins a `local.` type carrying a discriminator | Nine broker notes carry one vocabulary, not nine |
| 4c | Late-field re-sweep over the documents that preceded the addition | Absence means "not stated", never "not asked" |

No new barrier is built. Both passes run under the existing sequential barrier —
the coarse class because 4a's category mirror commits to the shared `documents`
collection, the read because the master has to accumulate — which keeps 4b to
one question rather than two. Type-lane concurrency — `grouped_sequential`, 18
steps against 84 — is deferred, and is a pure scheduling change whenever the
read pass is measured to be worth it.

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

That holds for **both** of 4b.1's actions. Neither `refresh` nor
`revise_vocabulary` re-asks the category: the first is a question about one
document's extraction and the second about one type's vocabulary, and a document
that changed category would belong to neither pass. Correcting a category stays
the auditor's *Held as* control, which is where a decision that moves a document
across the partition belongs.

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

Each needs a decision before its phase.

**Settled by the 4b.1 design** — *what a failed read does mid-type.* Splitting
the stamp into its own capability answers it. A stage with any failed unit folds
to `failed`, so a read that dies at document 9 never reaches
`documents.schemas_stamped`: `save_schema` does not run, the type has no current
schema, and its eight readings keep their `master_ref` and are never stamped
into evidence. Nothing is lost — the readings are on disk and the re-run resumes
against them — and nothing records a partial vocabulary as complete. See *The
graph after 4b.1*.

- **Where the read's visual cap sits.** The page-at-a-time path allows 20 pages
  at 4 images each; one call cannot. Evidence documents are short, so a low cap
  costs nothing on this corpus and the over-window report catches the rest — but
  the number should come from measuring the engagement's evidence rather than
  from a guess, and it interacts with the character bound rather than sitting
  beside it. Worth setting once against real page counts, and worth revisiting
  the first time an engagement carries long scanned evidence.
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
  is 84 reads where today's chunked extraction overlaps. Total model calls go
  down — the sample pass and its reconcile calls are gone — so what is being
  spent is wall-clock, which is an acceptable trade for one engagement and an
  obvious one to revisit; what is unsettled is the threshold. Two answers
  exist and neither is foreclosed: a `grouped_sequential` barrier, which is a
  scheduling change touching no stored data and no unit contract; or batching
  several documents into one call, which keeps one writer while cutting the step
  count but needs a rule for a batch that disagrees with itself. A third answer
  applies only to the coarse class, which is serialized for a storage reason
  rather than a semantic one: get the fifteen readers off the document entry and
  it becomes parallel with no barrier work at all, recovering 84 of the corpus's
  168 steps.
- **Whether planning should hold its ground.** Reading a document now commits a
  revision, which re-opens the planning chain. Correct — the corpus changed —
  but a workspace carrying only evidence has no planning material to
  re-synthesize from, and its context capability fails rather than keeping what
  it had. The question belongs to the planning capability rather than here.

Open against what already shipped, and therefore open now:

- **Judged verdict stability.** Same values, same verdict. There is no property
  test for it, and the reader is already in the verdict slot on every two-sided
  assertion — so this is owed against the current code, not against a later
  phase. What makes it tractable is that the judged half is narrow: resolution
  is deterministic, and the only question asked of the model is whether two
  values state the same fact.
- **The per-verdict override.** The auditor disposes of the item; they cannot
  yet say the reader was wrong about one cell. Until they can, disagreeing with
  a single check means dispositioning the whole item around it.

Open against the deferred phases rather than the next one:

- **Cost per item.** Vouching runs over sampled items, not the 1,000-row
  population — 18 here. At engagement scale this needs a number, not a guess.
  One call per item rather than per assertion is what makes this affordable at
  all, and it is the number that decides whether judging can ever widen beyond
  a sample.
- **Assembler determinism.** Two runs over the same corpus should bind the same
  documents. If they do not, the stored binding is the record and reruns need a
  diff, not a silent replacement.
- **Where the master still earns its keep once assembly lands.** As prior art it
  is cheap and probably useful. If it turns out to bias extraction toward its own
  field names, it should go entirely.
