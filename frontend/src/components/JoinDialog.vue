<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'

import { api, ApiError } from '../api'
import type { ColumnSchema, WorkspaceSummary } from '../types'
import UiAdvancedSection from './ui/UiAdvancedSection.vue'

const props = defineProps<{ workspace: WorkspaceSummary; visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; saved: [] }>()

const toast = useToast()

const name = ref('')
const left = ref<string | null>(null)
const right = ref<string | null>(null)
const how = ref('left')
const pairs = ref<{ left_on: string | null; right_on: string | null }[]>([
  { left_on: null, right_on: null },
])
const saving = ref(false)

const howOptions = [
  { label: 'Left join (keep all left rows)', value: 'left' },
  { label: 'Inner join (matching rows only)', value: 'inner' },
  { label: 'Full join (all rows from both)', value: 'full' },
  { label: 'Semi join (left rows with a match)', value: 'semi' },
  { label: 'Anti join (left rows without a match)', value: 'anti' },
]

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))
const leftColumns = ref<string[]>([])
const rightColumns = ref<string[]>([])

async function loadColumns(table: string | null, target: 'left' | 'right') {
  const columns = table
    ? (
        await api.get<{ columns: ColumnSchema[] }>(
          `/api/workspaces/${props.workspace.id}/tables/${table}/schema`,
        )
      ).columns.map((c) => c.name)
    : []
  if (target === 'left') leftColumns.value = columns
  else rightColumns.value = columns
}

watch(left, (table) => loadColumns(table, 'left'))
watch(right, (table) => loadColumns(table, 'right'))
watch([left, right], ([leftTable, rightTable]) => {
  if (!name.value.trim() && leftTable && rightTable) name.value = `${leftTable}_${rightTable}`
})
watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      name.value = ''
      left.value = null
      right.value = null
      how.value = 'left'
      pairs.value = [{ left_on: null, right_on: null }]
    }
  },
)

const valid = computed(
  () =>
    name.value.trim() &&
    left.value &&
    right.value &&
    pairs.value.length > 0 &&
    pairs.value.every((p) => p.left_on && p.right_on),
)

async function save() {
  saving.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/joins`, {
      name: name.value,
      left: left.value,
      right: right.value,
      how: how.value,
      left_on: pairs.value.map((p) => p.left_on),
      right_on: pairs.value.map((p) => p.right_on),
    })
    emit('saved')
    emit('update:visible', false)
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Join failed', detail, life: 6000 })
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    header="Add join"
    :style="{ width: '36rem' }"
    @update:visible="emit('update:visible', $event)"
  >
    <div class="field">
      <label>Joined table name</label>
      <InputText v-model="name" placeholder="e.g. transactions_with_customers" />
    </div>

    <div class="row">
      <div class="field grow">
        <label>Left table</label>
        <Select v-model="left" :options="tableOptions" placeholder="Pick a table" />
      </div>
      <div class="field grow">
        <label>Right table</label>
        <Select v-model="right" :options="tableOptions" placeholder="Pick a table" />
      </div>
    </div>

    <div class="field">
      <label>Join keys</label>
      <div v-for="(pair, index) in pairs" :key="index" class="row pair-row">
        <Select v-model="pair.left_on" :options="leftColumns" placeholder="Left column" class="grow" filter />
        <i class="pi pi-arrows-h muted" />
        <Select v-model="pair.right_on" :options="rightColumns" placeholder="Right column" class="grow" filter />
        <Button icon="pi pi-times" text severity="danger" :disabled="pairs.length === 1" @click="pairs.splice(index, 1)" />
      </div>
      <Button label="Add key pair" icon="pi pi-plus" text size="small" @click="pairs.push({ left_on: null, right_on: null })" />
    </div>

    <UiAdvancedSection title="Advanced join type" description="Left join keeps every row from the left table">
      <div class="field advanced-field"><label>Join type</label><Select v-model="how" :options="howOptions" optionLabel="label" optionValue="value" /></div>
    </UiAdvancedSection>

    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="emit('update:visible', false)" />
      <Button label="Create join" icon="pi pi-link" :disabled="!valid" :loading="saving" @click="save" />
    </template>
  </Dialog>
</template>

<style scoped>
.field {
  margin-bottom: 0.9rem;
}

.row {
  display: flex;
  gap: 0.6rem;
  align-items: center;
}

.grow {
  flex: 1;
  min-width: 0;
}

.pair-row {
  margin-bottom: 0.5rem;
}
.advanced-field { margin: 0; }
</style>
