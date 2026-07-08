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
  kind: 'query' | 'analytics' | 'python' | 'pivot'
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
  source: 'library' | 'ai'
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

export interface AssistantStatus {
  configured: boolean
  backend: 'groq' | 'openrouter' | 'lmstudio' | string
  model: string
  base_url: string
  error?: string
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
