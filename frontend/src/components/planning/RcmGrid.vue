<script setup lang="ts">
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'

import type { DataTest, DocTest, FindingRollups, RcmRow } from '../../types'
import { workspaceQuery } from '../../composables/useWorkspaceNavigation'

const props = defineProps<{
  rows: RcmRow[]
  dataTests?: DataTest[]
  documentTests?: Array<Pick<DocTest, 'id' | 'title' | 'status'>>
  findingRollups?: FindingRollups
  generating?: boolean
  canGenerate?: boolean
}>()
const emit = defineEmits<{
  add: []
  remove: [string]
  update: [string, Partial<RcmRow>]
  open: [RcmRow]
  generate: [string[]]
}>()

const ratings = ['low', 'medium', 'high', 'critical']
const router = useRouter()
function openFinding(id: string) { void router.replace({ query: workspaceQuery('findings', { finding: id }) }) }
function testCount(row: RcmRow) { return row.execution_rollup.tests ?? row.test_refs.length }
function testTitles(row: RcmRow) { return (row.execution_rollup.test_rollups ?? []).map(item => item.title).join('; ') || 'Add a test' }
function exceptionsLabel(row: RcmRow) { return (row.execution_rollup.completed ?? 0) ? String(row.execution_rollup.exceptions ?? 0) : 'Not assessed' }
function statusSeverity(status?: string) { return status?.includes('exception') || status === 'blocked' ? 'danger' : status === 'completed_no_exception' ? 'success' : status === 'review_required' ? 'warn' : 'secondary' }
</script>

<template>
  <div class="rcm-grid">
    <div class="grid-head">
      <div><strong>Risk &amp; Control Matrix</strong><small>{{ rows.length }} rows</small></div>
      <Button label="Add risk" icon="pi pi-plus" size="small" outlined @click="emit('add')" />
    </div>
    <DataTable :value="rows" scrollable scrollHeight="60vh" size="small" stripedRows>
      <Column field="id" header="ID" frozen style="min-width: 7rem"><template #body="{ data }"><strong class="row-id">{{ data.id }}</strong></template></Column>
      <Column header="Process" style="min-width: 10rem"><template #body="{ data }"><Textarea v-model="data.process" rows="1" autoResize @change="emit('update', data.id, { process: data.process })" /></template></Column>
      <Column header="Risk" style="min-width: 18rem"><template #body="{ data }"><Textarea v-model="data.risk" rows="2" autoResize @change="emit('update', data.id, { risk: data.risk })" /></template></Column>
      <Column header="Rating" style="min-width: 8rem"><template #body="{ data }">
        <Select v-model="data.risk_rating" :options="ratings" @change="emit('update', data.id, { risk_rating: data.risk_rating })">
          <template #value="{ value }"><span v-if="value" class="rating"><span class="dot" :data-rating="value" />{{ value }}</span></template>
          <template #option="{ option }"><span class="rating"><span class="dot" :data-rating="option" />{{ option }}</span></template>
        </Select>
      </template></Column>
      <Column header="Control" style="min-width: 20rem"><template #body="{ data }"><Textarea v-model="data.control" rows="2" autoResize @change="emit('update', data.id, { control: data.control })" /></template></Column>
      <Column header="Test summary" style="min-width: 18rem"><template #body="{ data }"><button class="summary-link" @click="emit('open', data)"><strong>{{ testCount(data) }} test(s)</strong><span>{{ testTitles(data) }}</span></button></template></Column>
      <Column header="Execution status" style="min-width: 12rem"><template #body="{ data }"><div class="rollup"><Tag :value="testCount(data) ? `${data.execution_rollup.completed ?? 0}/${testCount(data)} complete` : 'not ready'" :severity="data.execution_rollup.blocked ? 'danger' : data.execution_rollup.review_required ? 'warn' : data.execution_rollup.completed === testCount(data) && testCount(data) ? 'success' : 'secondary'"/><small>{{ data.execution_rollup.passed ?? 0 }} passed · {{ data.execution_rollup.failed ?? 0 }} failed · {{ data.execution_rollup.blocked ?? 0 }} blocked</small></div></template></Column>
      <Column header="Exceptions" style="min-width: 8rem"><template #body="{ data }"><Tag :value="exceptionsLabel(data)" :severity="(data.execution_rollup.completed ?? 0) && (data.execution_rollup.exceptions ?? 0) ? 'danger' : 'secondary'"/></template></Column>
      <Column header="Conclusion" style="min-width: 10rem"><template #body="{ data }"><Tag :value="data.execution_rollup.control_conclusion ?? 'no conclusion'" :severity="statusSeverity(data.execution_rollup.control_conclusion)"/></template></Column>
      <Column header="Findings" style="min-width: 9rem"><template #body="{ data }"><span class="refs"><button v-for="finding in findingRollups?.by_rcm[data.id] ?? []" :key="finding.id" type="button" class="finding" @click="openFinding(finding.id)">{{ finding.id }} · {{ finding.severity }}</button><span v-if="!(findingRollups?.by_rcm[data.id]?.length)" class="muted">None</span></span></template></Column>
      <Column header="Review" style="min-width: 8rem"><template #body="{ data }"><Tag :value="data.review_status.replaceAll('_', ' ')" severity="secondary"/></template></Column>
      <Column frozen alignFrozen="right" style="min-width: 12rem"><template #body="{ data }"><div class="row-actions"><Button v-if="!testCount(data)" label="Generate test" icon="pi pi-sparkles" text size="small" :loading="props.generating" :disabled="!props.canGenerate" @click="emit('generate', [data.id])"/><Button icon="pi pi-eye" text size="small" @click="emit('open', data)"/><Button icon="pi pi-trash" text severity="danger" size="small" @click="emit('remove', data.id)" /></div></template></Column>
    </DataTable>
  </div>
</template>

<style scoped>
.rcm-grid { min-width: 0; }
.grid-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.75rem; }
.grid-head > div { display: flex; align-items: baseline; gap: 0.6rem; }
.grid-head small, .muted { color: var(--aw-muted); }
.refs { display:flex; flex-wrap:wrap; gap:.25rem }.refs button { border:1px solid var(--aw-border); border-radius:999px; background:var(--p-primary-50); color:var(--aw-teal); font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); padding:.2rem .45rem; cursor:pointer }
.refs button.exception { background:var(--p-red-50); color:var(--p-red-700) }.refs button.complete { background:var(--p-green-50); color:var(--p-green-700) }
.refs button.finding { background:var(--p-orange-50); color:var(--p-orange-800) }
.summary-link { display:flex; flex-direction:column; gap:.2rem; width:100%; padding:.25rem; border:0; background:transparent; text-align:left; color:inherit; cursor:pointer }.summary-link span { color:var(--aw-muted); font-size:var(--aw-text-xs); line-height:1.35 }.summary-link:hover strong { color:var(--aw-teal) }.rollup { display:flex; flex-direction:column; align-items:flex-start; gap:.3rem }.rollup small { color:var(--aw-muted) }.row-actions { display:flex }

/* Prose grid, not a ledger: sans face at one size everywhere except the ID. */
.rcm-grid :deep(.p-datatable-tbody > tr > td) { font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); vertical-align: top; padding: .45rem .55rem; }
.row-id { font-family: var(--aw-font-mono); font-size: 0.78rem; letter-spacing: -0.01em; }
:deep(.p-datatable-thead > tr > th) { background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; text-transform: uppercase; letter-spacing: .06em; }

/* Ghost controls: read as table text, reveal editability on hover/focus. */
:deep(.p-inputtext), :deep(.p-textarea), :deep(.p-select) { width: 100%; font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); line-height: 1.45; }
:deep(td .p-inputtext), :deep(td .p-textarea), :deep(td .p-select) { background: transparent; border-color: transparent; box-shadow: none; transition: border-color .12s, background .12s; }
:deep(td .p-inputtext), :deep(td .p-textarea) { padding: .3rem .45rem; }
:deep(td .p-select .p-select-label) { padding: .3rem .45rem; }
:deep(td .p-select .p-select-dropdown) { width: 1.75rem; color: transparent; }
:deep(.p-datatable-tbody > tr:hover td .p-inputtext),
:deep(.p-datatable-tbody > tr:hover td .p-textarea),
:deep(.p-datatable-tbody > tr:hover td .p-select) { border-color: var(--aw-border); }
:deep(.p-datatable-tbody > tr:hover td .p-select .p-select-dropdown) { color: var(--aw-muted); }
:deep(.p-datatable-tbody > tr td .p-inputtext:focus),
:deep(.p-datatable-tbody > tr td .p-textarea:focus),
:deep(.p-datatable-tbody > tr td .p-select.p-focus) { background: #fff; border-color: var(--aw-border-strong); }

/* Rating severity dot */
.rating { display: inline-flex; align-items: center; gap: .4rem; font-size: var(--aw-text-sm); text-transform: capitalize; }
.dot { width: .5rem; height: .5rem; border-radius: 50%; background: var(--aw-muted); flex: none; }
.dot[data-rating='low'] { background: #facc15; }
.dot[data-rating='medium'] { background: #f59e0b; }
.dot[data-rating='high'] { background: #dc2626; }
.dot[data-rating='critical'] { background: #7f1d1d; }
</style>
