<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { SavedAnalysis, WorkspaceSummary } from '../types'
import AnalysisLibrary from './analysis/AnalysisLibrary.vue'
import AnalysisPython from './analysis/AnalysisPython.vue'
import AnalysisCode from './analysis/AnalysisCode.vue'

// The Analysis tab: a rail of saved analyses on the left, an editor on the
// right. Two creation paths — the predefined Library or Code (hand-written
// Polars). AI-assisted analysis lives in the Agent drawer, which saves into
// this same rail. Saving persists to the rail; Pinning (inside each pane)
// promotes a copy to the dashboard.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const confirm = useConfirm()

const analyses = ref<SavedAnalysis[]>([])
const selectedId = ref<string | null>(null)
const creating = ref<'library' | 'code' | null>(null)
const loading = ref(false)

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}
const sourceIcon: Record<string, string> = {
  library: 'pi pi-book', ai: 'pi pi-sparkles', code: 'pi pi-code',
}
const sourceLabel: Record<string, string> = {
  library: 'library', ai: 'AI code', code: 'custom code',
}

const selected = computed(() => analyses.value.find((a) => a.id === selectedId.value) ?? null)

async function load() {
  loading.value = true
  try {
    analyses.value = (
      await api.get<{ analyses: SavedAnalysis[] }>(`/api/workspaces/${props.workspace.id}/analyses`)
    ).analyses
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not load analyses', detail, life: 6000 })
  } finally {
    loading.value = false
  }
}
watch(() => props.workspace.id, load, { immediate: true })

// Live-refresh the rail while an agent run saves analyses into it.
const unsubscribe = useAgentRun(props.workspace.id).onWorkspaceChanged((change) => {
  if (change.kind === 'analysis') void onChanged()
})
onUnmounted(unsubscribe)

function startLibrary() {
  creating.value = 'library'
  selectedId.value = null
}
function startCode() {
  creating.value = 'code'
  selectedId.value = null
}
function select(a: SavedAnalysis) {
  selectedId.value = a.id
  creating.value = null
}

async function onSaved(created: SavedAnalysis) {
  await load()
  select(created)
}
async function onChanged() {
  const keep = selectedId.value
  await load()
  selectedId.value = keep
}
async function onDeleted() {
  selectedId.value = null
  creating.value = null
  await load()
}

function confirmDelete(a: SavedAnalysis) {
  confirm.require({
    header: 'Delete analysis',
    message: `Delete "${a.title}"? Pinned dashboard copies are unaffected.`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/analyses/${a.id}`)
        if (selectedId.value === a.id) selectedId.value = null
        await load()
      } catch (error) {
        const detail = error instanceof ApiError ? error.message : String(error)
        toast.add({ severity: 'error', summary: 'Delete failed', detail, life: 6000 })
      }
    },
  })
}
</script>

<template>
  <div class="analysis">
    <aside class="rail surface-panel">
      <div class="rail-heading"><p class="eyebrow">Analysis library</p><strong>Saved procedures</strong></div>
      <div class="rail-actions">
        <Button label="Library" icon="pi pi-book" size="small" :outlined="creating !== 'library'" @click="startLibrary" />
        <Button label="Code" icon="pi pi-code" size="small" :outlined="creating !== 'code'" @click="startCode" />
      </div>

      <p class="rail-title">Engagement analyses</p>
      <p v-if="!loading && analyses.length === 0" class="muted small">
        Nothing saved yet — create one from the library, your own code, or the
        Agent panel.
      </p>

      <button
        v-for="a in analyses"
        :key="a.id"
        class="rail-item"
        :class="{ active: selectedId === a.id }"
        @click="select(a)"
      >
        <div class="rail-item-head">
          <span class="rail-item-title">{{ a.title }}</span>
          <Tag
            v-if="a.error"
            value="error"
            severity="danger"
          />
          <Tag
            v-else-if="a.verdict"
            :value="a.verdict"
            :severity="verdictSeverity[a.verdict]"
          />
        </div>
        <div class="rail-item-meta">
          <i :class="sourceIcon[a.source] ?? 'pi pi-book'" />
          {{ sourceLabel[a.source] ?? a.source }}
          <span v-if="a.table"> · {{ a.table }}</span>
          <i class="pi pi-trash del" @click.stop="confirmDelete(a)" v-tooltip.bottom="'Delete'" />
        </div>
      </button>
    </aside>

    <section class="detail surface-panel">
      <AnalysisLibrary
        v-if="creating === 'library' || selected?.kind === 'analytics'"
        :key="selected?.id ?? 'new-library'"
        :workspace="workspace"
        :analysis="creating === 'library' ? null : selected"
        @saved="onSaved"
        @changed="onChanged"
        @deleted="onDeleted"
      />
      <AnalysisCode
        v-if="creating === 'code'"
        :key="workspace.id"
        :workspace="workspace"
        @saved="onSaved"
      />
      <AnalysisPython
        v-if="!creating && selected?.kind === 'python'"
        :key="selected.id"
        :workspace="workspace"
        :analysis="selected"
        @changed="onChanged"
        @deleted="onDeleted"
      />
      <div v-if="!creating && !selected" class="empty-state analysis-empty">
        <div><span class="empty-state-icon"><i class="pi pi-shield" /></span>
        <h3>Choose an analysis path</h3>
        <p>Use a predefined audit procedure, write visible Polars code, or ask the Agent panel to analyze for you.</p></div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.analysis {
  display: flex;
  gap: 1rem;
  align-items: stretch;
  /* Fill the viewport below the app banner / workspace header / tab strip so
     the rail and the detail pane scroll independently instead of the whole
     page scrolling as one. */
  height: calc(100vh - 215px);
  min-height: 24rem;
}

.rail {
  flex: 0 0 15rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  overflow-y: auto;
  padding: 0.75rem;
  box-shadow: none;
}
.rail > * { flex-shrink: 0; }

.rail-actions {
  display: flex;
  gap: 0.5rem;
}
.rail-heading { padding: .15rem .1rem .5rem; border-bottom: 1px solid var(--aw-border); }
.rail-heading .eyebrow { margin-bottom: .1rem; }
/* Three creation buttons share the 15rem rail — trim padding so the labels
   stay on one line. */
.rail-actions :deep(.p-button) { flex: 1; padding-inline: 0.35rem; white-space: nowrap; }

.rail-title {
  margin: 0.5rem 0 0.15rem;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--p-surface-500);
}

.small { font-size: 0.85rem; }

.rail-item {
  text-align: left;
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.55rem 0.7rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.rail-item:hover { border-color: var(--p-primary-300); }
.rail-item.active { border-color: var(--p-primary-500); box-shadow: 0 0 0 1px var(--p-primary-500); }
.rail-item.active { border-color: #6ca9a3; box-shadow: inset 3px 0 0 var(--aw-teal); background: #f3fbfa; }

.rail-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.4rem;
}
.rail-item-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.9rem;
  font-weight: 500;
}
.rail-item-meta {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.2rem;
  font-size: 0.72rem;
  color: var(--p-surface-500);
}
.rail-item-meta .del {
  margin-left: auto;
  cursor: pointer;
  padding: 0.1rem;
}
.rail-item-meta .del:hover { color: var(--p-red-500); }

.detail {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  /* Room for the focus ring of the sticky header inputs. */
  padding: 1rem;
  box-shadow: none;
}

.empty {
  text-align: center;
  color: var(--p-surface-500);
  padding: 3rem 1rem;
}
.empty i { font-size: 2rem; color: var(--p-primary-400); }
.empty p { margin-top: 0.6rem; }
.analysis-empty { height: 100%; border: 0; background: transparent; }

@media (max-width: 900px) {
  .analysis { height: auto; flex-direction: column; }
  .rail { flex-basis: auto; max-height: 18rem; }
  .detail { min-height: 24rem; }
}
</style>
