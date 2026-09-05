<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'

import { api } from '../../api'
import type { CycleShape, CycleStep, TableInfo } from '../../types'
import UiDefinitionDrawer from '../ui/UiDefinitionDrawer.vue'

/**
 * The step list, edited as a list.
 *
 * The strip is a reading surface; this is where the shape is changed. One save
 * is one `PATCH /planning` of the whole `cycle` object, because the shape is
 * one artifact and the backend re-validates it as one — a step whose population
 * is no longer a loaded table is refused with every other problem it has, in
 * one message, rather than the drawer having to guess the rules.
 *
 * An auditor's edit *is* the confirmation. There is no separate confirmed
 * state and nothing downstream waits on one.
 */

const visible = defineModel<boolean>({ required: true })
const props = defineProps<{
  shape: CycleShape | null
  workspaceId: string
}>()
const emit = defineEmits<{
  save: [shape: CycleShape]
  error: [summary: string, error: unknown]
}>()

const draft = ref<CycleStep[]>([])
const name = ref('')
const crossCutting = ref('')
const documentTypes = ref<Array<{ id: string; label: string }>>([])
const tables = ref<string[]>([])

/** Where the anchor sits, as one choice rather than a flag on every row: a
 *  cycle has at most one, and a checkbox per population invites two. */
const anchorKey = ref('')

function keyOf(stepIndex: number, table: string): string {
  return `${stepIndex}:${table}`
}

watch(visible, async open => {
  if (!open) return
  name.value = props.shape?.name ?? ''
  crossCutting.value = props.shape?.cross_cutting?.name ?? ''
  draft.value = (props.shape?.steps ?? []).map(step => ({
    name: step.name,
    roles: step.roles.map(role => ({ ...role })),
    populations: step.populations.map(population => ({ ...population })),
    themes: [...step.themes],
  }))
  anchorKey.value = ''
  draft.value.forEach((step, index) => {
    for (const population of step.populations) {
      if (population.anchor) anchorKey.value = keyOf(index, population.table)
    }
  })
  try {
    const [types, workspace] = await Promise.all([
      api.get<{ types: Array<{ id: string; label: string }>; local_types: Array<{ id: string; label: string }> }>(
        `/api/workspaces/${props.workspaceId}/documents/types`,
      ),
      api.get<{ tables: TableInfo[] }>(`/api/workspaces/${props.workspaceId}`),
    ])
    // ``other`` is deliberately absent: a role may not name it, so offering it
    // would be offering a choice the save is then refused for.
    documentTypes.value = [...types.types, ...(types.local_types ?? [])]
      .filter(type => type.id !== 'other')
      .map(type => ({ id: type.id, label: `${type.label} · ${type.id}` }))
    tables.value = (workspace.tables ?? []).map(table => table.name)
  } catch (error) {
    emit('error', 'Could not load the cycle vocabulary', error)
  }
}, { immediate: true })

const tableOptions = computed(() => tables.value.map(table => ({ id: table, label: table })))

const anchorOptions = computed(() => [
  { id: '', label: 'No anchor population' },
  ...draft.value.flatMap((step, index) =>
    step.populations.map(population => ({
      id: keyOf(index, population.table),
      label: `${step.name || `Step ${index + 1}`} · ${population.table}`,
    })),
  ),
])

const ready = computed(
  () => Boolean(name.value.trim()) && draft.value.some(step => step.name.trim()),
)

function move(index: number, by: number) {
  const to = index + by
  if (to < 0 || to >= draft.value.length) return
  const steps = [...draft.value]
  const [moved] = steps.splice(index, 1)
  steps.splice(to, 0, moved)
  // The anchor is addressed by position, so re-derive it from the table it was
  // on rather than leaving it pointing at whichever step moved into that slot.
  const [wasIndex, wasTable] = anchorKey.value.split(':')
  draft.value = steps
  if (wasTable) {
    const moved = Number(wasIndex)
    const next = moved === index ? to : moved === to ? index : moved
    anchorKey.value = keyOf(next, wasTable)
  }
}

function addStep() {
  draft.value = [...draft.value, { name: '', roles: [], populations: [], themes: [] }]
}

function removeStep(index: number) {
  draft.value = draft.value.filter((_, position) => position !== index)
}

function addRole(index: number) {
  draft.value[index].roles.push({ name: '', document_type: '' })
}

function removeRole(stepIndex: number, roleIndex: number) {
  draft.value[stepIndex].roles.splice(roleIndex, 1)
}

function addPopulation(index: number) {
  draft.value[index].populations.push({ table: '' })
}

function removePopulation(stepIndex: number, populationIndex: number) {
  draft.value[stepIndex].populations.splice(populationIndex, 1)
}

function onSave() {
  const steps = draft.value.map((step, index) => ({
    ...step,
    populations: step.populations.map(population => {
      const { anchor: _ignored, ...rest } = population
      return keyOf(index, population.table) === anchorKey.value
        ? { ...rest, anchor: true }
        : rest
    }),
  }))
  emit('save', {
    ...(props.shape ?? {
      created_by: 'user',
      agent_run_id: null,
      apm_sha1: '',
      updated: null,
    }),
    name: name.value.trim(),
    steps,
    cross_cutting: crossCutting.value.trim()
      ? {
          name: crossCutting.value.trim(),
          themes: props.shape?.cross_cutting?.themes ?? [],
        }
      : null,
  } as CycleShape)
}
</script>

<template>
  <UiDefinitionDrawer
    v-model="visible"
    eyebrow="CYCLE"
    :title="name || 'Cycle design'"
    editing
    :ready="ready"
    consequence="The matrix takes its process names from these steps, and the cycle rules take their roles."
    @save="onSave"
  >
    <div class="steps-editor">
      <label class="field">
        <span class="field__label">Cycle name</span>
        <InputText v-model="name" placeholder="Procure-to-pay" />
      </label>

      <ol class="steps">
        <li v-for="(step, index) in draft" :key="index" class="step">
          <div class="step__head">
            <InputText v-model="step.name" placeholder="Step name" class="step__name" />
            <Button
              icon="pi pi-arrow-up" text size="small" aria-label="Move up"
              :disabled="index === 0" @click="move(index, -1)"
            />
            <Button
              icon="pi pi-arrow-down" text size="small" aria-label="Move down"
              :disabled="index === draft.length - 1" @click="move(index, 1)"
            />
            <Button
              icon="pi pi-trash" text size="small" severity="danger"
              aria-label="Remove step" @click="removeStep(index)"
            />
          </div>

          <div class="step__group">
            <span class="field__label">Documents that record it</span>
            <div v-for="(role, roleIndex) in step.roles" :key="roleIndex" class="row">
              <InputText v-model="role.name" placeholder="role name" class="row__id" />
              <Select
                v-model="role.document_type"
                :options="documentTypes"
                option-label="label"
                option-value="id"
                placeholder="document type"
                filter
                class="row__wide"
              />
              <Button
                icon="pi pi-times" text size="small" aria-label="Remove role"
                @click="removeRole(index, roleIndex)"
              />
            </div>
            <Button label="Add a document role" text size="small" icon="pi pi-plus" @click="addRole(index)" />
          </div>

          <div class="step__group">
            <span class="field__label">Population</span>
            <div v-for="(population, popIndex) in step.populations" :key="popIndex" class="row">
              <Select
                v-model="population.table"
                :options="tableOptions"
                option-label="label"
                option-value="id"
                placeholder="table"
                class="row__wide"
              />
              <Button
                icon="pi pi-times" text size="small" aria-label="Remove population"
                @click="removePopulation(index, popIndex)"
              />
            </div>
            <Button label="Add a population" text size="small" icon="pi pi-plus" @click="addPopulation(index)" />
          </div>
        </li>
      </ol>

      <Button label="Add a step" size="small" outlined icon="pi pi-plus" @click="addStep" />

      <label class="field">
        <span class="field__label">Anchor population</span>
        <Select
          v-model="anchorKey"
          :options="anchorOptions"
          option-label="label"
          option-value="id"
        />
        <small class="field__hint">Where a cycle test starts from. A cycle has at most one.</small>
      </label>

      <label class="field">
        <span class="field__label">Cross-cutting bucket</span>
        <InputText v-model="crossCutting" placeholder="Procurement operations" />
        <small class="field__hint">
          What runs across the cycle rather than within a step — override,
          monitoring, segregation.
        </small>
      </label>
    </div>
  </UiDefinitionDrawer>
</template>

<style scoped>
.steps-editor { display: flex; flex-direction: column; gap: 16px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field__label {
  font-size: var(--aw-text-xs);
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--aw-muted);
}
.field__hint { font-size: var(--aw-text-xs); color: var(--aw-muted); }
.steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.step {
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.step__head { display: flex; align-items: center; gap: 4px; }
.step__name { flex: 1; }
.step__group { display: flex; flex-direction: column; gap: 6px; }
.row { display: flex; align-items: center; gap: 6px; }
.row__id { width: 9rem; }
.row__wide { flex: 1; }
</style>
