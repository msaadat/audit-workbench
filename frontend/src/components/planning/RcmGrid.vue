<script setup lang="ts">
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'

import type { DataTest, DocTest, FindingRollups, RcmRow } from '../../types'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import { plural } from '../../format'

const props = defineProps<{
  rows: RcmRow[]
  dataTests?: DataTest[]
  documentTests?: Array<Pick<DocTest, 'id' | 'title' | 'status' | 'rcm_id' | 'rcm_refs'>>
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
const nav = useWorkspaceNav()
function openFinding(id: string) { void nav.replace('findings', { finding: id }) }
function testCount(row: RcmRow) { return row.execution_rollup.tests ?? row.test_refs.length }
function testTitles(row: RcmRow) { return (row.execution_rollup.test_rollups ?? []).map(item => item.title).join('; ') || 'Add a test' }
function exceptionsLabel(row: RcmRow) { return (row.execution_rollup.completed ?? 0) ? String(row.execution_rollup.exceptions ?? 0) : 'Not assessed' }
function attributeSummary(row: RcmRow) { return row.control_attributes.map(item => `${item.assertion} · ${item.requirement}`).join('; ') }
function statusSeverity(status?: string) { return status?.includes('exception') || status === 'blocked' ? 'danger' : status === 'completed_no_exception' ? 'success' : status === 'review_required' ? 'warn' : 'secondary' }
</script>

<template>
  <div class="rcm-grid">
    <div class="grid-head">
      <div><strong>Risk &amp; Control Matrix</strong><small>{{ rows.length }} rows</small></div>
      <Button label="Add risk" icon="pi pi-plus" size="small" outlined @click="emit('add')" />
    </div>
    <DataTable :value="rows" scrollable scrollHeight="60vh" size="small" stripedRows>
      <!-- Process rides under the id rather than taking a column of its own:
           it is a short label, and the width it was costing is width the risk
           and control statements need. -->
      <Column field="id" header="Risk" frozen style="min-width: 10rem"><template #body="{ data }">
        <div class="row-identity">
          <strong class="row-id">{{ data.id }}</strong>
          <Textarea v-model="data.process" rows="1" class="row-process" @change="emit('update', data.id, { process: data.process })" />
        </div>
      </template></Column>
      <Column header="Risk statement" style="min-width: 18rem"><template #body="{ data }"><Textarea v-model="data.risk" rows="2" @change="emit('update', data.id, { risk: data.risk })" /></template></Column>
      <Column header="Rating" style="min-width: 8rem"><template #body="{ data }">
        <Select v-model="data.risk_rating" :options="ratings" @change="emit('update', data.id, { risk_rating: data.risk_rating })">
          <template #value="{ value }"><span v-if="value" class="rating"><span class="dot" :data-rating="value" />{{ value }}</span></template>
          <template #option="{ option }"><span class="rating"><span class="dot" :data-rating="option" />{{ option }}</span></template>
        </Select>
      </template></Column>
      <Column header="Control" style="min-width: 20rem"><template #body="{ data }"><Textarea v-model="data.control" rows="2" @change="emit('update', data.id, { control: data.control })" /></template></Column>
      <Column header="Control attributes" style="min-width: 18rem"><template #body="{ data }"><button class="summary-link" @click="emit('open', data)"><strong>{{ plural(data.control_attributes.length, 'attribute') }}</strong><span>{{ attributeSummary(data) }}</span></button></template></Column>
      <Column header="Test summary" style="min-width: 18rem"><template #body="{ data }"><button class="summary-link" @click="emit('open', data)"><strong>{{ plural(testCount(data), 'test') }}</strong><span>{{ testTitles(data) }}</span></button></template></Column>
      <Column header="Execution status" style="min-width: 14rem"><template #body="{ data }"><div class="rollup"><Tag :value="testCount(data) ? `${data.execution_rollup.completed ?? 0}/${testCount(data)} complete` : 'not ready'" :severity="data.execution_rollup.blocked ? 'danger' : data.execution_rollup.review_required ? 'warn' : data.execution_rollup.completed === testCount(data) && testCount(data) ? 'success' : 'secondary'"/><small>{{ plural(data.execution_rollup.tested_items ?? 0, 'item') }} tested · {{ data.execution_rollup.failed_items ?? 0 }} failed · {{ plural(data.execution_rollup.assertion_mismatches ?? 0, 'assertion mismatch', 'assertion mismatches') }}</small></div></template></Column>
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
.refs { display:flex; flex-wrap:wrap; gap:.25rem }.refs button { border:1px solid var(--aw-border); border-radius:var(--aw-radius-pill); background:var(--aw-teal-soft); color:var(--aw-teal); font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); padding:.2rem .45rem; cursor:pointer }
.refs button.exception { background:var(--aw-danger-soft); color:var(--aw-danger) }.refs button.complete { background:var(--aw-ok-soft); color:var(--aw-ok) }
.refs button.finding { background:var(--aw-warn-soft); color:var(--aw-warn-ink) }
.summary-link { display:flex; flex-direction:column; gap:.2rem; width:100%; padding:.25rem; border:0; background:transparent; text-align:left; color:inherit; cursor:pointer }
/* Clamped for the same reason the editable cells are bounded: one row with six
   control attributes must not set the height of every row in the table. The
   whole list is behind the click this button already performs. */
.summary-link span { display:-webkit-box; overflow:hidden; color:var(--aw-muted); font-size:var(--aw-text-xs); line-height:1.35; -webkit-box-orient:vertical; -webkit-line-clamp:2 }
.summary-link:hover strong { color:var(--aw-teal) }
.rollup { display:flex; flex-direction:column; align-items:flex-start; gap:.3rem }
.rollup small { display:-webkit-box; overflow:hidden; color:var(--aw-muted); -webkit-box-orient:vertical; -webkit-line-clamp:2 }
.row-actions { display:flex }

/* Prose grid, not a ledger: sans face at one size everywhere except the ID. */
.rcm-grid :deep(.p-datatable-tbody > tr > td) { font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); vertical-align: top; padding: .45rem .55rem; }
.row-identity { display: flex; flex-direction: column; gap: .1rem; }
.row-id { font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); letter-spacing: -0.01em; }
.row-identity :deep(.row-process) { color: var(--aw-muted); font-size: var(--aw-text-xs); }

/* `autoResize` sized every row to its longest cell, so rows ran 130–190px and
   three and a half of nineteen fitted on screen. A fixed two-row box scrolls
   its own overflow, keeps the cell editable, and makes row height uniform.
   The full text is one click away in the RCM detail dialog. */
:deep(td .p-textarea) { resize: vertical; overflow-y: auto; }

/* The right-hand action column floats over the cells that scroll beneath it, so
   it needs an opaque ground and an edge — without them the Control text ran
   under the buttons and simply stopped mid-word. */
:deep(.p-datatable-tbody > tr > td.p-datatable-frozen-column),
:deep(.p-datatable-thead > tr > th.p-datatable-frozen-column) { background: var(--aw-panel); }
:deep(.p-datatable-tbody > tr:nth-child(even) > td.p-datatable-frozen-column) { background: var(--aw-canvas); }
/* Both frozen columns carry the same class, so the edge is picked by position:
   the first casts its shadow rightwards, the last leftwards, which is what
   tells the reader the middle columns scroll under them. */
:deep(.p-datatable-frozen-column:first-child) { box-shadow: 6px 0 6px -6px rgb(13 35 64 / 18%); }
:deep(.p-datatable-frozen-column:last-child) { box-shadow: -6px 0 6px -6px rgb(13 35 64 / 18%); }
:deep(.p-datatable-thead > tr > th) { background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }

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
:deep(.p-datatable-tbody > tr td .p-select.p-focus) { background: var(--aw-panel); border-color: var(--aw-border-strong); }

/* Rating severity dot */
.rating { display: inline-flex; align-items: center; gap: .4rem; font-size: var(--aw-text-sm); text-transform: capitalize; }
.dot { width: .5rem; height: .5rem; border-radius: 50%; background: var(--aw-muted); flex: none; }
.dot[data-rating='low'] { background: var(--aw-low); }
.dot[data-rating='medium'] { background: var(--aw-warn); }
.dot[data-rating='high'] { background: var(--aw-danger); }
.dot[data-rating='critical'] { background: var(--aw-danger-ink); }
</style>
