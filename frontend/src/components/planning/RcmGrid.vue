<script setup lang="ts">
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type { DocTest, FindingRollups, RcmRow } from '../../types'

const props = defineProps<{ rows: RcmRow[]; documentTests?: DocTest[]; findingRollups?: FindingRollups }>()
const emit = defineEmits<{
  add: []
  remove: [string]
  update: [string, Partial<RcmRow>]
}>()

const ratings = ['low', 'medium', 'high', 'critical']
const router = useRouter()
function openRef(ref: string) {
  if (ref.startsWith('doctest:')) void router.replace({ query: { ...router.currentRoute.value.query, tab: 'doc-tests', test: ref.slice(8) } })
}
function openFinding(id: string) { void router.replace({ query: { ...router.currentRoute.value.query, tab: 'findings', finding: id } }) }
function test(ref: string) { return props.documentTests?.find(item => `doctest:${item.id}` === ref) }
function refLabel(ref: string) {
  const item = test(ref)
  if (!item) return ref
  const exceptions = item.state_counts?.exception ?? 0
  return `${item.title} · ${exceptions ? `${exceptions} exception(s)` : item.status.replace('_', ' ')}`
}
function refClass(ref: string) {
  const item = test(ref)
  return item && (item.state_counts?.exception ?? 0) > 0 ? 'exception' : item?.status === 'completed' ? 'complete' : ''
}
</script>

<template>
  <div class="rcm-grid">
    <div class="grid-head">
      <div><strong>Risk &amp; Control Matrix</strong><small>{{ rows.length }} rows</small></div>
      <Button label="Add risk" icon="pi pi-plus" size="small" outlined @click="emit('add')" />
    </div>
    <DataTable :value="rows" scrollable scrollHeight="60vh" size="small" stripedRows>
      <Column field="id" header="ID" frozen style="min-width: 8rem"><template #body="{ data }"><strong>{{ data.id }}</strong></template></Column>
      <Column header="Process" style="min-width: 11rem"><template #body="{ data }"><InputText v-model="data.process" @change="emit('update', data.id, { process: data.process })" /></template></Column>
      <Column header="Risk" style="min-width: 18rem"><template #body="{ data }"><Textarea v-model="data.risk" rows="2" autoResize @change="emit('update', data.id, { risk: data.risk })" /></template></Column>
      <Column header="Rating" style="min-width: 9rem"><template #body="{ data }"><Select v-model="data.risk_rating" :options="ratings" @change="emit('update', data.id, { risk_rating: data.risk_rating })" /></template></Column>
      <Column header="Assertion" style="min-width: 11rem"><template #body="{ data }"><InputText v-model="data.assertion" @change="emit('update', data.id, { assertion: data.assertion })" /></template></Column>
      <Column header="Control" style="min-width: 20rem"><template #body="{ data }"><Textarea v-model="data.control" rows="2" autoResize @change="emit('update', data.id, { control: data.control })" /></template></Column>
      <Column header="Type" style="min-width: 10rem"><template #body="{ data }"><InputText v-model="data.control_type" @change="emit('update', data.id, { control_type: data.control_type })" /></template></Column>
      <Column header="Planned test" style="min-width: 22rem"><template #body="{ data }"><Textarea v-model="data.test_procedure" rows="2" autoResize @change="emit('update', data.id, { test_procedure: data.test_procedure })" /></template></Column>
      <Column header="Results & findings" style="min-width: 17rem"><template #body="{ data }"><span class="refs"><button v-for="ref in data.test_refs" :key="ref" type="button" :class="refClass(ref)" @click="openRef(ref)">{{ refLabel(ref) }}</button><button v-for="finding in findingRollups?.by_rcm[data.id] ?? []" :key="finding.id" type="button" class="finding" @click="openFinding(finding.id)">{{ finding.id }} · {{ finding.severity }}</button><span v-if="!data.test_refs.length && !(findingRollups?.by_rcm[data.id]?.length)" class="muted">None yet</span></span></template></Column>
      <Column frozen alignFrozen="right" style="width: 3.5rem"><template #body="{ data }"><Button icon="pi pi-trash" text severity="danger" size="small" @click="emit('remove', data.id)" /></template></Column>
    </DataTable>
  </div>
</template>

<style scoped>
.rcm-grid { min-width: 0; }
.grid-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.75rem; }
.grid-head > div { display: flex; align-items: baseline; gap: 0.6rem; }
.grid-head small, .muted { color: var(--aw-muted); }
.refs { display:flex; flex-wrap:wrap; gap:.25rem }.refs button { border:1px solid var(--aw-border); border-radius:999px; background:var(--p-primary-50); color:var(--aw-teal); font-size:.7rem; padding:.2rem .45rem; cursor:pointer }
.refs button.exception { background:var(--p-red-50); color:var(--p-red-700) }.refs button.complete { background:var(--p-green-50); color:var(--p-green-700) }
.refs button.finding { background:var(--p-orange-50); color:var(--p-orange-800) }
:deep(.p-inputtext), :deep(.p-textarea), :deep(.p-select) { width: 100%; font-size: 0.78rem; }
:deep(.p-datatable-thead > tr > th) { background: var(--aw-raised); }
</style>
