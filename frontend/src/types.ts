export interface WorkspaceListItem {
  id: string
  revision: number
  name: string
  description: string
  created: string
  table_count: number
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
  tile_count?: number
  document_count?: number
  finding_count?: number
}

export type DocumentCategory = 'background' | 'policy' | 'regulation' | 'contract' | 'minutes' | 'voucher' | 'evidence' | 'prior_report' | 'correspondence' | 'other'
export type DocumentTextState = 'pending' | 'extracted' | 'image_only' | 'partial' | 'failed'

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
  tile_order?: number
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
  audit_notes_markdown: string
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

export type DocTestKind = 'vouching' | 'attribute' | 'review' | 'qa'
export type DocTestItemState = 'pending' | 'agent_checked' | 'confirmed' | 'exception' | 'manual_review'

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
  verdict: 'pending' | 'match' | 'mismatch' | 'missing' | 'invalid'
  note: string
  comparisons: DocComparison[]
  evidence_refs: EvidenceRef[]
}

export interface DocTestItem {
  id: string
  label: string
  /** The procedure being performed on this item, always populated. */
  instruction: string
  state: DocTestItemState
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
  evidence_coverage?: {
    document_ids: string[]
    available_document_types: string[]
    missing_document_types: string[]
    image_only: boolean
  }
  evidence_request_ids?: string[]
}

export interface DocTestRollup {
  items: number
  matched: number
  mismatched: number
  confirmed: number
  exceptions: number
  manual_review: number
  pending: number
}

/** What the auditor still has to do about one worklist item. */
export type DocTestClassification =
  | 'exception'
  | 'needs_review'
  | 'awaiting_evidence'
  | 'confirmed'
  | 'not_run'

export type DocTestCounts = Record<DocTestClassification, number>

/** One worklist item, flattened across tests for engagement-level triage. */
export interface DocTestSummaryItem {
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

export interface DocTestSummaryTest {
  test_id: string
  title: string
  kind: DocTestKind | null
  status: TestStatus
  rcm_id: string | null
  item_count: number
  counts: DocTestCounts
  objective: string
  result_summary: string
  conclusion: string
  control_conclusion: ControlConclusion
  scope_limitations: string
  next_action: string
  exception_count: number
  open_exception_count: number
  updated: string
}

export interface DocTestSummaryPayload {
  counts: DocTestCounts
  items: DocTestSummaryItem[]
  tests: DocTestSummaryTest[]
}

export interface DocTest extends TestPlan, TestOutcome {
  id: string
  kind: DocTestKind | null
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
  assertion: string
  control: string
  control_type: string
  control_owner: string
  criteria: string
  criteria_refs: unknown[]
  test_refs: string[]
  execution_rollup: RcmExecutionRollup
  finding_refs: string[]
  evidence_refs: EvidenceRef[]
  prepared_by: string | null
  reviewed_by: string | null
  review_status: 'draft' | 'prepared' | 'review_required' | 'reviewed'
  updated: string
}

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
  control_conclusion?: string
  findings?: number
  review_status?: string
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
  status: TestStatus
  semantic_warnings: string[]
  last_run: DataTestRunSummary | null
  evidence_refs: EvidenceRef[]
  created_by: 'agent' | 'user'
  agent_run_id: string | null
  created: string
  updated: string
}

export interface DataTestResult extends DataTestRunSummary {
  data_test_id: string
  rcm_id: string | null
  verdict_text: string
  statistics: Array<{ label: string; value: string }>
  viz: Record<string, unknown> | null
  stdout: string
  summary_frame: FramePayload | null
  exception_frame: FramePayload | null
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
  condition: string
  criteria: string
  cause: string
  effect: string
  recommendation: string
  management_response: string
  rcm_refs: string[]
  procedure_refs: string[]
  test_refs: string[]
  execution_refs: string[]
  evidence_refs: EvidenceRef[]
  cause_pending: boolean
  severity_rationale: string
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
  html: string
  quality: ReportQuality
  requires_reconcile?: boolean
  current_markdown?: string
  candidate_markdown?: string
  used_model?: boolean
  chunked?: boolean
}

export interface ReportContext {
  workspace: { id: string; name: string; description: string }
  planning: Record<string, unknown>
  rcm: Array<Record<string, unknown>>
  rcm_rollup: Array<Record<string, unknown>>
  data_tests: Array<Record<string, unknown>>
  document_tests: Array<Record<string, unknown>>
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
  estimated_size_mb: number
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

export interface DashboardTile {
  id: string
  title: string
  kind: 'query' | 'analytics' | 'python' | 'pivot' | 'validation'
  table: string | null
  note: string
  viz: VizSpec
  created: string
  error: string | null
  frame?: FramePayload | null
  total_rows?: number
  verdict?: 'ok' | 'warn' | 'fail' | 'info'
  verdict_text?: string
  stats?: StatChip[]
  code?: string
  stdout?: string | null
}

export interface DashboardTarget {
  tab: 'dashboard' | 'planning' | 'documents' | 'doc-tests' | 'data' | 'query' | 'validation' | 'analysis' | 'findings' | 'report'
  query: Record<string, string>
}

export interface DashboardPhase {
  id: 'planning' | 'fieldwork' | 'report'
  label: string
  state: 'not_started' | 'in_progress' | 'complete' | 'attention'
  complete: boolean
  summary: string
  counts: Record<string, number>
  issues: string[]
  target: DashboardTarget
}

export interface EngagementStatusPayload {
  phases: DashboardPhase[]
}

export interface DashboardAction {
  id: string
  title: string
  reason: string
  priority: 'high' | 'medium' | 'low'
  source: 'deterministic' | 'ai'
  target: DashboardTarget
}

export interface DashboardAttention {
  id: string
  severity: 'error' | 'warning' | 'info'
  title: string
  message: string
  target: DashboardTarget
}

export interface DashboardAdvice {
  items: DashboardAction[]
  generated_at: string
  provider: string
  model: string
  input_hash: string
  stale: boolean
}

export interface DashboardOverview {
  tables: number
  readable_tables: number
  table_errors: number
  rows: number
  documents: number
  rcm_rows: number
  tests: number
  data_tests: number
  document_tests: number
  findings: number
  pinned_tiles: number
  report_errors: number
  report_warnings: number
}

export interface DashboardPayload {
  overview: DashboardOverview
  phases: DashboardPhase[]
  actions: DashboardAction[]
  attention: DashboardAttention[]
  ai_advice: DashboardAdvice | null
  tiles: DashboardTile[]
}

// A saved analysis: the computed payload the Analysis rail + detail render.
// Same spec-recompute shape as DashboardTile, restricted to the two kinds the
// Analysis tab creates, plus a `source` for the rail icon.
export interface SavedAnalysis {
  id: string
  title: string
  kind: 'analytics' | 'python'
  table: string | null
  note: string
  viz: VizSpec
  source: 'library' | 'ai' | 'code'
  created: string
  error: string | null
  frame?: FramePayload | null
  total_rows?: number
  verdict?: 'ok' | 'warn' | 'fail' | 'info'
  verdict_text?: string
  stats?: StatChip[]
  code?: string
  stdout?: string | null
  spec?: Record<string, unknown>
  last_result?: AnalysisLastResult
}

/** Bounded outcome persisted by an exploratory-analysis workflow run. */
export interface AnalysisLastResult {
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
  input_sha1?: string
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
  state: 'current' | 'stale' | 'not_run'
  run_id: string | null
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
    needs_review: number
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
// spec (like analyses/tiles) and recomputed live on every run.
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
  artifact_refs: string[]
  summary_sha1: string
  created_at: string
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

export interface AssistantMilestoneProjection {
  id: string
  type: 'milestone'
  derived: true
  run_id: string
  created_at: string
  milestone: AgentMilestone
}

export interface AssistantCapabilities {
  ask: boolean
  act: boolean
  assistant: AssistantStatus
  agent: AssistantStatus
}

export interface AssistantChat extends Omit<AssistantChatSummary, 'message_count'> {
  schema_version: number
  next_ordinal: number
  composer_context: { document_ids: string[] }
  messages: AssistantChatMessage[]
  transcript: Array<AssistantChatMessage | AssistantRunProjection | AssistantInteractionProjection | AssistantApprovalProjection | AssistantMilestoneProjection>
  artifacts: Record<string, AssistantArtifact>
  artifact_errors: Array<{ id: string; error: string }>
  runs: AssistantRunProjection[]
  missing_document_ids: string[]
  capabilities: AssistantCapabilities
  /** What this engagement actually needs next, from workspace readiness. */
  suggestions: AssistantSuggestion[]
  active_workspace_run: AssistantRunProjection | null
}

export interface AssistantSuggestion {
  capability: string
  requested_outcomes: string[]
  label: string
  command: string
  reason: string
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
    | 'doc_tests_workflow_v1'
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
  kind: 'table' | 'join' | 'ruleset' | 'analysis' | 'tile' | string
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

export interface EngagementPlan {
  template: string
  outcomes: EngagementOutcome[]
  estimate: EngagementEstimate
  destination: EngagementDestination
  gates: { mode: 'auto' | 'permission'; summary: string }
}

/** The planning-context fields a brief fills. */
export interface EngagementBrief {
  objective: string
  scope: string
  entity?: string
  period?: string
  materiality?: string
  background_notes?: string
}
