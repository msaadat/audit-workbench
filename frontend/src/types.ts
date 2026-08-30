/** Where each phase stands, for the index card's strip. States are the
 *  backend's own, taken verbatim — the index never recomputes a phase. */
export interface WorkspaceProgress {
  planning: EngagementPhase['state']
  fieldwork: EngagementPhase['state']
  report: EngagementPhase['state']
}

export interface WorkspaceListItem {
  id: string
  revision: number
  name: string
  description: string
  created: string
  table_count: number
  /** Absent where the engagement's status could not be derived. */
  progress?: WorkspaceProgress | null
}

export interface JoinSpec {
  name: string
  left: string
  right: string
  how: string
  left_on: string[]
  right_on: string[]
}

export interface TableInfo {
  name: string
  kind: 'file' | 'join'
  source: string
  rows: number | null
  columns: number | null
  error: string | null
  join?: JoinSpec
}

export interface WorkspaceSummary {
  id: string
  revision: number
  name: string
  description: string
  created: string
  tables: TableInfo[]
  document_count?: number
  finding_count?: number
}

export type DocumentCategory = 'background' | 'policy' | 'regulation' | 'contract' | 'minutes' | 'voucher' | 'evidence' | 'prior_report' | 'correspondence' | 'other'
export type DocumentTextState = 'pending' | 'extracted' | 'image_only' | 'partial' | 'failed'

/** Who decided a document's type. A reclassification rerun may replace a
 *  model assignment; an auditor's stands until another auditor changes it. */
export type DocumentTypeAssigner = 'model' | 'auditor'

export interface DocumentClassification {
  document_type: string | null
  /** Set only when the type is `other`: what the auditor or model called it. */
  document_type_other: string | null
  assigned_by: DocumentTypeAssigner | null
  assigned_at: string | null
  confidence: 'high' | 'medium' | 'low' | null
  rationale: string
  previous_document_type: string | null
  agent_run_id: string | null
  unit_id: string | null
  /** The catalog this answer was chosen from. An `other` is only worth
   *  re-asking once this differs from the workspace's current catalog. */
  catalog_sha1: string | null
}

export interface DocumentTypeDefinition {
  id: string
  label: string
  area: string
  discriminator: string
  aliases: string[]
  active: boolean
}

export interface DocumentTypeArea {
  id: string
  label: string
}

/** A type an auditor coined for this engagement. Prefixed `local.`. */
export interface LocalDocumentType {
  id: string
  label: string
  discriminator: string
  created: string
  created_by: string
}

export interface DocumentTypeSummary {
  documents: number
  classified: number
  unclassified: number
  other: number
  auditor_assigned: number
  types_present: string[]
  local_types: string[]
}

export interface DocumentTypeCatalog {
  areas: DocumentTypeArea[]
  types: DocumentTypeDefinition[]
  local_prefix: string
  other: string
  local_types: LocalDocumentType[]
  summary: DocumentTypeSummary
}

/** A document and the type it currently carries. Covers both halves of the
 *  review: the `other` bucket, and an assignment the model got confidently
 *  wrong — which never enters that bucket. */
export interface ClassifiedDocument extends DocumentClassification {
  document_id: string
  title: string
}

/** A field on an induced document schema. `role` is the only part with
 *  downstream meaning: only an `identifier` field may serve as a join key. */
export interface DocumentSchemaField {
  name: string
  role: 'identifier' | 'party' | 'attribute' | 'control'
  value_type: 'identifier' | 'date' | 'number' | 'text' | 'boolean'
  cardinality: 'one' | 'many'
  verbatim: boolean
  confidence: 'high' | 'medium' | 'low'
  label: string
}

export interface DocumentSchema {
  document_type: string
  schema_version: number
  schema_hash: string
  fields: DocumentSchemaField[]
  derived_from: string[]
  reconciled: boolean
  /** Induced from a single sample, so nothing in it was corroborated. */
  low_confidence: boolean
  created: string
  updated: string
}

/** What an extraction was made against. Exact-matched on read. */
export interface DocumentSchemaRef {
  document_type: string
  schema_version: number
  schema_hash: string
}

export type CycleRulesetStatus = 'proposed' | 'approved' | 'rejected' | 'superseded'

export interface CycleRole {
  name: string
  document_type: string
  cardinality: 'one' | 'many'
  required: boolean
}

export interface CycleOperandRef {
  role: string
  field: string
}

export interface CycleJoinKey {
  id: string
  left: CycleOperandRef
  right: CycleOperandRef
  match: 'normalized_equal' | 'exact_equal'
  rationale: string
}

export interface CycleRulesetAssertion {
  id: string
  label: string
  left: CycleOperandRef
  right: CycleOperandRef | null
  operator: string
  tolerance: Record<string, number> | null
  rationale: string
}

/** Code-computed, never model-supplied. Fan-out is what makes an entity
 *  identifier masquerading as a join key visible to a reviewer. */
export interface JoinKeyMeasurement {
  left_documents: number
  right_documents: number
  left_stating_key: number
  matched_pairs: number
  left_unmatched: number
  fan_out_p50: number
  fan_out_p95: number
  fan_out_max: number
}

export interface AssertionMeasurement {
  left_stating: number
  right_stating: number | null
  evaluable_records: number
  /** Nothing states both sides, so the rule would never run — which looks the
   *  same as one that always passes. */
  silent: boolean
}

export interface CycleRulesetMeasurement {
  join_keys: Record<string, JoinKeyMeasurement>
  assertions: Record<string, AssertionMeasurement>
  records_measured: number
}

export interface CycleRulesetConcern {
  rule: string
  kind: 'join_key' | 'assertion'
  concern: 'entity_fan_out' | 'poor_coverage' | 'silent'
  detail: string
}

export interface CycleRuleset {
  ruleset_id: string
  status: CycleRulesetStatus
  cycle_label: string
  roles: CycleRole[]
  anchor: { table: string; column: string; role: string; field: string }
  join_keys: CycleJoinKey[]
  assertions: CycleRulesetAssertion[]
  schema_refs: DocumentSchemaRef[]
  ruleset_hash: string
  proposed_by: string
  approved_by: string | null
  approved_at: string | null
  created: string
  updated: string
  measured: CycleRulesetMeasurement
  concerns: CycleRulesetConcern[]
}

/** What a cycle test can be built on in a workspace with approved rules.
 *  One candidate, not a list: the anchor is part of what was approved, so the
 *  only decision left is the selection. */
export interface CycleRulesetCandidate {
  kind: 'ruleset'
  ruleset_id?: string
  ruleset_hash?: string
  cycle_label?: string
  roles?: CycleRole[]
  anchor?: { table: string; column: string; role: string; field: string }
  assertions?: Array<{ id: string; label: string; operator: string; rationale: string }>
  reach?: {
    population_rows: number
    linked_rows: number
    complete_cycles: number
    missing_role_counts: Record<string, number>
  }
  selection_confirmation?: { reason: string; eligible_row_count: number } | null
  /** Present when there is nothing to build on, and says which of the two
   *  reasons it is. */
  reason?: 'no_approved_ruleset' | 'population_table_missing'
}

export interface CycleRulesetDefinition {
  ruleset_id: string
  population: {
    selection:
      | { mode: 'evidence_linked' }
      | {
          mode: 'sample'
          method: string
          size: number
          seed: number
          stratify_by?: string
        }
  }
}

export interface CycleRulesetListing {
  items: CycleRuleset[]
  /** At most one ruleset is effective in a workspace. */
  effective_ruleset_id: string | null
  schemas: DocumentSchema[]
  types_present: string[]
}

export interface AuditDocument {
  id: string
  file: string
  source: string
  source_id: string | null
  relative_path: string | null
  title: string
  category: DocumentCategory
  pages: number | null
  sha1: string
  text_state: DocumentTextState
  note: string
  created: string
  updated: string | null
  created_by: string
  agent_run_id: string | null
  analysis_run_state: DocumentAnalysisRunState
  analysis_coverage_state: DocumentAnalysisCoverageState
  analysis_validity_state: 'current' | 'stale' | null
  analysis_updated_at: string | null
  analysis_review_state: 'not_applicable' | 'needs_review' | 'reviewed'
  has_analysis_overrides: boolean
  candidate_analysis_id: string | null
  analysis_resumable_run_id: string | null
  search_index_state: DocumentSearchIndexState
  analysis_vision_used: boolean
  classification: DocumentClassification
}

export type DocumentAnalysisRunState = 'idle' | 'queued' | 'analyzing' | 'paused' | 'interrupted' | 'failed' | 'cancelled'
export type DocumentAnalysisCoverageState = 'none' | 'complete' | 'partial' | 'unsupported'
export type DocumentSearchIndexState = 'pending' | 'indexing' | 'ready' | 'stale' | 'failed' | 'unsupported'

export interface DocumentIndexingStatus {
  state: 'idle' | 'indexing'
  job_count: number
  total_documents: number
  completed_documents: number
  remaining_documents: number
  active_document_id: string | null
  pace_seconds: number
}

export interface DocumentAnalysisCitation {
  id: string
  page: number
  excerpt?: string
  excerpt_hash?: string
  source_sha1: string
  evidence_kind?: 'text' | 'visual'
  description?: string
  region?: { x: number; y: number; width: number; height: number } | null
  variant?: string
  prepared_sha256?: string
  generated_description?: boolean
}

export interface GeneratedDocumentAnalysis {
  id: string
  document_id: string
  source_sha1: string
  extracted_text_sha1: string
  derived_text_markdown: string
  derived_text_sha256: string
  prepared_media_set_hash: string
  vision_used: boolean
  generation_profiles: ModelProfileStatus[]
  prompt_version: string
  provider: string | null
  model: string | null
  generated_at: string
  summary_markdown: string
  summary_origin?: 'model' | 'structured_evidence'
  audit_notes_markdown: string
  /** Profile-specific structured extraction, such as voucher IDs and approvals. */
  fields?: Record<string, unknown>
  /** Records a schema-guided extraction stated, under `schema_ref`. */
  records?: Array<Record<string, unknown>>
  schema_ref?: DocumentSchemaRef
  citations: DocumentAnalysisCitation[]
  coverage: {
    state: DocumentAnalysisCoverageState
    analyzed_pages: number[]
    text_analyzed_pages?: number[]
    vision_analyzed_pages?: number[]
    omitted_pages: number[]
    omissions?: Array<{ page: number; reason: string }>
    reason?: string | null
  }
}

export type EvidenceKindId = string

export interface CycleVouchMetadata {
  schema_version: number
  cardinalities: string[]
  reuse_rules: string[]
  selection_modes: Array<'evidence_linked' | 'sample'>
  sampling_methods: Array<'random' | 'interval' | 'stratified'>
  assurance_scopes: string[]
  assertions: string[]
  verdicts: CycleVerdict[]
  limits: {
    max_graph_hops: number
    max_cycle_records: number
    max_traversed_edges: number
    max_roles: number
    max_assertions: number
    max_items: number
  }
  /** Evidence strategies a control attribute may declare. */
  registry?: { evidence_kinds: Array<{ id: EvidenceKindId; label: string }> }
}

export interface NormalizedEvidenceValue<T = unknown> {
  raw_value: string
  value: T | null
  normalization_status: 'normalized' | 'invalid'
  normalization_error: string | null
  citation: string | DocumentAnalysisCitation | Array<string | DocumentAnalysisCitation>
}

export interface DocumentAnalysisDetail {
  document_id: string
  index_revision: number
  review_revision: number
  generated: GeneratedDocumentAnalysis | null
  effective: GeneratedDocumentAnalysis | null
  candidate: GeneratedDocumentAnalysis | null
  review: {
    revision: number
    summary_override: string | null
    audit_notes_override: string | null
    fragment_overrides?: Array<Record<string, unknown>>
    fragment_override_state?: 'current' | 'stale'
    review_state: 'not_applicable' | 'needs_review' | 'reviewed'
    reviewed_at: string | null
    updated_at: string | null
  }
  status: Pick<AuditDocument, 'analysis_run_state' | 'analysis_coverage_state' | 'analysis_validity_state' | 'analysis_updated_at' | 'analysis_review_state' | 'has_analysis_overrides' | 'candidate_analysis_id' | 'analysis_resumable_run_id' | 'search_index_state' | 'analysis_vision_used'>
}

export interface DocumentSearchResult {
  document_id: string
  title: string
  page: number
  excerpt: string
  score: number
  lexical_score: number
  vector_score: number
  citation_id: string
  citation: EvidenceRef
  origin?: 'extracted_text' | 'vision_transcript'
  analysis_id?: string | null
}

export interface DocumentPage {
  page: number
  text: string
  characters: number
  embedded_images: number
  image_only: boolean
  no_usable_text_no_image?: boolean
}

/**
 * An evidence anchor a criterion points at, carrying the `[C7]` marker the
 * document analysis authored alongside the frozen page and excerpt.
 */
export interface CriterionRef extends EvidenceRef {
  citation_id?: string
}

export interface EvidenceRef {
  id: string
  source_kind: 'document' | 'table' | 'analysis' | 'ruleset' | 'doctest' | 'procedure' | 'rcm' | 'datatest'
  source_id: string
  source_sha1: string | null
  page: number | null
  excerpt: string
  excerpt_hash: string | null
  item_id: string | null
  field: string | null
  generated_by: string | null
  confirmed_by: string | null
  confirmed_at: string | null
  evidence_kind?: string
  auditor_confirmed?: boolean
  analysis_id?: string | null
  legacy_ref?: string
  available?: boolean
}

export interface AIActivityEvent {
  id: string
  at: string
  stage: string
  purpose: string
  provider: string
  model: string
  document_ids: string[]
  page_ranges: number[]
  source_hashes: string[]
  response_hash: string | null
  disposition: string
  representation?: 'raw_pages' | 'summary' | 'audit_notes' | 'excerpt'
  analysis_id?: string | null
  search_query_hash?: string | null
  characters_supplied?: number
  cache_hit?: boolean
  retrieval_duration_ms?: number | null
  model_duration_ms?: number | null
  context_outcome?: 'supplied' | 'trimmed' | 'scope_required' | 'unavailable'
}

export interface KnowledgePack {
  id: string
  name: string
  scope: 'workspace' | 'reusable'
  version: number
  sha1: string
  source: string
  updated: string
}

export type DocTestKind = 'vouching' | 'attribute' | 'review' | 'qa' | 'cycle_vouch'
/** The joint reading of an item: the auditor's call when it is current, the
 *  runner's verdict otherwise. Derived — never written directly. */
export type DocTestItemState = 'pending' | 'agent_checked' | 'confirmed' | 'exception' | 'manual_review'
/** What the runner found. Only the runner writes this. */
export type DocTestEvaluationState = 'not_run' | 'agent_checked' | 'passed' | 'failed' | 'inconclusive'
/** What the auditor decided. Only the auditor writes this. */
export type DocTestDispositionState = 'pending' | 'confirmed' | 'exception' | 'needs_review'

export interface DocTestEvaluation {
  state: DocTestEvaluationState
  note: string
  /** Hash of the evidence and procedure this run consumed. */
  input_sha1: string | null
  ran_at: string | null
}

export interface DocTestDisposition {
  state: DocTestDispositionState
  note: string
  actor: string | null
  at: string | null
  evaluated_input_sha1: string | null
  /** The evidence moved after sign-off; the decision stands on the record but
   *  no longer counts as current. */
  stale: boolean
}
export type CycleEvaluationState = 'not_run' | 'passed' | 'failed' | 'incomplete' | 'needs_review' | 'stale'
export type CycleDispositionState = 'pending' | 'confirmed' | 'exception'
export type CycleAssertionVerdict = 'match' | 'mismatch' | 'cannot_determine' | 'missing_evidence' | 'invalid_extraction' | 'ambiguous' | 'not_run'
export type CycleAssuranceScope = 'targeted_evidence_only' | 'sampled_population'
/** What a reader may answer for a pair it was asked to judge. Agreement is
 *  settled against the values, so there is no comparison operator here. */
export type CycleVerdict = 'agrees' | 'disagrees' | 'cannot_determine'
export type CycleReuseRule = 'exclusive' | 'allowed'

export interface CycleSelectionConfirmation {
  kind: 'selection_confirmation'
  candidate_id: string
  eligible_row_count: number
  maximum_items: number
  suggested_selection: { mode: 'sample'; method: 'random'; size: number; seed: number; stratify_by?: string }
  reason: string
}

export interface DocComparison {
  document_id: string
  source_sha1?: string
  page?: number | null
  expected: unknown
  found: unknown
  method: 'exact' | 'normalized' | 'fuzzy' | 'numeric_tolerance' | 'date_tolerance'
  normalization: unknown
  tolerance: unknown
  result: 'match' | 'mismatch' | 'missing' | 'invalid' | 'missing_document'
  similarity?: number | null
  evidence?: EvidenceRef
}

export interface DocTestCheck {
  field: string
  expected: unknown
  found: unknown
  method: DocComparison['method']
  tolerance: unknown
  verdict: 'pending' | 'match' | 'mismatch' | 'missing' | 'invalid' | 'ambiguous'
  note: string
  comparisons: DocComparison[]
  evidence_refs: EvidenceRef[]
}

export interface DocTestItem {
  id: string
  label: string
  /** The procedure being performed on this item, always populated. */
  instruction: string
  /** The joint reading of `evaluation` and `disposition`, derived by the
   *  backend. Read it for a one-word status; never send it back as a mutation. */
  state?: DocTestItemState
  document_ids: string[]
  evidence_refs: EvidenceRef[]
  frozen?: Record<string, unknown>
  checks?: DocTestCheck[]
  attributes?: Array<{ name: string; expected: string; verdict: string; note: string; evidence_refs: EvidenceRef[] }>
  page?: number
  review_kind?: string
  summary?: string
  excerpt?: string
  question?: string
  response?: string
  citations?: EvidenceRef[]
  /** Per-document assessment, keyed by document id. */
  qa_answers?: Record<string, { answer: string; outcome: string; citations: EvidenceRef[] }>
  runner_note?: string
  document_conflicts?: { duplicate_documents: string[][] }
  transaction_identifiers?: string[]
  /** Required roles no attached document filled; the item cannot be concluded. */
  missing_roles?: string[]
  evidence_coverage?: {
    document_ids: string[]
    available_document_types: string[]
    missing_document_types: string[]
    image_only: boolean
  }
  evidence_request_ids?: string[]
  population_ref?: { table: string; source_row: number; source_sha1: string }
  frozen_row?: Record<string, unknown>
  /** The anchor value this item was seeded from, and the field it came from. */
  cycle_identifiers?: Array<{ role: string; field: string; value: string }>
  role_bindings?: Array<{
    role: string
    document_id: string
    record_id: string
    document_type?: string
    record_content_hash?: string
    extraction_hash?: string
    matched_by: Array<Record<string, unknown>>
  }>
  unassigned_records?: Array<string | Record<string, unknown>>
  role_conflicts?: Array<Record<string, unknown>>
  shared_record_facts?: Array<Record<string, unknown>>
  collisions?: Array<Record<string, unknown>>
  linkage_state?: 'linked' | 'needs_review'
  linkage_review?: Record<string, unknown>
  result_by_assertion?: Record<string, {
    assertion_sha1: string
    registry_definition_hash: string
    input_hashes: {
      population_source_sha1: string
      frozen_row_sha1: string
      bound_record_hashes: string[][]
      extraction_hashes: string[][]
      role_binding_sha1: string
      input_sha1: string
    }
    verdict: CycleAssertionVerdict
    display?: string
    comparisons: Array<Record<string, unknown>>
    evidence_refs: EvidenceRef[]
    stale?: boolean
    result_sha1?: string | null
  }>
  /** Item-first tests carry `DocTestEvaluation`; cycle items carry the typed
   *  registry-hashed variant. Narrow on `test.kind === 'cycle_vouch'`. */
  evaluation?:
    | DocTestEvaluation
    | { state: CycleEvaluationState; definition_sha1: string; result_sha1?: string | null }
  disposition?:
    | DocTestDisposition
    | {
      state: CycleDispositionState
      evaluated_definition_sha1: string | null
      stale: boolean
    }
  disposition_history?: Array<{
    state: CycleDispositionState
    evaluated_definition_sha1: string | null
    stale: boolean
    superseded_at: string
    superseded_by: string
    reason: string
    definition_sha1: string
  }>
}

export interface DocTestRollup {
  items: number
  tested_items?: number
  item_counts?: Partial<Record<CycleEvaluationState, number>>
  disposition_counts?: Partial<Record<CycleDispositionState, number>>
  assertion_columns?: number
  assertion_counts?: CycleAssertionCounts
  failed_items?: number
  incomplete_items?: number
  needs_review_items?: number
  confirmed_items?: number
  exception_items?: number
  pending_dispositions?: number
  coverage?: Record<string, unknown>
  assurance_scope?: CycleAssuranceScope
  assurance_label?: string
  conclusion_eligible?: boolean
  control_conclusion?: ControlConclusion
  assertion_mismatches?: number
  open_exceptions?: number
  matched: number
  mismatched: number
  confirmed: number
  exceptions: number
  manual_review: number
  pending: number
}

export interface EvidenceRequest {
  id: string
  document_test_id: string
  item_id: string
  rcm_id?: string | null
  reason: string
  next_action?: string
  missing_document_types: string[]
  status: 'open' | 'received' | 'cancelled'
  auditor_note?: string
  created?: string
  updated?: string
}

/** What the auditor still has to do about one worklist item. */
export type DocTestClassification =
  | 'exception'
  | 'needs_review'
  | 'awaiting_evidence'
  | 'confirmed'
  | 'not_run'

export type DocTestCounts = Record<DocTestClassification, number>

/** Where a test's control conclusion stands, as one exclusive bucket. `stale`
 *  is one recorded against evidence that has since moved; `agent` is an
 *  unattended run's own conclusion, which no auditor has reviewed. Written
 *  reasoning without a control conclusion is not a conclusion — those tests
 *  stay `none`. */
export type TestConclusionState = 'none' | 'stale' | 'agent' | 'auditor'

export type CycleAssertionCounts = Record<CycleAssertionVerdict, number> & { total: number }

/** One worklist item, flattened across tests for engagement-level triage. */
export interface DocTestSummaryItem {
  entry_type: 'item'
  test_id: string
  test_title: string
  test_kind: DocTestKind | null
  test_status: TestStatus
  rcm_id: string | null
  item_id: string
  label: string
  instruction: string
  state: DocTestItemState
  classification: DocTestClassification
  /** Test-grain, repeated on every item of the test — the auditor concludes
   *  once per test, and the worklist filters per row. */
  conclusion_state: TestConclusionState
  /** Both sides behind `state`, so the worklist can show what the runner found
   *  next to what the auditor decided without loading the test. */
  evaluation: DocTestEvaluation
  disposition: DocTestDisposition
  question: string
  response: string
  runner_note: string
  document_count: number
  citation_count: number
  evidence_count: number
  checks_total: number
  checks_matched: number
  checks_failed: number
  missing_document_types: string[]
  image_only: boolean
  evidence_request_count: number
  has_conflict: boolean
  updated: string
}

export interface DocTestSummaryCycleTest {
  entry_type: 'cycle_test'
  test_id: string
  title: string
  test_kind: 'cycle_vouch'
  test_status: TestStatus
  rcm_id: string | null
  classification: DocTestClassification
  conclusion_state: TestConclusionState
  item_count: number
  tested_item_count: number
  evaluation_counts: Partial<Record<CycleEvaluationState, number>>
  disposition_counts: Partial<Record<CycleDispositionState, number>>
  assertion_columns: number
  assertion_counts: CycleAssertionCounts
  coverage: Record<string, unknown>
  selection_basis: 'evidence_linked' | 'sample'
  assurance_scope: CycleAssuranceScope
  assurance_label: string
  requirement_refs: string[]
  updated: string
}

export type DocTestSummaryEntry = DocTestSummaryItem | DocTestSummaryCycleTest

export interface DocTestSummaryPayload {
  entry_counts: DocTestCounts
  test_counts: Record<DocTestKind, number> & { total: number; item_first: number }
  tested_item_counts: {
    total: number
    executed: number
    passed: number
    failed: number
    incomplete: number
    needs_review: number
    not_run: number
    stale: number
    confirmed: number
    exceptions: number
    pending_disposition: number
  }
  assertion_counts: CycleAssertionCounts
  entries: DocTestSummaryEntry[]
}

export interface CycleVouchGridComparison {
  side?: string
  role?: string
  document_id?: string | null
  state?: string
  verdict?: CycleAssertionVerdict
  record_ids: string[]
  display_values: unknown[]
  entry_count: number
  evidence_count: number
}

export interface CycleVouchGridCell {
  verdict: CycleAssertionVerdict
  display: string
  comparison_count: number
  evidence_count: number
  comparisons: CycleVouchGridComparison[]
  /** The runner's own flag that this cell needs re-running. */
  stale: boolean
  /** The stored verdict cannot be read under the current column definition. */
  attribution_stale: boolean
}

export interface CycleVouchGridPayload {
  test_id: string
  test_sha1: string
  definition_sha1: string
  title: string
  population: { table: string; column: string; selection: Record<string, unknown> }
  coverage: Record<string, unknown>
  selection_basis: 'evidence_linked' | 'sample'
  assurance_scope: CycleAssuranceScope
  assurance_label: string
  tested_item_counts: Partial<Record<CycleEvaluationState, number>>
  assertion_counts: CycleAssertionCounts
  columns: Array<{
    key: string
    label: string
    requirement: string
    applicable_roles: string[]
    /** Excludes unattributable cells: sum(counts) + stale_cells === row count. */
    counts: Record<CycleAssertionVerdict, number>
    stale_cells: number
  }>
  rows: Array<{
    item_id: string
    label: string
    evaluation_state: CycleEvaluationState
    disposition_state: CycleDispositionState
    disposition_stale: boolean
    definition_stale: boolean
    roles_present: string[]
    missing_roles: string[]
    shared_record_facts: Array<{
      role: string
      record_id: string
      related_item_ids: string[]
      related_item_count: number
      related_items_truncated: boolean
      reuse_across_items: CycleReuseRule
      identifier_edge: Record<string, unknown>
    }>
    cells: Record<string, CycleVouchGridCell>
  }>
  stale_definition: boolean
  stale_cell_count: number
  page: { offset: number; limit: number; total: number }
  truncated: boolean
}

export interface DocTest extends TestPlan, TestOutcome {
  id: string
  kind: DocTestKind | null
  schema_version?: 2
  requirement_refs?: string[]
  procedure_key?: string
  definition?: {
    ruleset_id: string
    ruleset_hash?: string
    population: { table: string; column: string; selection: Record<string, unknown> }
  }
  coverage?: Record<string, unknown>
  title: string
  status: TestStatus
  semantic_id: string
  rcm_refs: string[]
  procedure_refs: string[]
  rcm_id: string | null
  spec: Record<string, unknown>
  steps: DocTestStep[]
  items: DocTestItem[]
  item_count?: number
  state_counts?: Record<string, number>
  rollup?: DocTestRollup
  evidence_requests?: EvidenceRequest[]
  created: string
  updated: string
  sha1: string
}

export interface WorkingPaper {
  procedure_id?: string
  rcm_id?: string
  generated_at: string
  source_sha1: string
  markdown: string
  html: string
}

export interface PlanningContext {
  objective: string
  entity: string
  period: string
  scope: string
  materiality: string
  key_contacts: string
  background_notes: string
  interview_answers: Record<string, string>
}

export interface PlanningRecord {
  context: PlanningContext
  apm_markdown: string
  created_by: 'agent' | 'user'
  agent_run_id: string | null
  updated: string | null
}

export interface RcmRow {
  id: string
  semantic_id: string
  created_by: 'agent' | 'user'
  agent_run_id: string | null
  process: string
  risk: string
  risk_rating: 'low' | 'medium' | 'high' | 'critical'
  business_cycle: string
  control_attributes: RcmControlAttribute[]
  /** Set by the load path when a row's pack reference no longer resolves. */
  attributes_status?: 'valid' | 'invalid'
  attributes_error?: string
  control: string
  control_type: string
  control_owner: string
  criteria: string
  /**
   * The sentences a criterion rests on, frozen as typed anchors when the row
   * was written: a page, an excerpt, and the source hash at that moment. The
   * `citation_id` is the `[C7]` marker the analysis authored.
   */
  criteria_refs: CriterionRef[]
  test_refs: string[]
  execution_rollup: RcmExecutionRollup
  finding_refs: string[]
  evidence_refs: EvidenceRef[]
  prepared_by: string | null
  /** Sign-off, and nothing finer: a row is either signed or it is not. */
  review_status: 'draft' | 'reviewed'
  updated: string
}

export interface RcmControlAttributeBase {
  key: string
  assertion: 'Existence' | 'Completeness' | 'Accuracy' | 'Authorization' | 'Valuation' | 'Cut-off' | 'Compliance' | 'Operational'
  requirement: string
}

/** One side of a schema-backed comparison: a document type and a field on it. */
export interface RcmSchemaOperand {
  document_type: string
  field: string
}

/** What a control attribute requires of the cycle, in the vocabulary the
 *  documents were extracted under. Replaces the pack operand; the two never
 *  appear on one attribute. */
export interface RcmSchemaComparison {
  key: string
  left: RcmSchemaOperand
  right?: RcmSchemaOperand | null
  requirement: string
  tolerance?: Record<string, number> | null
  rationale?: string
}

/** The induced schemas, as the RCM editor needs to see them. */
export interface DocumentSchemaCatalogEntry {
  document_type: string
  fields: Array<{ name: string; role: string; value_type: string; label: string }>
}

/** A cycle attribute answered by this engagement's own induced schemas. */
export interface RcmSchemaCycleAttribute {
  evidence_kind: 'transaction_cycle'
  required_comparisons: RcmSchemaComparison[]
}

export type RcmControlAttribute = RcmControlAttributeBase & (
  | RcmSchemaCycleAttribute
  | {
      evidence_kind: 'tabular_population' | 'document_content' | 'manual_inspection' | 'inquiry' | 'mixed'
      required_comparisons?: never
    }
)

/** The one source a test is answered from. */
export type TestSource = 'data' | 'document'
export type TestStatus =
  | 'draft'
  | 'ready'
  | 'in_progress'
  | 'review_required'
  | 'blocked'
  | 'completed'
  | 'completed_no_exception'
  | 'completed_with_exception'
  | 'not_applicable'
export type ControlConclusion =
  | 'effective'
  | 'partially_effective'
  | 'ineffective'
  | 'no_conclusion'
  | 'not_applicable'

/** The audit plan every test carries, whatever its source. */
export interface TestPlan {
  title: string
  objective: string
  criteria: string
  methodology_refs: Array<Record<string, unknown>>
}

export interface DataTestStep {
  step_id?: string
  label: string
  instruction: string
  code: string
}

export interface DocTestStepCheck {
  field: string
  expected: string
}

export interface DocTestStep {
  step_id?: string
  label: string
  instruction: string
  mode: 'question' | 'vouch'
  document_ids: string[]
  missing_evidence: string
  question?: string
  checks?: DocTestStepCheck[]
}

export interface DataTestStepResult {
  step_id: string
  step_label: string
  status: TestStatus | 'error'
  exception_count: number
  error: string | null
}

/** The outcome roll-up computes onto every test. */
export interface TestOutcome {
  status: TestStatus
  result_summary: string
  conclusion: string
  control_conclusion: ControlConclusion
  scope_limitations: string
  next_action: string
  exception_count: number
  open_exception_count: number
  evidence_refs: EvidenceRef[]
  finding_refs: string[]
}

export interface TestRollup {
  test_id: string
  kind: 'datatest' | 'doctest'
  title: string
  executed_count: number
  exception_count: number
  open_exception_count: number
  evidence_count: number
  status: TestStatus
  result_summary: string
  conclusion: string
  control_conclusion: ControlConclusion
  conclusion_eligible?: boolean
  assurance_scope?: CycleAssuranceScope | null
  assurance_label?: string | null
  selection_basis?: string
  coverage?: Record<string, unknown>
  tested_items?: number
  failed_items?: number
  incomplete_items?: number
  needs_review_items?: number
  confirmed_items?: number
  assertion_mismatches?: number
  scope_limitations: string
  finding_refs: string[]
}

export interface RcmExecutionRollup {
  tests?: number
  completed?: number
  passed?: number
  failed?: number
  blocked?: number
  review_required?: number
  draft?: number
  exceptions?: number
  open_exceptions?: number
  tested_items?: number
  failed_items?: number
  incomplete_items?: number
  needs_review_items?: number
  confirmed_items?: number
  assertion_mismatches?: number
  conclusion_eligible_tests?: number
  supplemental_tests?: number
  assurance_scopes?: CycleAssuranceScope[]
  control_conclusion?: string
  /** Why the conclusion could not go higher than the evidence class allows. */
  evidence_ceiling?: string
  /** False when the ceiling is recorded beside a conclusion the auditor owns. */
  evidence_ceiling_applied?: boolean
  test_rollups?: TestRollup[]
}

export type DataTestEngine = 'analytics' | 'validation' | 'polars'

export interface DataTestRunSummary {
  id: string
  run_at: string
  status: TestStatus
  verdict: 'ok' | 'warn' | 'fail' | 'info' | 'error'
  exception_count: number
  semantic_valid: boolean
  dataset_fingerprints: Record<string, string>
  source_sha1: string
  result_sha1: string
}

/** What the run found. Only the runner writes this. */
export type DataTestEvaluationState = 'not_run' | 'passed' | 'failed' | 'inconclusive'
/** How one group of exceptions was ruled on. Only a decider writes this. */
export type DataTestDispositionState = 'pending' | 'accepted' | 'exception' | 'needs_review'
/** Who decided. `agent` is an unattended run's own conclusion — real, recorded,
 *  and overridable; `auditor` outranks it and is never overwritten. */
export type DataTestConclusionSource = 'none' | 'agent' | 'auditor'

export interface DataTestEvaluation {
  state: DataTestEvaluationState
  note: string
  exception_count: number
  /** The groups an auditor can rule on, carried so a reader never has to open
   *  the result document to know what is outstanding. */
  reasons: DataTestExceptionReason[]
  /** What the run reads as, offered to whoever concludes. Not a conclusion. */
  suggested_control_conclusion: ControlConclusion
  /** Hash of the definition and the data this run consumed. */
  input_sha1: string | null
  ran_at: string | null
}

export interface DataTestExceptionDisposition {
  scope: 'reason'
  /** The reason label this ruling covers. */
  key: string
  state: DataTestDispositionState
  note: string
  rows: number
  records: number
  actor: string | null
  source: DataTestConclusionSource
  at: string | null
  evaluated_input_sha1: string | null
  /** The evidence moved after the ruling; it stands on the record but no
   *  longer counts as current. */
  stale: boolean
}

export interface DataTest extends TestPlan, TestOutcome {
  id: string
  semantic_id: string
  rcm_id: string | null
  title: string
  objective: string
  engine: DataTestEngine | null
  table_refs: string[]
  steps: DataTestStep[]
  spec: Record<string, unknown>
  /** The joint reading of `evaluation` and the dispositions, derived by the
   *  backend. Read it for a one-word status; never send it back as a mutation. */
  status: TestStatus
  semantic_warnings: string[]
  evaluation: DataTestEvaluation
  exception_dispositions: DataTestExceptionDisposition[]
  conclusion_source: DataTestConclusionSource
  control_conclusion_source: DataTestConclusionSource
  control_conclusion_input_sha1: string | null
  /** The conclusion was reached against evidence that has since moved. */
  control_conclusion_stale: boolean
  /** The stored result no longer describes the current definition or data —
   *  a statement about the workspace now, not about any decision on it. */
  result_stale: boolean
  semantic_review: { at: string; actor: string; note: string } | null
  last_run: DataTestRunSummary | null
  evidence_refs: EvidenceRef[]
  created_by: 'agent' | 'user'
  agent_run_id: string | null
  created: string
  updated: string
}

export interface DataTestExceptionReason {
  label: string
  rows: number
  records: number
  /** The columns still carrying a value for the rows this reason claims. */
  columns: string[]
}

/** The record-level reading of an exception frame: what failed, and out of how many. */
export interface DataTestExceptionProfile {
  /** The column identifying one record, when the steps agree on one. */
  entity_key: string | null
  record_count: number
  row_count: number
  population: number | null
  population_table: string | null
  /** 'step' means the reasons are step labels, not per-condition attribution. */
  reason_source: 'predicate' | 'step'
  reasons: DataTestExceptionReason[]
}

export interface DataTestResult extends DataTestRunSummary {
  data_test_id: string
  rcm_id: string | null
  /** What the run reads as. A suggestion for whoever concludes, never itself
   *  a conclusion — nothing signs the file by running. */
  suggested_control_conclusion: ControlConclusion
  /** Hash of the definition and the data this run consumed. */
  input_sha1: string
  verdict_text: string
  statistics: Array<{ label: string; value: string }>
  viz: Record<string, unknown> | null
  stdout: string
  summary_frame: FramePayload | null
  exception_frame: FramePayload | null
  exception_profile: DataTestExceptionProfile | null
  semantic_issues: string[]
  join_diagnostics: Array<Record<string, unknown>>
  step_results: DataTestStepResult[]
  error: string | null
}

export interface AuditProcedure {
  id: string
  semantic_id: string
  created_by: 'agent' | 'user'
  agent_run_id: string | null
  rcm_refs: string[]
  objective: string
  criteria: string
  steps: string[]
  method: string
  expected_evidence: string
  test_refs: string[]
  evidence_refs: EvidenceRef[]
  methodology_refs?: Array<{ pack_id: string; pack_name: string; version: number; sha1: string; section: string; citation: string }>
  result_summary: string
  conclusion: string
  scope_limitations: string
  updated: string
}

export interface PlanningPayload {
  planning: PlanningRecord
  rcm: RcmRow[]
  procedures: AuditProcedure[]
  data_tests: DataTest[]
  observations: AuditObservation[]
  document_tests: Array<Pick<DocTest, 'id' | 'title' | 'status' | 'rcm_id' | 'rcm_refs'>>
  findings: AuditFinding[]
  finding_rollups: FindingRollups
}

export interface RcmCoverage {
  ok: boolean
  issue_count: number
  rows_without_tests: string[]
  unspecified_tests: Array<{ rcm_id: string; test_id: string }>
  invalid_test_parents: Array<{ id: string; reason: string }>
  high_risks_without_executable_work: string[]
  completed_without_durable_result: Array<{ rcm_id: string; test_id: string }>
  inconsistent_conclusions: Array<{ rcm_id: string; test_id: string; reason: string }>
}

/**
 * `GET /rcm/completion`. The gates the backend already computes over the whole
 * matrix, which is what the RCM status bar reports rather than re-deriving them
 * row by row in the browser.
 */
export interface RcmCompletion {
  status: 'completed' | 'completed_with_open_items' | 'completed_with_issues'
  coverage: RcmCoverage
  incomplete_outcomes: Array<{ rcm_id: string; test_id: string; status?: string }>
  blank_conclusions: Array<{ rcm_id: string; test_id: string }>
  /** A disclosure, not a gate: nobody read what the agent concluded. */
  unreviewed_agent_conclusions: Array<{ rcm_id: string; test_id: string }>
  blocked_without_plan: Array<{ rcm_id: string; test_id: string; missing: string[] }>
  rcm_without_conclusion: string[]
  /** Conclusions capped by their evidence class rather than earned. */
  evidence_ceilings: Array<{ rcm_id: string; reason: string }>
  pending_cycle_dispositions: Array<{ rcm_id: string; test_id: string; item_id: string | null }>
  untested_columns: Array<{ table: string; column_count: number; untested_count: number; flagged_count: number }>
  undispositioned_analyses: number
  declined_analyses: number
}

export interface AuditObservation {
  id: string
  rcm_id: string
  test_id: string
  execution_ref: string
  exception_count: number
  summary: string
  classification: string
  outcome: 'exception' | 'needs_manual_check'
  created: string
  updated: string
}

export type FindingSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info'

export interface AuditFinding {
  id: string
  semantic_id: string
  created_by: 'agent' | 'user'
  agent_run_id: string | null
  title: string
  severity: FindingSeverity
  /** Report-ready Markdown; its `##` sections come from the workspace finding template. */
  narrative: string
  management_response: string
  rcm_refs: string[]
  procedure_refs: string[]
  test_refs: string[]
  execution_refs: string[]
  evidence_refs: EvidenceRef[]
  evidence_warnings?: string[]
  cause_pending: boolean
  auditor_confirmed: boolean
  source: 'agent' | 'manual' | 'promoted'
  created: string
  updated: string
}

export interface FindingSummary {
  id: string
  title: string
  severity: FindingSeverity
}

export interface FindingRollups {
  by_rcm: Record<string, FindingSummary[]>
  by_test: Record<string, FindingSummary[]>
  by_procedure: Record<string, FindingSummary[]>
}

export interface FindingsPayload {
  items: AuditFinding[]
  rcm: RcmRow[]
  procedures: AuditProcedure[]
  data_tests: DataTest[]
  document_tests: Array<Pick<DocTest, 'id' | 'title' | 'status'>>
  rollups: FindingRollups
  evidence_options: Array<{ anchor: EvidenceRef; label: string }>
}

export interface ReportQualityIssue {
  code: string
  severity: 'error' | 'warning' | 'info'
  message: string
  refs: string[]
  source: 'deterministic' | 'editorial'
}

export interface ReportQuality {
  checked_at: string
  issues: ReportQualityIssue[]
  counts: { error: number; warning: number; info: number }
  ok: boolean
  editorial?: ReportQualityIssue[]
}

export interface AuditReport {
  markdown: string
  generated_markdown: string
  generated_at: string | null
  generated_by_run: string | null
  edited: boolean
  updated: string | null
  generation_warnings: string[]
  quality: ReportQuality
  requires_reconcile?: boolean
  current_markdown?: string
  candidate_markdown?: string
  used_model?: boolean
  drafted_sections?: string[]
}

export interface ReportContext {
  workspace: { id: string; name: string; description: string }
  planning: Record<string, unknown>
  rcm: Array<Record<string, unknown>>
  findings: Array<Record<string, unknown>>
  draft_findings_excluded: string[]
  scope_limitations: Array<{ rcm_id: string; test_id: string; text: string }>
  completion: Record<string, unknown>
  preliminary: boolean
  statistics: Record<string, number>
}

export interface MarkdownTemplate {
  name: 'apm' | 'rcm' | 'interview' | 'workpaper' | 'report'
  markdown: string
  source: 'default' | 'workspace'
}

export type IntakeRoute = 'table' | 'document' | 'unsupported' | 'ignore'

export interface IntakeClassification {
  route: IntakeRoute
  document_category: string | null
  table_role: string | null
  subtype: string
  proposed_name: string
  confidence: 'high' | 'medium' | 'low'
  rationale: string
  duplicate_ref: string | null
  proposed_action: 'import' | 'ignore'
  deterministic_route: IntakeRoute
}

export interface IntakeBatchItem {
  id: string
  relative_path: string
  size: number
  last_modified: number
  state: 'new' | 'changed' | 'unchanged' | 'excluded'
  needs_upload: boolean
  uploaded: boolean
  local_metadata: Record<string, unknown>
  classification: IntakeClassification | null
  action: string
  target_ref: string | null
  error: string | null
}

export interface IntakeSuggestedAction {
  id: 'update_planning'
  agent_kind: 'planning'
  title: string
  reason: string
  document_ids: string[]
  documents: Array<{ id: string; title: string; category: string; pages: number | null }>
  omitted_document_count: number
  requires_doc_ai: boolean
}

export interface IntakeBatch {
  id: string
  source_id: string
  mode: 'auto' | 'permission'
  status: 'uploading' | 'classifying' | 'awaiting_approval' | 'applying' | 'completed' | 'failed'
  manifest_count: number
  unchanged_count: number
  excluded_count: number
  unsupported_count: number
  items: IntakeBatchItem[]
  summary?: { imported: number; unchanged: number; ignored: number; ambiguous: number }
  indexing_job?: {
    id: string
    state: 'queued' | 'indexing' | 'completed' | 'completed_with_issues'
    document_ids: string[]
    coalesced_document_ids: string[]
  }
  suggested_actions?: IntakeSuggestedAction[]
}

export interface ColumnSchema {
  name: string
  dtype: string
  kind: 'numeric' | 'date' | 'boolean' | 'text'
}

export interface FramePayload {
  columns: string[]
  dtypes: string[]
  rows: (string | number | boolean | null)[][]
}

export interface QueryResult extends FramePayload {
  total_rows: number
  filtered_rows: number
  page: number
  page_size: number
  // Present only in cross-tab mode (when the query spec carries split_by).
  row_fields?: string[]
  split_field?: string | null
  value_names?: string[]
  column_keys?: string[]
  grand_total?: (string | number | boolean | null)[] | null
}

export interface TopValue {
  value: string | null
  count: number
  pct: number
}

export interface ColumnProfile {
  name: string
  dtype: string
  total: number
  blank_count: number
  blank_pct: number
  distinct_count: number
  distinct_pct: number
  inferred_type: string
  min: string | null
  max: string | null
  mean: string | null
  top_values: TopValue[]
}

export interface TableProfile {
  rows: number
  columns: number
  sampled: boolean
  sample_rows: number
  duplicate_rows: number
  estimated_size_bytes: number
  column_profiles: ColumnProfile[]
}

export interface FilterSpec {
  column: string
  op: string
  value: string
  value2?: string
}

export interface AggSpec {
  column: string
  func: string
}

export interface QuerySpec {
  columns?: string[]
  filters: FilterSpec[]
  group_by: string[]
  split_by?: string | null
  aggs: AggSpec[]
  sort: { column: string; desc: boolean }[]
  page: number
  page_size: number
}

export interface AnalyticsParamMeta {
  name: string
  kind: 'column' | 'columns' | 'number' | 'select' | 'text'
  label: string
  column_kind?: string
  optional?: boolean
  default?: string | number
  options?: { label: string; value: string | number }[]
}

export interface AnalyticsTest {
  id: string
  label: string
  icon: string
  group: string
  description: string
  params: AnalyticsParamMeta[]
}

export interface StatChip {
  label: string
  value: string
}

export interface AnalyticsResult {
  title: string
  verdict: 'ok' | 'warn' | 'fail' | 'info'
  verdict_text: string
  stats: StatChip[]
  viz: VizSpec | null
  summary: FramePayload | null
  summary_rows: number
  detail: FramePayload | null
  detail_rows: number
}

export interface VizSpec {
  type: 'table' | 'bar' | 'line' | 'pie'
  x?: string
  y?: string[]
}

export interface NavigationTarget {
  // A destination name, resolved through `useWorkspaceNavigation`, which owns
  // which surface each of these currently lives on. The field is still called
  // `tab` because it is the wire name the backend sends.
  tab: 'apm' | 'rcm' | 'chain' | 'documents' | 'doc-tests' | 'data-tests' | 'data' | 'query' | 'analysis' | 'findings' | 'report'
  query: Record<string, string>
}

export interface EngagementSubPhase {
  id: string
  label: string
  state: 'not_started' | 'in_progress' | 'complete' | 'attention'
  complete: boolean
  target: NavigationTarget
}

export interface EngagementPhase {
  id: 'planning' | 'fieldwork' | 'report'
  label: string
  state: 'not_started' | 'in_progress' | 'complete' | 'attention'
  complete: boolean
  summary: string
  counts: Record<string, number>
  issues: string[]
  target: NavigationTarget
  sub: EngagementSubPhase[]
}

/** A rail entry's own state, where a phase is broader than the tab it opens.
 *  Keyed by rail section id. */
export interface EngagementSection {
  id: string
  label: string
  state: EngagementPhase['state']
  complete: boolean
  issues: string[]
  /** `total` and `concluded`, so a chip reads "36/39" without recounting the
   *  population and reaching a different answer than the state beside it. */
  counts?: Record<string, number>
  /** Data tests that have never run. Present on `data-tests` only. */
  unrun_test_ids?: string[]
  /** Data tests whose result no longer describes its basis. `data-tests` only. */
  stale_test_ids?: string[]
}

export interface EngagementStatusPayload {
  phases: EngagementPhase[]
  sections?: Record<string, EngagementSection>
}

export interface SavedAnalysis {
  id: string
  title: string
  kind: 'analytics' | 'python'
  table: string | null
  note: string
  viz: VizSpec
  source: 'library' | 'ai' | 'code'
  created: string
  created_by?: 'user' | 'agent' | string | null
  spec?: Record<string, unknown>
  outcome_policy?: AnalysisOutcomePolicy
  /** Freshness of `last_result` against the current definition and inputs. */
  state: AnalysisResultState
  /** The triage meaning of the recorded outcome. Never derived from a preview. */
  classification: AnalysisSummaryClassification
  last_result?: AnalysisLastResult
}

/** What returned rows mean. Absent is read as `informational`. */
export interface AnalysisOutcomePolicy {
  mode?: 'exception_rows' | 'informational'
}

export type AnalysisResultState = 'current' | 'stale' | 'not_run'

/**
 * One recomputed analysis. The frame and stats here are a *preview* of what the
 * spec returns now; `last_result` is what an execution durably concluded. They
 * can legitimately disagree, and the recorded one is the procedure's status.
 */
export interface AnalysisDetail extends SavedAnalysis {
  error: string | null
  frame?: FramePayload | null
  total_rows?: number
  verdict?: 'ok' | 'warn' | 'fail' | 'info'
  verdict_text?: string
  stats?: StatChip[]
  code?: string
  stdout?: string | null
}

/** Bounded outcome persisted by an analysis execution, agent-run or manual. */
export interface AnalysisLastResult {
  /** `manual_…` for an execution the auditor started; otherwise the run id. */
  run_id: string
  executed_at: string
  status: 'ok' | 'error'
  error: string | null
  verdict: 'ok' | 'warn' | 'fail' | 'info' | null
  verdict_text: string | null
  row_count: number
  column_count: number
  stat_count: number
  stats: StatChip[]
  /** How many rows the procedure flagged. `exception_rows_retained` of them
   *  are readable back through the exceptions endpoint; the rest were capped. */
  exception_count: number
  exception_rows_retained: number
  input_sha1?: string
  result_sha1?: string
}

/**
 * The rows a recorded result concluded about, read back without recomputing.
 * `frame` is null when the procedure flagged nothing, or when the evidence no
 * longer belongs to the result currently on the definition.
 */
/**
 * The exploratory-analysis memo. Derived and read-only: it is regenerated from
 * the recorded results rather than edited, which is why it always agrees with
 * the procedures it cites. `stale` means the results moved after it was written.
 */
export interface AnalysisMemo {
  markdown: string
  cited_analysis_ids: string[]
  generated_at: string | null
  run_id: string | null
  stale: boolean
}

export interface AnalysisExceptions {
  analysis_id: string
  exception_count: number
  retained: number
  run_id: string | null
  executed_at: string | null
  frame: FramePayload | null
}

/** True when a result was recorded by the auditor rather than by an agent run. */
export function isManualResult(result: AnalysisLastResult | undefined | null): boolean {
  return Boolean(result?.run_id?.startsWith('manual_'))
}

export type AnalysisSummaryClassification =
  | 'exception'
  | 'unusual'
  | 'execution_error'
  | 'clear'
  | 'informational'
  | 'stale'
  | 'not_run'

export interface AnalysisSummaryItem {
  analysis_id: string
  title: string
  table: string | null
  kind: 'analytics' | 'python'
  source: 'library' | 'ai' | 'code'
  classification: AnalysisSummaryClassification
  state: AnalysisResultState
  run_id: string | null
  manual: boolean
  executed_at: string | null
  status: 'ok' | 'error' | null
  verdict: 'ok' | 'warn' | 'fail' | 'info' | null
  verdict_text: string | null
  error: string | null
  row_count: number
  stats: StatChip[]
  result_sha1: string | null
}

export interface AnalysisSummaryPayload {
  counts: {
    exception: number
    unusual: number
    errors: number
    clear: number
    informational: number
    stale: number
    not_run: number
  }
  items: AnalysisSummaryItem[]
}

// ------------------------------------------------------------- validation
export interface CheckParamMeta {
  name: string
  kind:
    | 'number'
    | 'select'
    | 'text'
    | 'date'
    | 'toggle'
    | 'column'
    | 'columns'
    | 'values'
    | 'table'
    | 'lookup_column'
    | 'code'
  label: string
  optional?: boolean
  default?: string | number | boolean
  options?: { label: string; value: string | number }[]
}

export interface CheckMeta {
  id: string
  label: string
  icon: string
  scope: 'column' | 'table'
  column_kinds: string[]
  description: string
  params: CheckParamMeta[]
  needs_lookup?: boolean
}

export interface ValidationRule {
  id: string
  column: string | null
  check: string
  params: Record<string, unknown>
  severity: 'fail' | 'warn'
  enabled: boolean
}

// A saved rule set: field-wise checks bound to a table by name, stored as a
// spec (like analyses) and recomputed live on every run.
export interface RuleSet {
  id: string
  title: string
  table: string
  rules: ValidationRule[]
  note: string
  created: string
  // Summary-only history of saved-spec runs (never row data).
  runs?: RunSummary[]
}

export interface RunSummary {
  run_at: string
  table: string
  rows: number
  verdict: 'ok' | 'warn' | 'fail' | 'info'
  counts: { passed: number; warned: number; failed: number; errored: number; skipped: number }
}

export interface RuleResult {
  rule_id: string | null
  column: string | null
  check: string
  label: string
  severity: 'fail' | 'warn'
  verdict: 'ok' | 'warn' | 'fail' | 'error' | 'skipped'
  fail_count: number
  checked_rows: number
  pass_pct: number | null
  error: string | null
}

export interface ValidationRun {
  table: string
  rows: number
  run_at: string
  verdict: 'ok' | 'warn' | 'fail' | 'info'
  counts: { passed: number; warned: number; failed: number; errored: number; skipped: number }
  results: RuleResult[]
  // Present only on saved-ruleset runs, which record history server-side.
  history?: RunSummary[]
}

export interface ValidationDetail {
  label: string
  detail: FramePayload
  detail_rows: number
}

export interface ColumnValues {
  distinct: number
  truncated: boolean
  values: string[]
}

export interface AssistantStatus {
  configured: boolean
  backend: 'groq' | 'openrouter' | 'mistral' | 'lmstudio' | string
  provider?: 'groq' | 'openrouter' | 'mistral' | 'lmstudio' | string
  model: string
  base_url: string
  providers?: AssistantProvider[]
  vision_configured?: boolean
  vision_provider?: string
  vision_model?: string
  vision_unavailability_reason?: string | null
  text_profile?: ModelProfileStatus
  vision_profile?: ModelProfileStatus
  error?: string
}

export interface ModelProfileStatus {
  name: string
  provider: string
  model: string
  capabilities: string[]
  configuration_source: string
  configured: boolean
  base_url?: string
  profile_hash: string
  unavailability_reason?: string | null
}

export interface AssistantProvider {
  id: 'groq' | 'openrouter' | 'mistral' | 'lmstudio' | string
  label: string
  base_url: string
  api_key_env: string
  api_key_configured: boolean
  default_model: string
  models: string[]
  local: boolean
  vision?: boolean
  vision_model?: string
  model_capabilities?: Record<string, string[]>
}

export interface AssistantStep {
  tool: string
  args: Record<string, unknown>
  ok: boolean
}

export interface AssistantArtifact {
  id: string
  tool: 'query_table' | 'run_analytics' | 'run_python'
  title: string
  table: string | null
  kind: 'query' | 'analytics' | 'python'
  spec: Record<string, unknown>
  viz: VizSpec
  frame: FramePayload | null
  total_rows: number
  error: string | null
  code?: string
  stdout?: string | null
  verdict?: 'ok' | 'warn' | 'fail' | 'info'
  verdict_text?: string
  stats?: StatChip[]
  chat_id?: string
  message_id?: string
  created_at?: string
  updated_at?: string
  revision?: number
  last_run_at?: string | null
  last_error?: string | null
}

export type AssistantMessageIntent = 'auto' | 'ask' | 'act'

export interface AssistantChatSummary {
  id: string
  workspace_id: string
  title: string
  title_source: 'auto' | 'user'
  created_at: string
  updated_at: string
  message_count: number
}

export interface AssistantChatMessage {
  id: string
  ordinal: number
  type: 'message'
  derived: boolean
  role: 'user' | 'assistant'
  kind: 'text' | 'clarification' | 'error'
  content: string
  created_at: string
  request_id: string | null
  state: 'pending' | 'complete' | 'failed'
  requested_intent: AssistantMessageIntent | null
  resolved_intent: 'ask' | 'act' | 'interaction_response' | 'clarify' | null
  reply_to_id: string | null
  artifact_ids: string[]
  citations?: EvidenceRef[]
  citation_ids?: string[]
  tool_trace?: AssistantStep[]
  document_manifest?: AssistantAnswer['document_context']
  outcome: { kind: string; run_id?: string; command_id?: string; message_id?: string; position?: number } | null
  error: string | null
}

/** One line of the agent's live progress log, newest last. */
export interface AgentNarrationEntry {
  at: string
  kind: 'progress' | 'stage_started' | 'stage_settled' | string
  text: string
  stage_id: string | null
  unit_id: string | null
}

export interface AgentMilestone {
  id: string
  capability: string
  stage_id: string
  status: 'completed' | 'completed_with_issues' | 'needs_review' | string
  headline: string
  summary: string
  metrics: Array<{ label: string; value: string | number | boolean | null }>
  highlights: Array<{
    severity: 'info' | 'warning' | 'error' | string
    label: string
    detail: string
    artifact_ref: string | null
  }>
  /**
   * A short severity-graded tally, for a stage whose result is a distribution
   * rather than a list. Absent unless the stage fills it.
   */
  stats?: Array<{
    label: string
    value: string | number | boolean | null
    severity: 'info' | 'warning' | 'error' | string
  }>
  artifact_refs: string[]
  summary_sha1: string
  created_at: string
}

/**
 * The engagement record: what this engagement filed, keyed by the work product
 * rather than by the conversation that asked for it.
 *
 * Every attempt at one capability collapses into a single entry — the demo
 * engagement files nine work products across twenty-three milestones — so
 * `attempts` is how many runs it took and `elapsed_ms` is what all of them
 * cost together. Where `measured_attempts` is short of `attempts.length`, some
 * of those runs were cancelled or failed and their wall clock counts time
 * spent waiting for a person, so they are deliberately not timed.
 */
/**
 * What the runs behind one stage recorded. Absent on a stage no run ever filed,
 * which is a normal state and not an error: cost, attempts and the milestone's
 * own narrative are the things only a run knows, and the row stands without
 * them.
 */
export interface EngagementStageHistory {
  id: string
  capability: string
  at: string | null
  first_at: string | null
  status: string
  headline: string
  summary: string
  metrics: AgentMilestone['metrics']
  highlights: AgentMilestone['highlights']
  /** Empty on every stage whose result is not a distribution. */
  stats: NonNullable<AgentMilestone['stats']>
  objective: string
  run_id: string
  chat_id: string | null
  attempts: Array<{
    run_id: string
    run_status: string
    at: string | null
    elapsed_ms: number | null
  }>
  elapsed_ms: number | null
  measured_attempts: number
}

/**
 * One work product, drawn whether or not a run ever filed it.
 *
 * The row exists because the audit graph says the stage exists, so the ledger
 * survives a lost run folder, a stage that produces its artifact without
 * narrating, and a stage that is still running.
 */
export interface EngagementStage {
  id: string
  capability: string
  /** null where the engagement plan does not contain this capability at all. */
  order: number | null
  /** Whether the engagement holds the work product. */
  held: boolean
  runnable: boolean
  /** The imperative a stage carries while its work product is absent. */
  headline: string
  /** Empty when nothing holds the stage. */
  blocked_reason: string
  /** null on a stage the record cannot ask for, and on one already held. */
  start: { prompt: string; outcomes: string[] } | null
  /**
   * How this stage is begun. `run` sends the assistant its prompt; `import`
   * opens the shell's import dialog, because bringing in the audit file is the
   * auditor's own act and no assistant command performs it. Empty on a stage
   * that is never offered.
   */
  action: 'run' | 'import' | '' | string
  /**
   * Doors beside the artifact card, for a stage that opens more than one thing
   * — Sources holds documents and tables — or offers a tool alongside what it
   * filed. A `tool` link is not something the engagement holds, and is drawn
   * differently so the record never implies it is.
   */
  links: Array<{
    label: string
    destination: string
    count: number | null
    kind: 'artifact' | 'tool' | string
  }>
  /** Null for a capability the record has no artifact mapping for. */
  filed: {
    label: string
    /**
     * A `WorkspaceDestination`, but left wide here so `types.ts` stays free of
     * imports and a destination this build does not know degrades to an
     * unlinked row instead of throwing inside the router.
     */
    destination: string
    unit: string
    /** Declared beside the unit so irregulars ("analyses") are not guessed. */
    unit_plural: string
    count: number | null
  } | null
  /**
   * The graph's own answer about this stage, carried beside `held` rather than
   * collapsed into it. Readiness says what is *left*; `held` says what exists,
   * and on a register that is thirty drafted and two short both are true.
   */
  readiness: {
    state: string
    reasons: string[]
    details: Record<string, unknown>
  }
  /**
   * What the stage amounts to now: the sentence, the severity distribution,
   * and the rows worth reading out. Recomputed from the workspace where the
   * projection describes state rather than a run's delta — a matrix that filed
   * "22 high" over 25 rows and now holds 22 rows rated 5 high says so — and
   * falling back to what the run reported otherwise.
   */
  summary: string
  /** Empty on every stage whose result is not a distribution. */
  stats: NonNullable<AgentMilestone['stats']>
  highlights: AgentMilestone['highlights']
  /** Whether the body above was recomputed, or is the milestone's own. */
  live_body: boolean
  /** What this stage left open behind it. */
  open_points: EngagementOpenPoint[]
  history: EngagementStageHistory | null
}

/**
 * A debt left behind by a stage that completed. It hangs off the entry that
 * created it, or on `orphaned_points` when that entry is not on the record.
 */
export interface EngagementOpenPoint {
  key: string
  capability: string
  message: string
  action: string
  destination: string
}

export type EngagementNextStep =
  | ({ kind: 'open_point' } & EngagementOpenPoint)
  | ({ kind: 'stage' } & EngagementStage)

export interface EngagementRecordPayload {
  /** Every work product, in plan order. One list, not a filed half and an owed half. */
  stages: EngagementStage[]
  open_points: EngagementOpenPoint[]
  next: EngagementNextStep | null
  counts: Record<string, number>
  totals: {
    /** What the engagement holds, which is not what a run was seen to file. */
    work_products: number
    runs: number
    runs_that_filed: number
    attempts: number
    elapsed_ms: number | null
    first_at: string | null
    last_at: string | null
  }
}

/**
 * A unit that stopped because it needs a person, already turned into a
 * question. `suggestions` are ordinary chat commands, so answering a blocker
 * goes through the same path as anything else the auditor types.
 */
export interface AgentBlocker {
  unit_id: string
  unit_ids: string[]
  stage_id: string
  stage_title: string
  subject: string
  subjects: string[]
  code: string | null
  status: string
  severity: 'blocked' | 'failed' | 'review'
  message: string
  where: string | null
  suggestions: Array<{ label: string; command: string }>
}

export interface AssistantRunProjection extends AgentRunSummary {
  id: string
  type: 'run'
  derived: true
  run_id: string
  source_message_id?: string
  created_at: string
  title: string
  current_activity: string
  pending_attention: boolean
  summary_line: string
  /** Humanized terminal/active status; `status` stays the machine value. */
  status_label: string
  /** The plan in the auditor's words, resolved before execution starts. */
  plan_line: string
  narration: AgentNarrationEntry[]
  blockers: AgentBlocker[]
  /** Injected client-side for an active run owned by another chat. */
  foreign?: boolean
}

export interface AssistantInteractionProjection {
  id: string
  type: 'interaction'
  derived: true
  run_id: string
  created_at: string
  interaction: AgentInteraction
}

export interface AssistantApprovalProjection {
  id: string
  type: 'approval'
  derived: true
  run_id: string
  created_at: string
  approval: AgentApproval
}

/** One document a step read, or deliberately did not. */
export interface ContextDocument {
  document_id: string
  name: string
  category: string
  pages: number | null
  reason?: string
}

/**
 * What one step read, as structure rather than as a sentence.
 *
 * The prose line remains in the transcript as the accessible reading; this is
 * what the cards draw, so a reader sees at a glance which files the work rests
 * on and which were held out of scope.
 */
/** A work product a step read as input — the APM, the analysis summary. */
export interface ContextArtifact {
  ref: string
  name: string
  badge: string
  destination: string
}

export interface ContextRead {
  at: string
  stage_title: string
  artifacts: ContextArtifact[]
  documents: ContextDocument[]
  withheld: ContextDocument[]
  supporting: string[]
  unavailable: string[]
  /** The prose reading of the same manifest, for assistive technology. */
  sentence: string
}

export interface AssistantContextProjection {
  id: string
  type: 'context'
  derived: true
  run_id: string
  created_at: string
  context: ContextRead
}

export interface AssistantMilestoneProjection {
  id: string
  type: 'milestone'
  derived: true
  run_id: string
  created_at: string
  milestone: AgentMilestone
}

export interface AssistantSlashCommand {
  id: string
  slash: string
  label: string
  description: string
}

export interface AssistantCapabilities {
  ask: boolean
  act: boolean
  assistant: AssistantStatus
  agent: AssistantStatus
  commands: AssistantSlashCommand[]
}

export interface AssistantChat extends Omit<AssistantChatSummary, 'message_count'> {
  schema_version: number
  next_ordinal: number
  composer_context: { document_ids: string[] }
  messages: AssistantChatMessage[]
  transcript: Array<AssistantChatMessage | AssistantRunProjection | AssistantInteractionProjection | AssistantApprovalProjection | AssistantMilestoneProjection | AssistantContextProjection>
  artifacts: Record<string, AssistantArtifact>
  artifact_errors: Array<{ id: string; error: string }>
  runs: AssistantRunProjection[]
  missing_document_ids: string[]
  capabilities: AssistantCapabilities
  /** What this engagement actually needs next, from workspace readiness. */
  suggestions: AssistantSuggestion[]
  /** Guided workflow shortcuts that still have incomplete outcomes. */
  guided_workflows: AssistantGuidedWorkflow[]
  active_workspace_run: AssistantRunProjection | null
}

export interface AssistantSuggestion {
  capability: string
  requested_outcomes: string[]
  label: string
  command: string
  reason: string
}

export interface AssistantGuidedWorkflow {
  label: string
  command: string
}

export interface AssistantAnswer {
  answer: string
  steps: AssistantStep[]
  artifacts: AssistantArtifact[]
  citations: EvidenceRef[]
  document_context: {
    manifest: Array<{
      document_id: string
      title: string
      total_pages: number
      included_pages: number[]
      truncated_pages: number[]
      omitted_pages: number[]
      characters_included: number
      trimmed: boolean
      text_state: DocumentTextState
      context_outcome?: 'supplied' | 'trimmed' | 'scope_required' | 'unavailable'
      source_sha1?: string
    }>
    trimmed: boolean
    scope_required?: boolean
    character_budget: number
  } | null
}

export interface RunPythonResult {
  frame: FramePayload
  total_rows: number
  stdout: string | null
}

// ------------------------------------------------------------------ agent
// A deterministic (backend) rule suggestion: a draft ValidationRule plus the
// reason it was proposed. Shared by the Validation tab and agent runs.
export interface RuleSuggestion {
  column: string | null
  check: string
  params: Record<string, unknown>
  severity: 'fail' | 'warn'
  rationale: string
}

export type AgentRunStatus =
  | 'queued'
  | 'interpreting'
  | 'executing'
  | 'awaiting_approval'
  | 'awaiting_input'
  | 'verifying'
  | 'paused'
  | 'interrupted'
  | 'completed'
  | 'completed_with_open_items'
  | 'completed_with_issues'
  | 'completed_with_failures'
  | 'failed'
  | 'cancelled'

export type AgentTaskStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'completed'
  | 'skipped'
  | 'failed'
  | 'cancelled'

export interface AgentTask {
  id: string
  stage: string
  title: string
  detail: string
  status: AgentTaskStatus
  error: string | null
  result_refs: string[]
  started_at?: string | null
  finished_at?: string | null
}

export interface AgentActivity {
  phase: string
  label: string
  detail: string | null
  current: number | null
  total: number | null
  attempt: number | null
  task_id: string | null
  action_id: string | null
  started_at: string
  updated_at: string
  waiting_on: 'model' | null
  model_calls_active: number
  model_started_at: string | null
}

export interface AgentStage {
  id: string
  title: string
  tasks: AgentTask[]
}

export type WorkflowReadinessState =
  | 'satisfied' | 'missing' | 'stale' | 'blocked' | 'review_required'

export type WorkflowUnitStatus =
  | 'queued' | 'running' | 'succeeded' | 'failed' | 'blocked'
  | 'awaiting_input' | 'awaiting_confirmation' | 'conflict'
  | 'skipped' | 'cancelled'

export interface WorkflowSidecarReference {
  path: string
  sha1?: string
  unit_id?: string
  manifest_hash?: string
  payload_hash?: string
  receipt_hash?: string
}

export interface WorkflowUnit {
  id: string
  kind: string
  title: string
  capability: string
  parent_refs: string[]
  status: WorkflowUnitStatus
  attempts: number
  input_sha1: string
  context_manifest: WorkflowSidecarReference | null
  proposal_sidecar: WorkflowSidecarReference | null
  receipt_sidecar: WorkflowSidecarReference | null
  result_refs: string[]
  error: string | null
  started_at: string | null
  finished_at: string | null
}

export interface WorkflowStage {
  id: string
  capability: string
  title: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'review_required' | 'blocked' | 'skipped' | 'cancelled'
  barrier: string
  units: WorkflowUnit[]
  readiness_before?: { state: WorkflowReadinessState; reasons?: string[]; blocking_on?: string[] }
  started_at?: string | null
  finished_at?: string | null
}

export interface AgentWorkflow {
  definition:
    | 'audit_workflow_v2'
    | 'analysis_workflow_v1'
    | 'documents_workflow_v1'
    | 'doc_tests_workflow_v2'
    | string
  definition_hash?: string
  revision: number
  route: 'workflow'
  requested_outcomes: string[]
  target_refs: string[]
  // Normalized scope the run resolved (target refs plus any workflow-specific
  // selection, such as the tables an analysis run covers).
  scope?: Record<string, unknown>
  generation_mode: 'reuse_existing' | 'force'
  workflow_explanation: string
  next_outcomes: string[]
  pending_checkpoint: string | null
  resolved_capabilities: string[]
  reused_capabilities: string[]
  reused_capability_details: Array<{ capability: string; currency_status: 'not_assessed' }>
  workspace_revision: number
  stages: WorkflowStage[]
}

// The one normalized route a command run persists before its worker thread
// launches. `status: 'pending'` means the deterministic pass could not classify
// the command and the bounded router worker decides on the thread; every other
// record carries a resolved route and, for workflow/action, a selected engine.
export interface AgentRoute {
  status: 'pending' | 'resolved'
  route: 'workflow' | 'action' | 'clarification' | 'unsupported' | null
  engine: 'workflow' | 'action' | null
  decided_by: string | null
  workflow_definition: string | null
  requested_outcomes: string[]
  objective: string
  target_refs: string[]
  generation_mode: 'reuse_existing' | 'force'
  action_intent: string | null
  constraints: string[]
  clarification: string | null
}

export interface AgentApprovalItem {
  id: string
  title: string
  rationale: string
  spec: Record<string, unknown>
  evidence: Record<string, unknown>
  decision: 'approved' | 'rejected' | 'edited' | null
  edited_spec: Record<string, unknown> | null
}

export interface AgentApproval {
  id: string
  kind: 'join' | 'rules' | 'tests' | string
  task_id: string
  status: 'pending' | 'resolved'
  created: string
  resolved?: string
  items: AgentApprovalItem[]
}

export interface AgentMessage {
  role: 'user' | 'agent'
  content: string
  at: string
  handled?: boolean
}

export interface AgentFinding {
  id: string
  severity: 'high' | 'medium' | 'low' | 'info'
  statement: string
  basis: 'observed' | 'interpretation'
  evidence_refs: string[]
}

export interface AgentRunContext {
  objective?: string
  period?: string
  materiality?: string | number | null
  notes?: string
  batch_id?: string
  source_id?: string
  test_id?: string
  document_ids?: string[]
}

export interface AgentRejectedProposal {
  at: string
  stage: 'command_interpreter' | 'command_planner' | string
  error: string
  actions: {
    id: string
    type: string
    target: { kind: string | null; selector: string | null; resolved_id: string | null }
    depends_on: string[]
    arg_keys: string[]
  }[]
}

export interface AgentAuditOutcome {
  audit_complete: boolean
  completion_status: string
  tests_total: number
  tests_completed: number
  tests_review_required: number
  tests_blocked: number
  tests_unspecified: number
  data_tests_executed: number
  document_tests_executed: number
  recorded_exception_observations: number
  supported_findings: number
  draft_findings: number
  report_quality_ok: boolean | null
  report_quality_errors: number
  output_readiness?: Record<string, { state: WorkflowReadinessState; reasons?: string[] }>
  open_gate_count: number
}

export interface AgentRun {
  schema_version?: number
  // Null only while a command run's route is still pending; dispatch requires a
  // supported value and fails closed without one.
  engine: 'workflow' | 'action' | 'intake' | null
  route?: AgentRoute | null
  id: string
  workspace_id: string
  parent_run_id: string | null
  planning_basis_run_id?: string | null
  chat_id?: string | null
  source_message_id?: string | null
  kind: 'audit' | 'intake'
  mode: 'auto' | 'permission'
  context: AgentRunContext
  status: AgentRunStatus
  created: string
  started: string | null
  finished: string | null
  duration_ms?: number | null
  activity?: AgentActivity | null
  activity_revision?: number
  usage: {
    llm_turns: number
    tool_calls: number
    planner_waves?: number
    actions_started?: number
    prompt_tokens?: number
    completion_tokens?: number
    estimated_prompt_tokens?: number
    request_characters?: number
    retries?: number
    max_concurrent_model_calls?: number
    model_calls_by_worker?: Record<string, number>
    model_usage_by_worker?: Record<string, {
      calls: number
      prompt_tokens: number
      completion_tokens: number
      request_characters: number
      latency_ms: number
      retries: number
    }>
    model_call_metrics?: Array<{
      worker: string
      request_characters: number
      estimated_input_tokens: number
      prompt_tokens: number
      completion_tokens: number
      latency_ms: number
      retry_number: number
      context_metrics?: Record<string, unknown> | null
    }>
  }
  plan: { stages: AgentStage[] }
  approvals: AgentApproval[]
  messages: AgentMessage[]
  /** Absent on runs recorded before narration existed. */
  narration?: AgentNarrationEntry[]
  /** Deterministic workflow result messages, absent on older runs. */
  milestones?: AgentMilestone[]
  artifacts: { kind: string; id: string; semantic_id: string; action: string }[]
  findings: AgentFinding[]
  finding_refs?: string[]
  supported_finding_refs?: string[]
  draft_finding_refs?: string[]
  audit_outcome?: AgentAuditOutcome
  summary_markdown: string | null
  warnings: string[]
  error: string | null
  cancellation?: {
    actor: string
    source: string
    reason: string | null
    requested_at: string | null
    cancelled_at: string
  } | null
  command?: AgentCommand
  goal?: AgentGoal
  graph_revision?: number
  actions?: AgentAction[]
  rejected_proposals?: AgentRejectedProposal[]
  target_adjustments?: Record<string, unknown>[]
  interactions?: AgentInteraction[]
  workflow?: AgentWorkflow
  workflow_explanation?: string
  pending_commands?: AgentCommand[]
  interview?: {
    captured: Record<string, unknown>
    turns: number
    pending_question: string | null
  }
}

export interface AgentRunSummary {
  id: string
  engine: AgentRun['engine']
  route?: AgentRoute | null
  workspace_id: string
  parent_run_id: string | null
  planning_basis_run_id?: string | null
  chat_id?: string | null
  source_message_id?: string | null
  kind: 'audit' | 'intake'
  mode: 'auto' | 'permission'
  status: AgentRunStatus
  created: string
  started: string | null
  finished: string | null
  duration_ms?: number | null
  activity?: AgentActivity | null
  activity_revision?: number
  task_counts: { total: number; completed: number; failed: number; blocked?: number }
  error: string | null
  cancellation?: AgentRun['cancellation']
  has_summary: boolean
  requested_outcomes?: string[]
  next_outcomes?: string[]
  workflow_explanation?: string | null
}

export interface AgentEvent {
  seq: number
  at: string
  type: string
  data: Record<string, unknown>
}

export interface WorkspaceChange {
  kind: 'table' | 'join' | 'ruleset' | 'analysis' | string
  id: string
  action: 'created' | 'updated' | 'removed' | string
}

export interface AgentDecision {
  item_id: string
  action: 'approve' | 'reject' | 'edit'
  spec?: Record<string, unknown>
}

export interface AgentCommand {
  id: string
  source: 'chat' | 'goal_template' | 'tab_button' | 'follow_up'
  text: string
  goal_template: string | null
  submitted_at: string
  status: string
  parent_command_id: string | null
  chat_id?: string | null
  source_message_id?: string | null
  context_refs?: Array<Record<string, unknown>>
}

export interface AgentGoal {
  objective: string
  constraints: string[]
  completion_criteria: string[]
}

export type AgentActionStatus =
  | 'proposed' | 'awaiting_input' | 'awaiting_confirmation' | 'ready'
  | 'blocked' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'cancelled'

export interface AgentAction {
  id: string
  command_id: string
  type: string
  definition_version: number
  args: Record<string, unknown>
  target: { kind: string | null; selector: string | null; resolved_id: string | null }
  resolution: {
    resolved_ref?: string | null
    title?: string
    confidence?: number
    reason?: string
  } | null
  depends_on: string[]
  depth: number
  status: AgentActionStatus
  attempts: number
  prepared_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  result_refs: string[]
  receipt?: { result?: Record<string, unknown> } | null
  error: string | null
}

export type AgentInteractionType =
  | 'clarification' | 'target_choice' | 'confirmation'
  | 'proposal_approval' | 'conflict_resolution'

export interface AgentInteractionOption {
  value?: string
  ref?: string
  id?: string
  label?: string
  title?: string
  reason?: string
  score?: number
}

export interface AgentInteraction {
  id: string
  action_id: string
  type: AgentInteractionType
  prompt: string
  options: AgentInteractionOption[]
  payload: Record<string, unknown>
  policy_reason: string
  status: 'pending' | 'resolved'
  response: Record<string, unknown> | null
  actor: string | null
  created_at: string
  resolved_at: string | null
}


// --------------------------------------------------------------------------
// Provenance — what the agent read, proposed, and committed for one unit.
// Content-free by construction: the manifest carries identities and sizes, the
// proposal is withheld, and the receipt is commit metadata.
// --------------------------------------------------------------------------
export type ProvenanceState = 'available' | 'absent' | 'unavailable' | 'invalid'

export interface ProvenanceSize {
  items: number
  characters: number
  estimated_tokens: number
  media_items?: number
  media_bytes?: number
  media_pixels?: number
  estimated_image_tokens?: number
}

export interface ProvenanceSelection {
  source_id: string
  source_type: string
  source_ref: string
  source_hash: string
  selector_kind: string
  selector_id: string
  selector_definition_hash: string
  reason: string
  representation: { kind: string; options: Record<string, unknown> }
  supplied_size: ProvenanceSize
  media?: Record<string, unknown> | null
}

export interface ProvenanceOmission {
  source_id: string
  source_ref: string | null
  source_hash: string | null
  reason: string
}

export interface ProvenanceTruncation {
  source_id: string
  source_ref: string
  reason: string
  original_size: ProvenanceSize
  supplied_size: ProvenanceSize
}

export interface ProvenanceContext {
  state: ProvenanceState
  /** Present whenever `state` is not `available`. */
  reason?: string
  manifest_hash?: string
  context_spec_hash?: string
  resolver_hash?: string
  supplied_size?: ProvenanceSize
  selections?: ProvenanceSelection[]
  omissions?: ProvenanceOmission[]
  truncations?: ProvenanceTruncation[]
  privacy_decisions?: Array<Record<string, unknown>>
}

export interface ProvenanceProposal {
  state: ProvenanceState
  reason?: string
  payload_hash?: string
  /** Always true when available: the body is the work product, not provenance. */
  content_withheld?: boolean
}

export interface ProvenanceReceipt {
  state: ProvenanceState
  reason?: string
  receipt_hash?: string
  proposal_hash?: string
  executor_id?: string
  executor_definition_hash?: string
  artifact_refs?: string[]
  postcondition_hashes?: Record<string, string>
  workspace_revision_before?: number
  workspace_revision_after?: number
  reconciled?: boolean
  output?: Record<string, unknown>
}

export interface ProvenanceModel {
  worker_kind: string
  provider: string
  model: string
  profile_hash: string
  configuration_source: string
  /** Usage is accounted per worker across the run, not per unit. */
  scope: 'worker_across_run'
  usage: Record<string, number>
}

export interface UnitProvenance {
  run_id: string
  run_status: string
  workflow_definition: string
  unit: {
    id: string
    title: string
    kind: string
    capability: string
    stage_id: string
    stage_title: string
    status: string
    attempts: number
    started_at: string | null
    finished_at: string | null
    error: string | null
  }
  context: ProvenanceContext
  proposal: ProvenanceProposal
  receipt: ProvenanceReceipt
  model: ProvenanceModel
}

export type ArtifactProvenance =
  | ({ state: 'attributed'; artifact_ref: string } & UnitProvenance)
  | { state: 'unattributed'; artifact_ref: string; reason: string }

// --------------------------------------------------------------------------
// Engagement brief — what the agent would do, stated before it starts.
// --------------------------------------------------------------------------
export interface EngagementOutcome {
  capability: string
  title: string
  stage_id: string
}

export type EngagementEstimate =
  | {
      state: 'measured'
      runs_observed: number
      basis: string
      median_model_calls: number
      median_minutes: number
      slowest_minutes: number
      caveat: string
    }
  | { state: 'insufficient_history'; runs_observed: number; reason: string }

export interface EngagementDestination {
  configured: boolean
  provider: string
  model: string
  local: boolean
  summary: string
}

/** `outcomes` grouped into the phases an auditor recognises. */
export interface EngagementPlanPhase {
  id: string
  title: string
  summary: string
  steps: EngagementOutcome[]
}

export interface EngagementPlan {
  template: string
  outcomes: EngagementOutcome[]
  phases: EngagementPlanPhase[]
  estimate: EngagementEstimate
  destination: EngagementDestination
  gates: { mode: 'auto' | 'permission'; summary: string }
}

/** Optional engagement details collected when a workspace is created. */
export interface EngagementBrief {
  entity?: string
  period?: string
  materiality?: string
  background_notes?: string
}
