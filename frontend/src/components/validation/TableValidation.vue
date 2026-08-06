<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type { RuleSet, WorkspaceSummary } from '../../types'
import RulesetEditor from './RulesetEditor.vue'

// Validation rule sets scoped to one table, embedded as a Data-tab sub-view.
// Same rule-set model and editor the workspace-level Validation tab used —
// just filtered down to the table currently selected there.
const props = defineProps<{ workspace: WorkspaceSummary; table: string }>()
const toast = useToast()

const rulesets = ref<RuleSet[]>([])
const selectedId = ref<string | null>(null)
const creating = ref(false)
const loading = ref(false)
// Verdict of the most recent run this session, per rule set (runs are
// stateless — nothing is persisted, so the bar only knows what it saw).
const lastVerdicts = ref<Record<string, string>>({})

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}

const selected = computed(() => rulesets.value.find((r) => r.id === selectedId.value) ?? null)

async function load() {
  loading.value = true
  try {
    const all = (
      await api.get<{ rulesets: RuleSet[] }>(`/api/workspaces/${props.workspace.id}/rulesets`)
    ).rulesets
    rulesets.value = all.filter((r) => r.table === props.table)
    if (!creating.value && (!selectedId.value || !rulesets.value.some((r) => r.id === selectedId.value))) {
      selectedId.value = rulesets.value[0]?.id ?? null
      creating.value = rulesets.value.length === 0
    }
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not load rule sets', detail, life: 6000 })
  } finally {
    loading.value = false
  }
}
void load()

// Live-refresh after durable agent commits, including revision-only commits.
const unsubscribe = useAgentRun(props.workspace.id).onWorkspaceInvalidated(() => {
  void onChanged()
})
onUnmounted(unsubscribe)

function startNew() {
  creating.value = true
  selectedId.value = null
}
function select(ruleset: RuleSet) {
  selectedId.value = ruleset.id
  creating.value = false
}
async function onSaved(created: RuleSet) {
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
  creating.value = false
  await load()
}
function onRan(rulesetId: string | null, verdict: string) {
  if (rulesetId) lastVerdicts.value = { ...lastVerdicts.value, [rulesetId]: verdict }
}
</script>

<template>
  <div class="table-validation">
    <div v-if="rulesets.length || creating" class="ruleset-bar">
      <button
        v-for="r in rulesets"
        :key="r.id"
        class="ruleset-chip"
        :class="{ active: !creating && selectedId === r.id }"
        @click="select(r)"
      >
        <span>{{ r.title }}</span>
        <small>{{ r.rules.length }} rule{{ r.rules.length === 1 ? '' : 's' }}</small>
        <Tag
          v-if="lastVerdicts[r.id]"
          :value="lastVerdicts[r.id]"
          :severity="verdictSeverity[lastVerdicts[r.id]] ?? 'info'"
        />
      </button>
      <Button label="New rule set" icon="pi pi-plus" size="small" outlined :disabled="creating" @click="startNew" />
    </div>

    <RulesetEditor
      v-if="creating || selected"
      :key="selected?.id ?? 'new-ruleset'"
      :workspace="workspace"
      :ruleset="creating ? null : selected"
      :defaultTable="table"
      @saved="onSaved"
      @changed="onChanged"
      @deleted="onDeleted"
      @ran="onRan"
    />
    <div v-else class="empty-state validation-empty">
      <div>
        <span class="empty-state-icon"><i class="pi pi-check-square" /></span>
        <h3>No validation rules yet</h3>
        <p>Create a rule set for this table.</p>
        <Button label="New rule set" icon="pi pi-plus" size="small" @click="startNew" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.table-validation { display: flex; flex-direction: column; gap: 0.7rem; min-width: 0; }

.ruleset-bar { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }

.ruleset-chip {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.7rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-panel);
  font: inherit;
  font-size: var(--aw-text-sm);
  color: inherit;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.ruleset-chip:hover { border-color: var(--aw-teal); }
.ruleset-chip.active { border-color: var(--aw-teal); background: var(--aw-teal-soft); color: var(--aw-teal); }
.ruleset-chip small { color: var(--aw-muted); }
.ruleset-chip.active small { color: inherit; }

.validation-empty { height: 100%; min-height: 16rem; border: 0; background: transparent; }
.validation-empty .p-button { margin-top: 0.6rem; }
</style>
