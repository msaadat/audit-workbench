# Cycle design after the APM: feasibility and mockups

**Status:** built, 5 September 2026 — the *Simplified view* page of the canvas
is what shipped. Read the *Landed as* notes under **step 5** of
[`rcm-generation-redesign.md`](../rcm-generation-redesign.md) before building
on this: three of the open questions below are answered there, and the
`sources.imported` edge this document asks for is an edge but a partial one.
The build plan is **step 5** of [`rcm-generation-redesign.md`](../rcm-generation-redesign.md),
which supersedes the file-level sketch under *Feasibility* below where the two
differ. Every claim about what the code does was read from the working tree
at commit `d8e179d` plus the uncommitted `control_type` change. Step 1 of that
document (cycle vouching out of the RCM) is what makes the question askable.

## The ask

Two things, taken together:

1. **The cycle is defined after the APM**, before the matrix, rather than
   after the matrix as `tests.cycle_ruleset_proposed` does today.
2. **The cycle is shown as a graph**: population tables and documents as
   nodes, the flow from requisition to payment across them, and the field
   relationships drawn between them the way a database relationship diagram
   draws foreign keys.

## The design reference

- **Design canvas (pan, zoom, inspect, export PNG):**
  <https://claude.ai/code/artifact/694a30dc-e11a-4cf7-b6b4-3e4e98c3a625>
- **Exact markup**, one file per artboard, in [`cycle-design/`](cycle-design/).
  Generated from `gen_cycle.py` in the session scratchpad so that every edge
  endpoint is computed from a field row rather than typed; regenerate from the
  script rather than editing the HTML by hand.

The canvas has two pages. *Simplified view* is the current direction;
*Earlier exploration* keeps the first pass for reference.

| Page | Artboard | File | What it shows |
|---|---|---|---|
| Simplified view | Cycle, scrolling view | [`Main.dc.html`](cycle-design/Main.dc.html) | The planning page: one strip that scrolls to the right, one column per step, the document above its population, orthogonal arrows |
| Simplified view | The whole strip | [`CycleStrip.dc.html`](cycle-design/CycleStrip.dc.html) | The same strip unclipped, so every arrow can be read |
| Earlier exploration | Where the stage sits | [`Pipeline.dc.html`](cycle-design/Pipeline.dc.html) | The planning branch of the dependency graph, today and proposed |
| Earlier exploration | Cycle, after the APM | [`CycleShape.dc.html`](cycle-design/CycleShape.dc.html) | First pass: shape only, with a step editor and status chips |
| Earlier exploration | Cycle, after the schemas | [`CycleBound.dc.html`](cycle-design/CycleBound.dc.html) | First pass: bound rules with a side panel for the selected join key |

### The simplified view

What the page is, after the first pass was judged too much:

- **One strip, scrolling right.** A column per step of the APM's process
  flow, in order, with the step's name as a band across the top. A step that
  holds two document types (invoice and voucher) spans two columns.
- **Document above, population below.** Each column has the step's document
  type on top and the table that records that step underneath. A step with no
  table of its own (goods receipt, payment) says in a dashed placeholder which
  table's columns hold its rows.
- **Only relationship-bearing fields.** A node lists the fields that take part
  in a rule and nothing else. The full schema is one click away on the
  document type, not on this page.
- **Orthogonal arrows, routed.** An arrow leaves the right edge of its source
  field and enters the left edge of its target field. Between neighbouring
  columns it takes a vertical track in the gutter, tracks handed out left to
  right so arrows do not cross. A relationship that skips a column rides a
  horizontal bus above the document nodes (or below the tables), one lane per
  arrow, so it never passes through a node. Four kinds: link (identifier =
  identifier, solid teal), must agree (dashed purple), population row to its
  document (solid navy, vertical, the anchor), table join (grey).
- **No side panel.** Two actions in the header: *Edit steps* and *Review
  rules*. The rule review stays the existing dialog for now; the strip is a
  reading surface.

The router is about forty lines and the same arithmetic the component would
use: a column index and a row index per endpoint, a track counter per gutter,
a lane counter per bus.

**Landed as** (`cycleLayout.ts`, `CycleStrip.vue`, 5 September 2026):

- **The anchor is the one vertical arrow.** It runs straight up the middle of
  its column from the population's top edge to the document's bottom edge,
  with the `COLUMN = field` pill on it, as the artboard draws it — not out
  through the gutter and back like a field-to-field rule. It falls back to the
  gutter only where another node sits between the two.
- **Tracks and lanes are made room for, not assumed.** The top lanes sit
  between the step band and the documents and push the documents down as they
  multiply; a table join between two populations rides a bottom bus under the
  tables, as the artboard's grey arrows do; every population sits on one
  baseline whatever its document's height.
- **Rows are ordered by the layout, not the ruleset.** The backend lists a
  node's fields in rule order. Whether two arrows into one node can be drawn
  without crossing depends on which row each enters: a short hop crosses the
  gutter at its own row and must therefore enter below every rider that comes
  down from the top bus. The layout sorts each node's rows by how their arrows
  travel, then by the rows they are joined to, before assigning tracks; the
  crossing count is a real one (a horizontal run cutting a vertical one,
  touching excepted) and a two-move-deep descent over track, lane and row
  swaps settles what the ordering leaves.
- **A node is addressed by step and name.** `po_data` is two nodes on the
  procurement strip, and keying fields by table name alone gave the goods
  receipt's placeholder the purchase order's rows. The borrowed occurrence is
  the dashed note the artboard shows, with no rows unless a rule reaches it
  there.
- **A step with two roles takes two slots** and the next step starts after
  both; the first build placed the following step over the second document.

All three are drawn at 1440 px with the `procurement` workspace as it stands:
six tables, twelve materialised joins, five induced schemas, 27 matrix rows
over four processes, two `transaction_cycle` attributes, no ruleset yet. The
documents-per-type counts are illustrative; the workspace's document index does
not record a type per document in a form this session could read.

## What exists today

| Concern | Where |
|---|---|
| The cycle definition | `cycle_rulesets.py`: roles (a position filled by a document type), one anchor (population table and column bound to a role's identifier field), join keys (identifier field = identifier field), assertions (field pairs that must agree, or one field that must be stated) |
| Its author | `agent/workers/tests.py` `LINKAGE_SYSTEM`, one turn given every induced schema and the matrix's requirements in words |
| Its place in the graph | `agent/workflows/audit.py`: `tests.cycle_ruleset_proposed` depends on `planning.rcm_ready` and `documents.schemas_stamped` |
| Its review | `frontend/src/components/doc-tests/CycleRulesetReview.vue`, a dialog opened from the document tests tab: three lists (roles, join keys with fan-out, assertions) and Approve / Reject |
| Field vocabulary | `document_schemas.py`, written at the end of the evidence read for each type; every rule is validated against it, so a ruleset cannot be stored before schemas exist |
| Table relationships | `agent/joins.py` and `workspace.joins`: inferred, measured and settled by `data.relationships_inferred` and `data.joins_ready`, with `left_on` / `right_on` column pairs |
| Process structure | The APM's *Process flow and understanding* section. On `procurement` it enumerates the four steps as a numbered list, and the matrix's `process` values are those four names |

Nothing today ties a table to a document type except the ruleset's single
anchor, and nothing ties a step of the process to either.

## The constraint

The field-level half of a cycle definition needs the induced schemas, and the
schemas need the evidence read, which is the expensive extraction pass over
every evidence document. Step 1 of the RCM redesign removed exactly that
dependency from the matrix, and the working tree's own comment above
`planning.rcm_ready` says why: a matrix invalidated by a re-derived schema was
the cost being paid.

So a single "cycle definition" stage between the APM and the matrix, defined
down to fields, would put the schema wait back in front of the matrix and
reintroduce the invalidation. That is the one shape this evaluation rules out.

## The proposal: one cycle, two layers

The cycle is one artifact to the auditor and one page in the UI, and it fills
in over the engagement in two layers with different prerequisites.

### Layer 1: the shape, after the APM

A new planning artifact, `planning["cycle"]`, alongside the scope map that
step 4 of the RCM redesign already specifies. In fact it *is* that scope map
with two additions, and should replace it rather than sit beside it:

```json
{
  "name": "Procure-to-pay",
  "steps": [
    {"name": "Requisition initiation and approval",
     "roles": [{"name": "requisition", "document_type": "purchase_requisition"}],
     "populations": [{"table": "requisitions"}],
     "themes": ["Authorisation against limits", "Segregation of duties"]},
    {"name": "Purchase order",
     "roles": [{"name": "order", "document_type": "purchase_order"}],
     "populations": [{"table": "po_data", "anchor": true}],
     "themes": ["..."]},
    {"name": "Goods receipt and inspection",
     "roles": [{"name": "receipt", "document_type": "goods_receipt"}],
     "populations": [{"table": "po_data", "columns": ["GRN_ID", "GRN_DATE", "GRN_STATUS"]}]},
    {"name": "Invoice processing and payment",
     "roles": [{"name": "invoice", "document_type": "vendor_invoice"},
               {"name": "voucher", "document_type": "payment_voucher"}],
     "populations": [{"table": "invoice_data"}]}
  ],
  "cross_cutting": {"name": "Procurement operations", "themes": ["Fraud risks considered"]},
  "created_by": "agent", "agent_run_id": "…", "apm_sha1": "…", "confirmed_by": null
}
```

What it needs: the APM, `document_classification.evidence_type_counts`, the
table names and column names (`workspace.get_frame(name).columns`, cheap), and
`workspace.joins`. All of those exist before any extraction runs. The two
additions to the scope map are the roles per step and the populations per
step; both are one line of the same small structured call, and both are
checked locally: a role's type must be one the engagement holds, a population
must be a loaded table.

The population-to-document link at this layer is a *candidate by column name*
(`PO_NUMBER` against a type called `purchase_order`), shown dotted on the graph
and never treated as a binding. It becomes the anchor only when layer 2 names
the field.

What it changes downstream, and this is the part worth wanting on its own:
`process` on a matrix row becomes one of the shape's step names. That is step
4e's closed vocabulary, delivered without the per-bucket units. The
`22 rows over 6 processes` figure in the redesign's expenses measurement is
the prompt doing by exhortation what this artifact does by construction.

### Layer 2: the bindings, after the schemas

`tests.cycle_ruleset_proposed` keeps its place and both of its edges, gains a
third on `planning.cycle_ready`, and asks for less: the roles are fixed by the
shape, so the worker chooses fields only. `LINKAGE_SYSTEM` loses the paragraph
inventing roles and gains the shape as input; `validate_linkage_proposal`
refuses a role the shape does not declare. The anchor's table and column come
from the shape's flagged population, and the worker supplies the role field.
Everything after that, approval, write-back onto the matrix rows, cycle tests,
is untouched.

The graph shown in `CycleBound.dc.html` is this ruleset drawn instead of
listed, plus the shape's step band, plus the table joins that already exist.

### The dependency change

```
planning.apm_ready ──► planning.cycle_ready ──► planning.rcm_ready
                              │
documents.schemas_stamped ────┴──► tests.cycle_ruleset_proposed ──► approved ──► tests.specified
```

`planning.cycle_ready` depends on `planning.apm_ready`,
`documents.types_classified` and `sources.imported`. `documents.types_classified`
is already a prerequisite of the matrix, so the closure's order does not move.
Readiness: `satisfied` when a shape exists whose `apm_sha1` is the current APM
hash, as step 4b specifies for the scope map; an auditor's confirmation keeps
the hash so edits survive until the APM changes. `invalidate_on=("planning:apm",)`.

## Feasibility

### Backend

Moderate, and most of it is step 4a and 4b of the RCM redesign under another
name.

- `workspaces.update_planning`: add `cycle` to the allowed set with a
  validator (`validate_cycle_shape`: at least one step, unique names, every
  role type held by the engagement, every population a loaded table, at most
  one anchor). `workspace_transactions.artifact_projection` gains
  `planning:cycle`.
- Worker `planning.cycle`, preset `planning.cycle` (planning context, APM,
  type counts, table metadata, joins; no document text). One repair.
  Reasoning `low`. Validator as above, plus every APM theme assigned once.
- Capability `planning.cycle_ready`, binder `_bind_cycle`, executor
  `execute_cycle` under `expected_parents={"planning:apm": …}`, reconciler by
  material projection. `DEPENDENCIES` edit and the
  `test_workflow_audit_definition.py` edge set.
- `_bind_rcm`: the shape's step names on the unit input, `RCM_ROWS_SYSTEM`
  told to use them, `_normalized_rcm_row` refusing a `process` outside them
  (a warning for one run, an error once measured).
- Linkage worker: shape on the unit input; roles from the shape; role check in
  the gate. `cycle_rulesets.validate` accepts a `cycle_sha1` and records it.
- A read-only projection for the page, `GET /planning/cycle/graph`, assembling
  nodes and edges from the shape, the tables and their columns, `workspace.joins`,
  the schemas, and the effective or latest proposed ruleset with its
  measurement. No model call; it is a join over things the workspace already
  holds. The frontend does no inference.

Not changed: `execute_cycle_ruleset`, approval, `cycle_linking`, the tests.

### Frontend: the graph

Feasible without a graph library, and better without one.

- The cycle is a sequence: at most a handful of steps, at most `MAX_ROLES`
  roles, a few populations. A **fixed lane layout** does it: one column per
  step in flow order; documents in the top lane, populations in the lane below,
  master data below that. No force layout, no layout engine, deterministic
  positions, nothing to jitter between loads.
- Nodes are HTML (a header, one row per field with a role glyph), the edge
  layer is one absolutely positioned SVG under them. Edge endpoints are the
  centre of a field row, computed from the row index, which is what makes the
  diagram read as a relationship diagram rather than a box-and-arrow sketch.
  The mockups were generated this way and the same arithmetic is the
  component's.
- Edge kinds are the artifact's own vocabulary and are drawn distinctly: join
  key (identifier = identifier, solid teal), assertion (must agree, dashed
  purple), anchor (population column to role field, solid navy), table join
  (grey), candidate by column name (dotted amber), and the SOP sequence
  (dashed grey) shown only while no fields exist.
- Fields shown by default are the ones a rule touches, with a `+N fields not
  in a rule` footer and a *Show all fields* toggle. `purchase_order` induced
  14 fields; drawing all of them on five nodes is a wall, and the reviewer's
  question is which fields the rules rest on.
- Interaction is selection, not on-graph editing: click a node or an edge and
  the right-hand panel shows it (fan-out and matched counts for a join key,
  the requirement and which matrix attributes it answers for an assertion).
  Editing the shape is the step list in the same panel: reorder, change a
  role's type, assign a population. Editing a rule stays the ruleset editor.
- Size: a `CycleGraph.vue` of roughly 500 lines with the layout arithmetic in
  a small pure module that can be unit-tested, plus a `CycleTab.vue` page and a
  route `bench/cycle` or a `planning/cycle` section, plus a `PlanningPayload`
  extension. Comparable to `ChainView.vue`.
- The existing `CycleRulesetReview.vue` dialog becomes redundant: its three
  lists and its Approve / Reject are the page's side panel. Keep the dialog
  until the page ships, then route the document tests tab's *Cycle rules*
  action to the page.

### What the mockup surfaced on real data

The bound artboard is drawn from the actual procurement schemas, and the graph
makes one defect visible that the list view does not: `purchase_requisition`
was induced with **no identifier field** (fourteen fields, none of role
`identifier`), so no join key can reach it, `cycle_rulesets.validate` would
refuse any ruleset that declares it as a required role, and the matrix's
`po-matches-req` attribute on `RCM-38C0D1` cannot be answered by a cycle.
On the page that is a red badge on the node, an *unbound* edge from
`requisitions`, and a concern in the panel. In the dialog today it would be a
validation error string at commit time.

## Costs and risks

- **Two model-authored artifacts agree by construction, not by luck.** The
  roles are named once, in the shape; the binding worker cannot rename them.
  That is the same rule the RCM redesign states as its governing one.
- **The shape can be wrong before any document has been read.** A step the
  SOP names but no document type fills shows as a step with a role and zero
  documents, which is a fact worth seeing, not an error. The RCM proceeds.
- **One cycle per workspace** is what `cycle_rulesets.effective` returns today.
  The shape stores one cycle; the JSON leaves room for a list, and the page
  would take a cycle selector when a second one is real.
- **Confirmation is not approval.** Confirming the shape is an auditor saying
  the steps and roles are right; it grants nothing. Approving the rules is the
  human act it is today. *Built without a separate confirmed state: an
  auditor's edit is the confirmation, and readiness never waits on one — see
  step 5a.*
- **The graph endpoint reads tables' column names** on every load; on a
  workspace with wide tables that is a frame open per table. Cache it on the
  workspace's table signature the way `projection_cache` caches the document
  test listing.
- **Scope map and cycle shape must not both exist.** If step 4 of the RCM
  redesign is built first, this artifact extends it; if this is built first,
  step 4 reads it. Building both is the one outcome to avoid.

## Open questions for the auditor

*All three are answered by step 5 as built; kept for the reasoning.*

1. Does the RCM's `process` become an error when it names a step the shape
   does not have, or a warning that reconciles rows into the nearest step? The
   mockup assumes the closed vocabulary. **Answered by 5d: a flag on the run
   first, promoted to a row error once one treasuryfull and one procurement
   regeneration have been read.**
2. Should the bindings be proposed as soon as schemas exist, before the matrix,
   with the coverage of matrix requirements as a later pass? It would let the
   graph fill in earlier at the cost of a second linkage turn. The mockup keeps
   today's single turn after the matrix. **Answered by 5e: the stage keeps its
   place and both of its edges; only the roles became an input.**
3. Where does the page live: under Planning beside the APM and the matrix, or
   under the workbench beside Tables? The mockup's breadcrumb says Planning.
   **Answered by 5f: a file section `cycle`, between `apm` and `coverage`.**
