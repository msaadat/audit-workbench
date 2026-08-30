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
read (1)    page 1 -> coarse class                    policy | minutes | other | evidence
read (2)    by class; evidence also gets a fine type  records + citations, master schema per type
assemble    one agentic pass per sampled item         role bindings + resolved operands
judge       model reader on raw values                verdict + reason, auditor overrides
```

Two model stages become one per document and one per item. The frozen artifacts
move from *before* the evidence to *after* it.

### Intake: routing only, on file type

Intake keeps one decision and loses one. **Route** — table, document,
unsupported, ignore — stays rule-based on file suffix, because a CSV cannot be
handed to a page-1 classifier and `loader.SUPPORTED_SUFFIXES` already answers it
without a model. **`document_category`** goes: it is the part filenames cannot
support, and the current prompt admits as much — "Filenames can be suggestive
but are not evidence of document content."

Content-blind intake therefore ends. Taken deliberately: in the code that
property is framed as an accuracy caveat rather than a policy, and reading page
one is strictly more accurate than guessing from a name.

### Read: one pass, two calls, a master schema per type

**Call one — coarse class, from page 1 (4,000 characters).** Four buckets:

```
policy   minutes   other document   evidence
```

The bucket decides which prompt runs next, and nothing else. It must preserve
the planning/voucher partition: the first three are planning material, the
fourth is transaction evidence. Every consumer tests set membership
(`category in PLANNING_DOCUMENT_CATEGORIES`) and never switches on an
individual category, so collapsing seven categories into four is safe. The
partition itself is not — Phase 9 recorded what happens when policy material
reaches the structured profile and planning receives a record dump in place of
narrative.

**Call two — by bucket.** Planning material is summarized as prose. Evidence is
read into structured records, and only evidence is given a fine document type.
Nothing consumes the fine type of a policy or a set of minutes: `document_type`
reaches the rest of the system solely through `transaction_evidence` and
`types_for_induction`, both voucher-only.

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

**Documents of one type are processed in order; types run in parallel.** Types
are the natural unit of concurrency — seven here, running concurrently while
each type's documents run in sequence. The critical path is the largest type,
18 steps against 21 for today's `84 / max_llm_concurrency`. At engagement scale
the largest type becomes the bottleneck and the answer is batching several
documents into one call, never re-introducing concurrent writes to the master.

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

Deleted with the contract: `schema_hash` provenance, `is_current`,
`stale_schema_reference`, `additional_fields`, escape rate, union-never-
intersect, the reconcile call, and the `schemas_sampled → schemas_induced`
edge pair. All of it exists to police a freeze that no longer happens.

### Assemble: search replaces the join

Per sampled item, the agent searches the document index for the records filling
each role and returns bindings with citations. This is where the join problems
dissolve: the nostro statement is found by its narration, the broker note by
counterparty and amount and value date. No composite-join mechanism is built,
because there is no declared key to compose.

**The agent returns bindings and operand values. It does not return a verdict.**
That separation is what keeps the next stage reviewable.

### Judge: the reader slot, filled by a model

Raw values, one assertion at a time, verdict plus reason, through the existing
`judgment_request` shape. The auditor sees every judged item and can change any
verdict; an auditor verdict is final and is never overwritten by a rerun — the
same provenance rule `assigned_by` already enforces for classification.

### Replay: freeze the resolution, not the vocabulary

Late binding threatens replay, and replay is not negotiable. The answer is to
store what each item resolved to: which documents, which fields, which raw
values, which citations. Replay then runs over the stored binding rather than a
re-search. Non-determinism is paid once, at assembly, and captured — instead of
being re-incurred on every read.

This also makes `result_sha1` meaningful again: it hashes what was actually
compared, not the rule that hoped to compare it.

## The thing that must not be lost

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

**RCM coverage returns to intent-level.** `required_comparisons` naming
`{document_type, field}` pairs cannot survive documents that no longer share
field names. Phase 8 explicitly withdrew intent-level matching; this reinstates
it. The evidence for reopening it is the 4-of-18 assertion: selector-exactness
bought precision about a name and lost the control it was naming.

**`local.` coined types keep their value and gain a duty.** A coined type read
by the RCM as the deal record, on its name alone, put a one-document anomaly on
the anchor side of three population-wide comparisons. Coining must capture a
discriminator, and the catalog must state document counts. Both are already
built; keep them.

**Measurement of the corpus stays.** `cycle_measurement` loses join-key fan-out
and gains assembler statistics. The parity property — that what measurement
reports equals what the engine reached — is worth preserving in the new form.

## Sequencing

Each phase leaves the tree working, and the first two are reversible.

| Phase | Content | Gate |
| --- | --- | --- |
| 0 | Correct the doc-to-code divergence: `dynamic-cycle-contracts.md` claims evaluation is computed; it is judged | Doc matches code |
| 1 | Assembler prototype against the 18 known packs; compare bindings to the four working join keys | Agreement where joins work; a real answer where they do not |
| 2 | Assembler statistics and the review screen that shows them | The broker gap reads as "0 of 9" before anyone approves |
| 3 | Model reader in the verdict slot, auditor override preserved | Judged verdicts carry reasons; overrides stick |
| 4a | Coarse class from page 1; intake `document_category` removed; routing stays on file suffix | Planning/voucher partition holds; no policy document reaches the structured prompt |
| 4b | Master schema per type, serialized per type, asymmetric modification contract, version stamped at end of type | Field drift gone: one name per fact across a type |
| 4c | Late-field re-sweep over the documents that preceded the addition | Absence means "not stated", never "not asked" |
| 5 | Delete the staleness family | `schema_hash`, escape rate, reconcile, sampled/induced edges gone |
| 6 | RCM comparisons move to intent-level selectors | Coverage reported against concepts, resolved per item |

Phase 1 is the whole bet and costs about a day: the corpus, the packs and the
answer key already exist, and four join keys give a known-good baseline to
diff against. Where the assembler agrees with them, the approach is safe. Where
it disagrees — the nine broker notes and the eighteen nostro statements, which
no join can reach — the diff *is* the result.

Do not start Phase 4 before Phase 2 reports. Removing the schema removes the
last thing that makes extraction consistent across documents, and the only
compensating control is the assembler statistics.

## Open questions

- **Cost per item.** Vouching runs over sampled items, not the 1,000-row
  population — 18 here. At engagement scale this needs a number, not a guess.
- **Assembler determinism.** Two runs over the same corpus should bind the same
  documents. If they do not, the stored binding is the record and reruns need a
  diff, not a silent replacement.
- **Judged verdict stability.** Same values, same verdict. Worth a property
  test before the reader is trusted on anything but sampled items.
- **Where the induced schema still earns its keep.** As a hint it is cheap and
  probably useful. If it turns out to bias extraction toward its own field
  names, it should go entirely.
- **What the first document of a type anchors.** Serialization plus an
  add-mostly master makes order matter far less, but document one still
  contributes its names with nothing to check them against. Seeding each type
  from `sample_for_induction`'s stratified pick would remove the last of it, at
  the cost of keeping a sampling step this plan otherwise deletes. Worth
  measuring before deciding: process a type in two different orders and diff the
  masters.
- **Incremental arrival.** A document imported after its type was stamped
  either appends to the master and re-stamps, or re-opens the type. Appending is
  cheap and consistent with a master that only grows; it needs the same
  late-field sweep when the new document contributes a field.
- **Batching within a type.** The escape valve if serialization becomes the
  bottleneck at scale. Several documents per call keeps one writer on the master
  while cutting the step count, but a batch that disagrees with itself needs a
  rule for which reading wins.
