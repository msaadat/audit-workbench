export interface WorkspaceListItem {
  id: string
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
  name: string
  description: string
  created: string
  tables: TableInfo[]
  tile_count?: number
  document_count?: number
  finding_count?: number
  settings?: { doc_llm_optin: boolean; doc_llm_optin_at: string | null; doc_pii_masking?: boolean }
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
  version: number
  supersedes: string | null
  text_state: DocumentTextState
  note: string
  created: string
  created_by: string
  agent_run_id: string | null
}

export interface DocumentPage {
  page: number
  text: string
  characters: number
  embedded_images: number
  image_only: boolean
}

export interface EvidenceRef {
  id: string
  source_kind: 'document' | 'table' | 'analysis' | 'ruleset' | 'doctest' | 'procedure'
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
  legacy_ref?: string
}

export interface DisclosureEvent {
  id: string
  at: string
  document_id: string
  source_sha1: string
  pages: number[]
  purpose: string
  pii_masked: boolean
  characters_disclosed?: number
  truncated_pages?: number[]
  omitted_pages?: number[]
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
  state: DocTestItemState
  auditor_disposition: 'pending' | 'accepted' | 'exception' | 'needs_manual_check'
  auditor_note: string
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
  runner_note?: string
  document_conflicts?: { duplicate_documents: string[][]; version_conflicts: Array<{ newer: string; older: string }> }
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

export interface DocTest {
  id: string
  kind: DocTestKind
  title: string
  status: 'draft' | 'in_progress' | 'completed'
  semantic_id: string
  rcm_refs: string[]
  procedure_refs: string[]
  spec: Record<string, unknown>
  items: DocTestItem[]
  item_count?: number
  state_counts?: Record<string, number>
  rollup?: DocTestRollup
  created: string
  updated: string
  sha1: string
}

export interface WorkingPaper {
  procedure_id: string
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
  status: 'draft' | 'final'
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
  test_procedure: string
  test_refs: string[]
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
  document_tests: DocTest[]
  findings: AuditFinding[]
  finding_rollups: FindingRollups
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
  status: 'draft' | 'final'
  rcm_refs: string[]
  procedure_refs: string[]
  evidence_refs: EvidenceRef[]
  source: 'agent' | 'manual' | 'promoted'
  created: string
  updated: string
}

export interface FindingSummary {
  id: string
  title: string
  severity: FindingSeverity
  status: 'draft' | 'final'
}

export interface FindingRollups {
  by_rcm: Record<string, FindingSummary[]>
  by_procedure: Record<string, FindingSummary[]>
}

export interface FindingsPayload {
  items: AuditFinding[]
  rcm: RcmRow[]
  procedures: AuditProcedure[]
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
  status: 'draft' | 'final'
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
  procedures: Array<Record<string, unknown>>
  document_tests: Array<Record<string, unknown>>
  findings: Array<Record<string, unknown>>
  scope_limitations: Array<{ procedure_id: string; text: string }>
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
  procedures: number
  document_tests: number
  analyses: number
  rulesets: number
  findings: number
  final_findings: number
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
  error?: string
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
}

export interface AssistantAnswer {
  answer: string
  steps: AssistantStep[]
  artifacts: AssistantArtifact[]
  disclosure: string
  citations: EvidenceRef[]
  document_context: {
    manifest: Array<{
      document_id: string
      title: string
      total_pages: number
      included_pages: number[]
      truncated_pages: number[]
      omitted_pages: number[]
      characters_disclosed: number
      trimmed: boolean
      text_state: DocumentTextState
    }>
    trimmed: boolean
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
  | 'discovering'
  | 'planning'
  | 'executing'
  | 'awaiting_approval'
  | 'awaiting_input'
  | 'verifying'
  | 'summarizing'
  | 'paused'
  | 'interrupted'
  | 'completed'
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

export interface AgentTask {
  id: string
  stage: string
  title: string
  detail: string
  status: AgentTaskStatus
  error: string | null
  result_refs: string[]
  disclosure: string[]
}

export interface AgentStage {
  id: string
  title: string
  tasks: AgentTask[]
}

export interface AgentApprovalItem {
  id: string
  title: string
  rationale: string
  spec: Record<string, unknown>
  evidence: Record<string, unknown>
  disclosure: string
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

export interface AgentDiscovery {
  domain?: string
  confidence?: 'high' | 'medium' | 'low'
  table_roles?: Record<string, string>
  assumptions?: string[]
  warnings?: string[]
  tables?: { table: string; rows: number; columns: number }[]
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

export interface AgentRun {
  schema_version?: number
  id: string
  workspace_id: string
  parent_run_id: string | null
  kind: 'audit' | 'analysis' | 'intake' | 'planning' | 'doc_test'
  mode: 'auto' | 'permission'
  context: AgentRunContext
  status: AgentRunStatus
  created: string
  started: string | null
  finished: string | null
  usage: {
    llm_turns: number
    tool_calls: number
    custom_analyses?: number
    planner_waves?: number
    actions_started?: number
  }
  discovery: AgentDiscovery
  plan: { stages: AgentStage[] }
  approvals: AgentApproval[]
  messages: AgentMessage[]
  artifacts: { kind: string; id: string; semantic_id: string; action: string }[]
  findings: AgentFinding[]
  summary_markdown: string | null
  warnings: string[]
  error: string | null
  command?: AgentCommand
  goal?: AgentGoal
  graph_revision?: number
  actions?: AgentAction[]
  interactions?: AgentInteraction[]
  pending_commands?: AgentCommand[]
  interview?: {
    captured: Record<string, unknown>
    turns: number
    pending_question: string | null
  }
}

export interface AgentRunSummary {
  id: string
  workspace_id: string
  parent_run_id: string | null
  kind: 'audit' | 'analysis' | 'intake' | 'planning' | 'doc_test'
  mode: 'auto' | 'permission'
  status: AgentRunStatus
  created: string
  started: string | null
  finished: string | null
  domain?: string | null
  task_counts: { total: number; completed: number; failed: number; blocked?: number }
  error: string | null
  has_summary: boolean
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
  result_refs: string[]
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
