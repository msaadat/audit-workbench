<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import type { RuleSet, WorkspaceSummary } from '../../types'
import RulesetEditor from './RulesetEditor.vue'

// The Validation tab: a rail of saved rule sets on the left, the rule-set
// editor (field-wise checks grid + run results) on the right. A rule set is
// a saved spec bound to a table by name, so re-running it against refreshed
// data is just Run again.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const confirm = useConfirm()

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
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not load rule sets', detail, life: 6000 })
  } finally {
    loading.value = false
  }
}
watch(() => props.workspace.id, load, { immediate: true })

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

function confirmDelete(ruleset: RuleSet) {
  confirm.require({
    header: 'Delete rule set',
    message: `Delete "${ruleset.title}" and its ${ruleset.rules.length} rule(s)?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/rulesets/${ruleset.id}`)
        if (selectedId.value === ruleset.id) selectedId.value = null
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
  <div class="validation">
    <aside class="rail">
      <Button
        label="New rule set"
        icon="pi pi-plus"
        size="small"
        :outlined="!creating"
        @click="startNew"
      />

      <p class="rail-title">Saved rule sets</p>
      <p v-if="!loading && rulesets.length === 0" class="muted small">
        Nothing saved yet — define field-wise checks once, then re-run them on
        every data refresh.
      </p>

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
          <i class="pi pi-trash del" @click.stop="confirmDelete(r)" v-tooltip.bottom="'Delete'" />
        </div>
      </button>
    </aside>

    <section class="detail">
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
      <div v-else class="empty">
        <i class="pi pi-check-square" />
        <p>
          Pick a saved rule set, or start a <strong>New rule set</strong> —
          attach checks to each field of a table, save once, and re-run on
          every new or refreshed dataset.
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.validation {
  display: flex;
  gap: 1rem;
  align-items: stretch;
  /* Same viewport-filling split as the Analysis tab: rail and detail scroll
     independently instead of the whole page. */
  height: calc(100vh - 215px);
  min-height: 24rem;
}

.rail {
  flex: 0 0 15rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  overflow-y: auto;
  padding-right: 0.25rem;
}
.rail > * { flex-shrink: 0; }

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
  padding: 2px;
}

.empty {
  text-align: center;
  color: var(--p-surface-500);
  padding: 3rem 1rem;
}
.empty i { font-size: 2rem; color: var(--p-primary-400); }
.empty p { margin-top: 0.6rem; }
</style>
