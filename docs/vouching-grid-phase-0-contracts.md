# Vouching grid Phase 0/0.1 contract inventory

This inventory locks the clean boundary before any extraction, linking,
generation, execution, or grid implementation is replaced.

## Canonical ownership

- `backend/app/cycle_registry/models.py` and `registry.py` own the immutable,
  hash-identified registry core. They know no procurement record names.
- `backend/app/cycle_registry/common.py` owns shared non-linking entity IDs,
  common fields, evidence strategies, and conservative normalization.
- `backend/app/cycle_registry/packs/procure_to_pay.py` and `packs/payroll.py`
  are registered domain packs. Record, identifier, and field IDs are
  namespaced; adding another engagement area means adding another validated
  pack, not changing the core contract.
- `backend/app/cycle_vouching.py` owns domain-neutral normalization envelopes,
  registry-backed fragment/reduction validation, RCM control-attribute
  validation, cycle-test definitions, typed assertions, assurance scope,
  limits, and evaluation/disposition accessors.
- `backend/app/doc_tests.py` remains generic persistence plus the four existing
  worklist implementations. It recognizes `cycle_vouch` but does not own its
  schema or evaluator.
- `frontend/src/types.ts` mirrors structural unions and generic string IDs,
  registry references, and runtime pack descriptors. The grid and manual cycle
  authoring components are intentionally deferred to Phases 5 and 2.

Every persisted fragment, reduced record, transaction-cycle RCM attribute, and
cycle test carries `{pack_id, pack_version, definition_hash}`. Assertion results
carry the definition hash. A missing, cross-pack, or stale reference fails
closed rather than being interpreted with the currently installed vocabulary.

Transaction-cycle RCM attributes also carry non-empty `required_comparisons`.
Each comparison names exact registry record kinds, field selectors, operator,
and tolerance before a test population or role alias exists. A generated Cycle
vouch test must cover every comparison attached to each `requirement_ref`; the
reference alone is not coverage. A selector the pack cannot express or the
supplied evidence does not contain fails closed instead of being replaced with
a related prerequisite. Non-cycle evidence strategies forbid this field.

## Fifth-kind switch inventory

| Surface | Phase 0 disposition |
| --- | --- |
| `doc_tests.KINDS`, create/apply/load/list/meta | `cycle_vouch` accepted only through the strict clean contract |
| hydration and items | cycle items use `evaluation` and `disposition`; no durable `item.state` |
| `execution_issues`, `result_rollup`, summary | clean cycle shape validated and projected without dotted checks |
| simple `/build/vouching` and `/prepare-evidence-aware` | retained for `vouching`; no cycle payload alias |
| old `/build/cycle` | retained only until its Phase 2 replacement; it does not emit `cycle_vouch` |
| comparisons patch | remains restricted to `vouching` |
| RCM manifest/readiness | durable execution is read through shared accessors |
| standalone capability graph | `doc_tests_workflow_v2`: definitions -> executed -> dispositioned |
| execution binders | one existing execution binder; a separate auditor-only disposition binder never signs off |
| frontend kind unions and labels | fifth kind enumerated; pack-specific IDs removed from TypeScript unions and supplied by registry metadata |
| `DocTestsTab` create switch | explicit four-kind handling; fails closed if cycle authoring is invoked before Phase 2 |

## Retained and replacement paths

- Simple `vouching`/tracing, literal comparisons, and evidence-aware selection
  are retained.
- The legacy broad-document-type cycle builder is not a `cycle_vouch` builder.
  Phase 2 removes its route and replaces it with `/build/cycle-vouch`, backed by
  the canonical service.
- Phase 0/0.1 performs no writes to `Workspaces/procurement`. Compact
  procure-to-pay and payroll fixtures prove both packs traverse the same
  validators under `backend/tests/fixtures/`.
