<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../../api'
import { useSession } from '../../composables/useSession'
import type {
  CycleGraph, CycleShape, PlanningPayload, WorkspaceSummary,
} from '../../types'
import CycleRulesetReview from '../doc-tests/CycleRulesetReview.vue'
import CycleStrip from './CycleStrip.vue'
import CycleStepsEditor from './CycleStepsEditor.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import { plural } from '../../format'

/**
 * The cycle: the steps of the process, what records each one holds, and how
 * they relate.
 *
 * One artifact in two layers, and one page for both. The *shape* — steps,
 * document roles, populations — is authored after the memorandum from nothing
 * extracted, so this page has something to draw from the moment planning
 * finishes. The *bindings* — which fields join and which must agree — are
 * authored after the schemas by the cycle-rules stage, and fill in on the same
 * strip when they exist.
 *
 * A reading surface with two actions. Editing the shape is the step list;
 * editing a rule stays where rules are reviewed and approved.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const session = useSession()

const graph = ref<CycleGraph | null>(null)
const shape = ref<CycleShape | null>(null)
const loading = ref(true)
const editing = ref(false)
const reviewOpen = ref(false)
const showAllFields = ref(false)

const base = computed(() => `/api/workspaces/${props.workspace.id}`)
const approverId = computed(() => session.user.value?.email ?? '')

const hasCycle = computed(() => (graph.value?.steps.length ?? 0) > 0)

const title = computed(() => {
  const name = graph.value?.name?.trim()
  if (!name) return 'Cycle'
  return /cycle$/i.test(name) ? name : `${name} cycle`
})

/** What the header says the strip contains, counted from what is drawn. */
const counts = computed(() => {
  const steps = graph.value?.steps ?? []
  const types = new Set(
    steps.flatMap(step => step.documents.map(document => document.document_type)),
  )
  const populations = new Set(
    steps.flatMap(step => step.populations.map(population => population.table)),
  )
  return [
    plural(steps.length, 'step'),
    plural(types.size, 'document type'),
    plural(populations.size, 'population'),
  ].join(' · ')
})

/**
 * Whether the field half of the cycle exists yet, said plainly.
 *
 * The two layers arrive at different times and the page is readable between
 * them, so it has to say which state it is in rather than leave a reader
 * wondering why no arrows are drawn.
 */
const rulesStatus = computed(() => {
  const ruleset = graph.value?.ruleset
  if (!ruleset) return 'no cycle rules yet'
  if (ruleset.status === 'approved') return 'rules approved'
  if (ruleset.status === 'proposed') return 'rules proposed, awaiting approval'
  return `rules ${ruleset.status}`
})

const LEGEND = [
  { kind: 'join', label: 'links, identifier = identifier' },
  { kind: 'assert', label: 'must agree' },
  { kind: 'anchor', label: 'population row to its document' },
  { kind: 'table_join', label: 'table join' },
] as const

function fail(summary: string, error: unknown) {
  toast.add({
    severity: 'error', life: 6000, summary,
    detail: error instanceof ApiError ? error.message : String(error),
  })
}

async function load() {
  loading.value = true
  try {
    const [drawn, planning] = await Promise.all([
      api.get<CycleGraph>(`${base.value}/planning/cycle/graph`),
      api.get<PlanningPayload>(`${base.value}/planning`),
    ])
    graph.value = drawn
    shape.value = planning.planning.cycle
  } catch (error) {
    fail('Could not load the cycle', error)
  } finally {
    loading.value = false
  }
}

async function save(edited: CycleShape) {
  try {
    await api.patch(`${base.value}/planning`, { cycle: edited })
    editing.value = false
    await load()
    emit('changed')
  } catch (error) {
    fail('Could not save the cycle', error)
  }
}

async function onRulesApproved() {
  reviewOpen.value = false
  await load()
  emit('changed')
}

onMounted(load)
</script>

<template>
  <section class="cycle-tab">
    <header class="ui-page-header cycle-tab__header">
      <div class="ui-page-header__copy cycle-tab__copy">
        <h2>{{ title }}</h2>
        <span v-if="hasCycle" class="cycle-tab__counts" data-testid="cycle-counts">
          {{ counts }} · {{ rulesStatus }}
        </span>
      </div>
      <div class="ui-page-header__actions">
        <Button
          v-if="hasCycle"
          label="Edit steps"
          size="small"
          outlined
          severity="secondary"
          icon="pi pi-pencil"
          @click="editing = true"
        />
        <Button
          v-if="hasCycle"
          label="Review rules"
          size="small"
          icon="pi pi-list-check"
          @click="reviewOpen = true"
        />
      </div>
    </header>

    <UiEmptyState
      v-if="!loading && !hasCycle"
      icon="pi pi-sitemap"
      title="No cycle has been designed"
      description="The cycle is read from the audit planning memorandum's process flow. Ask the agent to design it, or add the steps by hand."
    >
      <Button label="Add the steps" size="small" @click="editing = true" />
    </UiEmptyState>

    <div v-else-if="hasCycle" class="cycle-tab__card">
      <div class="cycle-tab__scroller">
        <CycleStrip :graph="graph!" :show-all-fields="showAllFields" />
      </div>
      <footer class="cycle-tab__legend">
        <span
          v-for="entry in LEGEND"
          :key="entry.kind"
          :class="['cycle-legend', `cycle-legend--${entry.kind}`]"
        >{{ entry.label }}</span>
        <span class="cycle-tab__note">
          <template v-if="showAllFields">Every induced field is shown.</template>
          <template v-else>Only fields that take part in a relationship are shown.</template>
          <button type="button" class="cycle-tab__toggle" @click="showAllFields = !showAllFields">
            {{ showAllFields ? 'Show rule fields only' : 'Show all fields' }}
          </button>
        </span>
      </footer>
    </div>

    <CycleStepsEditor
      v-model="editing"
      :shape="shape"
      :workspace-id="props.workspace.id"
      @save="save"
      @error="(summary, error) => fail(summary, error)"
    />

    <CycleRulesetReview
      v-model="reviewOpen"
      :workspace-id="props.workspace.id"
      :approver-id="approverId"
      @approved="onRulesApproved"
      @error="(summary, error) => fail(summary, error)"
    />
  </section>
</template>

<style scoped>
.cycle-tab { display: flex; flex-direction: column; gap: var(--aw-space-3); min-height: 0; }
.cycle-tab__header { align-items: center; }
.cycle-tab__copy { display: flex; align-items: baseline; gap: var(--aw-space-3); flex-wrap: wrap; }
.cycle-tab__counts {
  color: var(--aw-muted);
  font-size: var(--aw-text-sm);
  font-variant-numeric: tabular-nums;
}

.cycle-tab__card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  overflow: hidden;
}
.cycle-tab__scroller {
  overflow-x: auto;
  overflow-y: hidden;
}
.cycle-tab__legend {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 0.5rem 1.25rem;
  border-top: 1px solid var(--aw-border);
  background: var(--aw-canvas);
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
}
.cycle-legend { display: inline-flex; align-items: center; gap: 0.375rem; white-space: nowrap; }
.cycle-legend::before {
  content: '';
  width: 1.25rem;
  height: 0;
  border-top-width: 2px;
  border-top-style: solid;
}
.cycle-legend--join::before { border-top-color: var(--aw-teal); }
.cycle-legend--assert::before { border-top-color: var(--aw-accent); border-top-style: dashed; }
.cycle-legend--anchor::before { border-top-color: var(--aw-ink-strong); border-top-width: 3px; }
.cycle-legend--table_join::before {
  border-top-color: color-mix(in srgb, var(--aw-muted) 70%, var(--aw-panel));
}
.cycle-tab__note {
  margin-left: auto;
  display: inline-flex;
  align-items: baseline;
  gap: 0.5rem;
  font-style: italic;
  white-space: nowrap;
}
.cycle-tab__toggle {
  border: 0;
  padding: 0;
  background: none;
  color: var(--aw-teal);
  font: inherit;
  font-style: normal;
  font-weight: 600;
  cursor: pointer;
}
.cycle-tab__toggle:hover { text-decoration: underline; }
</style>
