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
  aggs: AggSpec[]
  sort: { column: string; desc: boolean }[]
  page: number
  page_size: number
}

export interface PivotValueSpec {
  column: string | null
  func: string
}

export interface PivotSpec {
  filters: FilterSpec[]
  rows: string[]
  columns: string[]
  values: { column: string; func: string }[]
  totals: boolean
}

export interface PivotResult extends FramePayload {
  row_fields: string[]
  column_field: string | null
  value_names: string[]
  column_keys: string[]
  filtered_rows: number
  grand_total: (string | number | boolean | null)[] | null
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
  kind: 'query' | 'analytics' | 'python'
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

export interface AssistantStatus {
  configured: boolean
  model: string
  base_url: string
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
