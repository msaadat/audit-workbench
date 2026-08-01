<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'

import { api, ApiError } from '../api'
import type { AgentMode } from '../composables/useAgentRun'
import type { EngagementBrief, EngagementPlan } from '../types'
import UiAdvancedSection from './ui/UiAdvancedSection.vue'

/**
 * Brief the agent instead of naming a folder.
 *
 * Start with the engagement name and add evidence. Planning context is then
 * assembled from the engagement material instead of requiring an auditor to
 * define an objective or scope before the workspace exists.
 *
 * The panel underneath states what the agent will do, where it will stop, where
 * requests go, and what past runs cost. Every line is read from the backend;
 * nothing here is illustrative.
 */

const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ created: [{ id: string; withImport: boolean }] }>()

const name = ref('')
const brief = ref<EngagementBrief>({ entity: '', period: '' })
const mode = ref<AgentMode>('auto')
const plan = ref<EngagementPlan | null>(null)
const busy = ref(false)
const error = ref('')

const modeOptions = [
  { label: 'Auto', value: 'auto' },
  { label: 'Ask first', value: 'permission' },
]
const canCreate = computed(() => Boolean(name.value.trim()) && !busy.value)

async function loadPlan() {
  try {
    plan.value = await api.get<EngagementPlan>(`/api/engagement/plan?mode=${mode.value}`)
  } catch {
    plan.value = null
  }
}

watch(visible, open => {
  if (!open) return
  name.value = ''
  brief.value = { entity: '', period: '' }
  error.value = ''
  void loadPlan()
})
watch(mode, () => { if (visible.value) void loadPlan() })

async function create(withImport: boolean) {
  if (!canCreate.value) return
  busy.value = true
  error.value = ''
  try {
    const workspace = await api.post<{ id: string }>('/api/workspaces', {
      name: name.value,
    })
    // Optional engagement details are only written when supplied.
    if (brief.value.entity?.trim() || brief.value.period?.trim()) {
      await api.post(`/api/workspaces/${workspace.id}/engagement/brief`, brief.value)
    }
    visible.value = false
    emit('created', { id: workspace.id, withImport })
  } catch (failure) {
    error.value = failure instanceof ApiError ? failure.message : String(failure)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    header="New engagement"
    :closable="!busy"
    :style="{ width: 'min(56rem, 96vw)' }"
    :contentStyle="{ maxHeight: '80vh', overflow: 'auto' }"
  >
    <div class="brief">
      <section class="fields">
        <label class="field">
          <span>Engagement name</span>
          <InputText v-model="name" placeholder="e.g. FY26 Procurement Audit" autofocus />
        </label>

        <UiAdvancedSection title="More detail" description="Optional — the agent infers what it can">
          <div class="more">
            <label class="field"><span>Entity</span><InputText v-model="brief.entity" placeholder="Global Bank" /></label>
            <label class="field"><span>Period</span><InputText v-model="brief.period" placeholder="1 Apr 2025 – 31 Mar 2026" /></label>
          </div>
        </UiAdvancedSection>

        <label class="field">
          <span>How should the agent work?</span>
          <SelectButton v-model="mode" :options="modeOptions" optionLabel="label" optionValue="value" :allowEmpty="false" size="small" />
        </label>

        <p v-if="error" class="error">{{ error }}</p>
      </section>

      <aside v-if="plan" class="proposal">
        <h4>Here's what I'd do</h4>

        <ol class="outcomes">
          <li v-for="outcome in plan.outcomes" :key="outcome.capability">{{ outcome.title }}</li>
        </ol>

        <p class="gate"><i class="pi pi-pause-circle" />{{ plan.gates.summary }}</p>

        <div class="facts">
          <p :class="{ warn: !plan.destination.configured }">
            <i :class="plan.destination.local ? 'pi pi-desktop' : 'pi pi-cloud'" />
            {{ plan.destination.summary }}
            <em v-if="plan.destination.model">{{ plan.destination.model }}</em>
          </p>

          <p v-if="plan.estimate.state === 'measured'">
            <i class="pi pi-clock" />
            About {{ plan.estimate.median_minutes }} minutes and
            {{ plan.estimate.median_model_calls }} model calls, from
            {{ plan.estimate.runs_observed }} {{ plan.estimate.basis }}.
            <em>{{ plan.estimate.caveat }}</em>
          </p>
          <p v-else>
            <i class="pi pi-clock" />
            {{ plan.estimate.reason }}
          </p>
        </div>
      </aside>
    </div>

    <template #footer>
      <div class="footer">
        <Button label="Cancel" severity="secondary" text :disabled="busy" @click="visible = false" />
        <span class="grow" />
        <Button label="Create only" severity="secondary" outlined size="small" :disabled="!canCreate" @click="create(false)" />
        <Button
          label="Create and add files"
          icon="pi pi-arrow-right"
          iconPos="right"
          size="small"
          :loading="busy"
          :disabled="!canCreate"
          @click="create(true)"
        />
      </div>
    </template>
  </Dialog>
</template>

<style scoped>
.brief { display: grid; grid-template-columns: minmax(0, 1fr) 19rem; gap: 1.4rem; align-items: start; }
.fields { display: grid; gap: 0.85rem; }
.field { display: grid; gap: 0.3rem; }
.field > span { color: #46576d; font-size: var(--aw-text-xs); font-weight: 700; }
.field small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.field :deep(.p-inputtext), .field :deep(.p-textarea) { width: 100%; }
.more { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem; }
.error { margin: 0; color: var(--aw-danger); font-size: var(--aw-text-sm); }

.proposal { padding: 0.9rem 1rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-md); background: var(--aw-canvas); }
.proposal h4 { margin: 0 0 0.6rem; font-size: var(--aw-text-sm); }
.outcomes { margin: 0; padding-left: 1.1rem; display: grid; gap: 0.15rem; }
.outcomes li { color: #46587a; font-size: var(--aw-text-xs); line-height: 1.4; }
.outcomes li::marker { color: var(--aw-teal); font-variant-numeric: tabular-nums; }

.gate { display: flex; align-items: flex-start; gap: 0.35rem; margin: 0.8rem 0 0; padding: 0.5rem 0.6rem; border-left: 3px solid var(--aw-warn); border-radius: 0 var(--aw-radius-sm) var(--aw-radius-sm) 0; background: var(--aw-warn-soft); color: #8a4308; font-size: var(--aw-text-xs); line-height: 1.45; }
.gate i { padding-top: 0.1rem; }

.facts { display: grid; gap: 0.5rem; margin-top: 0.8rem; padding-top: 0.7rem; border-top: 1px solid var(--aw-border); }
.facts p { display: flex; align-items: flex-start; gap: 0.35rem; margin: 0; color: #46587a; font-size: var(--aw-text-xs); line-height: 1.45; }
.facts p.warn { color: var(--aw-warn); }
.facts i { padding-top: 0.15rem; color: var(--aw-muted); }
.facts em { display: block; width: 100%; color: var(--aw-muted); font-size: 0.66rem; font-style: normal; }

.footer { display: flex; align-items: center; gap: 0.5rem; width: 100%; }
.grow { flex: 1; }

@media (max-width: 860px) {
  .brief { grid-template-columns: minmax(0, 1fr); }
  .more { grid-template-columns: 1fr; }
}
</style>
