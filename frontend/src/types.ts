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
  | 'discovering'
  | 'planning'
  | 'executing'
  | 'awaiting_approval'
  | 'verifying'
  | 'summarizing'
  | 'paused'
  | 'interrupted'
  | 'completed'
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
}

export interface AgentRun {
  id: string
  workspace_id: string
  parent_run_id: string | null
  mode: 'auto' | 'permission'
  context: AgentRunContext
  status: AgentRunStatus
  created: string
  started: string | null
  finished: string | null
  usage: { llm_turns: number; tool_calls: number; custom_analyses: number }
  discovery: AgentDiscovery
  plan: { stages: AgentStage[] }
  approvals: AgentApproval[]
  messages: AgentMessage[]
  artifacts: { kind: string; id: string; semantic_id: string; action: string }[]
  findings: AgentFinding[]
  summary_markdown: string | null
  warnings: string[]
  error: string | null
}

export interface AgentRunSummary {
  id: string
  workspace_id: string
  parent_run_id: string | null
  mode: 'auto' | 'permission'
  status: AgentRunStatus
  created: string
  started: string | null
  finished: string | null
  domain?: string | null
  task_counts: { total: number; completed: number; failed: number }
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
