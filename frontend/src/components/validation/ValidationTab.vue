<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type { RuleSet, WorkspaceSummary } from '../../types'
import RulesetEditor from './RulesetEditor.vue'
import UiMasterDetail from '../ui/UiMasterDetail.vue'

// The Validation tab: a rail of saved rule sets on the left, the rule-set
// editor (field-wise checks grid + run results) on the right. A rule set is
// a saved spec bound to a table by name, so re-running it against refreshed
// data is just Run again.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const rulesets = ref<RuleSet[]>([])
const selectedId = ref<string | null>(null)
const creating = ref(false)
const loading = ref(false)
// Verdict of the most recent run this session, per rule set (runs are
// stateless — nothing is persisted, so the rail only knows what it saw).
const lastVerdicts = ref<Record<string, string>>({})

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}

const selected = computed(() => rulesets.value.find((r) => r.id === selectedId.value) ?? null)

async function load() {
  loading.value = true
  try {
    rulesets.value = (
      await api.get<{ rulesets: RuleSet[] }>(`/api/workspaces/${props.workspace.id}/rulesets`)
    ).rulesets
    if (!creating.value && (!selectedId.value || !rulesets.value.some(item => item.id === selectedId.value))) {
      selectedId.value = rulesets.value[0]?.id ?? null
    }
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not load rule sets', detail, life: 6000 })
  } finally {
    loading.value = false
  }
}
watch(() => props.workspace.id, load, { immediate: true })

// Live-refresh the rail while an agent run creates or updates rule sets.
const unsubscribe = useAgentRun(props.workspace.id).onWorkspaceChanged((change) => {
  if (change.kind === 'ruleset') void onChanged()
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
  <UiMasterDetail railWidth="17rem" class="validation">
    <template #rail><div class="rail">
      <div class="rail-heading"><p class="eyebrow">Control testing</p><strong>Validation rule sets</strong></div>
      <Button
        label="New rule set"
        icon="pi pi-plus"
        size="small"
        :outlined="!creating"
        @click="startNew"
      />

      <p class="rail-title">Engagement rules</p>

      <button
        v-for="r in rulesets"
        :key="r.id"
        class="rail-item"
        :class="{ active: selectedId === r.id }"
        @click="select(r)"
      >
        <div class="rail-item-head">
          <span class="rail-item-title">{{ r.title }}</span>
          <Tag
            v-if="lastVerdicts[r.id]"
            :value="lastVerdicts[r.id]"
            :severity="verdictSeverity[lastVerdicts[r.id]] ?? 'info'"
          />
        </div>
        <div class="rail-item-meta">
          <i class="pi pi-table" />
          {{ r.table }} · {{ r.rules.length }} rule{{ r.rules.length === 1 ? '' : 's' }}
        </div>
      </button>
    </div></template>

    <section class="detail surface-panel">
      <RulesetEditor
        v-if="creating || selected"
        :key="selected?.id ?? 'new-ruleset'"
        :workspace="workspace"
        :ruleset="creating ? null : selected"
        @saved="onSaved"
        @changed="onChanged"
        @deleted="onDeleted"
        @ran="onRan"
      />
      <div v-else class="empty-state validation-empty">
        <div><span class="empty-state-icon"><i class="pi pi-check-square" /></span><h3>Define reusable controls</h3><p>Select or create a rule set.</p></div>
      </div>
    </section>
  </UiMasterDetail>
</template>

<style scoped>
.validation {
  /* Same viewport-filling split as the Analysis tab: rail and detail scroll
     independently instead of the whole page. */
  min-height: 32rem;
}

.rail {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  overflow: visible;
  padding: 0.75rem;
  box-shadow: none;
}
.rail > * { flex-shrink: 0; }
.rail-heading { padding: .15rem .1rem .5rem; border-bottom: 1px solid var(--aw-border); }
.rail-heading .eyebrow { margin-bottom: .1rem; }

.rail-title {
  margin: 0.5rem 0 0.15rem;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--aw-muted);
}

.small { font-size: 0.85rem; }

.rail-item {
  text-align: left;
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-sm);
  box-shadow: var(--aw-shadow-sm);
  padding: 0.65rem 0.8rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.rail-item:hover { border-color: var(--aw-teal); box-shadow: var(--aw-shadow-md); }
.rail-item.active { border-color: var(--aw-teal); box-shadow: inset 3px 0 0 var(--aw-teal); background: var(--aw-teal-soft); }

.rail-item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.4rem;
}
.rail-item-title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.3;
  font-size: 0.92rem;
  font-weight: 600;
}
.rail-item-meta {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.3rem;
  font-size: 0.75rem;
  color: var(--aw-muted);
}
.rail-item-meta .del {
  margin-left: auto;
  cursor: pointer;
  padding: 0.2rem;
  border-radius: 4px;
}
.rail-item-meta .del:hover { color: var(--aw-danger); }

.detail {
  min-width: 0;
  overflow: visible;
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
.validation-empty { height: 100%; border: 0; background: transparent; }

@media (max-width: 900px) {
  .validation { min-height: 0; }
  .rail { flex-basis: auto; max-height: 18rem; }
  .detail { min-height: 24rem; }
}
</style>
