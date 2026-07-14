<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'
import Skeleton from 'primevue/skeleton'

import { api, ApiError } from '../api'
import type { WorkspaceListItem, WorkspaceSummary } from '../types'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const workspaces = ref<WorkspaceListItem[]>([])
const loading = ref(true)
const showCreate = ref(false)
const creating = ref(false)
const createStep = ref(1)
const name = ref('')
const description = ref('')
const documentAi = ref<boolean | null>(null)
const importChoice = ref<'import' | 'skip' | null>(null)

const wizardSteps = ['Engagement', 'Document AI', 'Import']

async function load() {
  loading.value = true
  try {
    workspaces.value = await api.get<WorkspaceListItem[]>('/api/workspaces')
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Failed to load workspaces', detail: String(error), life: 5000 })
  } finally {
    loading.value = false
  }
}

async function create() {
  creating.value = true
  try {
    const ws = await api.post<WorkspaceSummary>('/api/workspaces', {
      name: name.value,
      description: description.value,
      doc_llm_optin: documentAi.value === true,
    })
    showCreate.value = false
    await router.push({
      path: `/workspace/${ws.id}`,
      query: importChoice.value === 'import' ? { import: '1' } : {},
    })
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not create workspace', detail, life: 5000 })
  } finally {
    creating.value = false
  }
}

function openCreate() {
  createStep.value = 1
  name.value = ''
  description.value = ''
  documentAi.value = null
  importChoice.value = null
  showCreate.value = true
}

function nextStep() {
  if (createStep.value === 1 && name.value.trim()) createStep.value = 2
  else if (createStep.value === 2 && documentAi.value !== null) createStep.value = 3
}

function remove(ws: WorkspaceListItem) {
  confirm.require({
    header: 'Delete workspace',
    message: `Delete "${ws.name}" and all its data files? This cannot be undone.`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      await api.del(`/api/workspaces/${ws.id}`)
      await load()
    },
  })
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="home-hero">
      <div>
        <p class="eyebrow">Engagement index</p>
        <h1>Your audit workspaces</h1>
        <p class="muted">
          Load, validate and analyse engagement data without moving raw records
          off this device.
        </p>
      </div>
      <Button label="New workspace" icon="pi pi-plus" @click="openCreate" />
    </div>

    <div v-if="loading" class="loading-grid">
      <Skeleton v-for="n in 3" :key="n" height="12rem" borderRadius="8px" />
    </div>
    <div v-else-if="workspaces.length === 0" class="empty-state">
      <div>
        <span class="empty-state-icon"><i class="pi pi-folder-open" /></span>
        <h3>Start your first engagement</h3>
        <p>Create a workspace, then add CSV, TSV or Excel files. Profiling and audit tests run entirely on this machine.</p>
        <Button label="Create workspace" icon="pi pi-plus" @click="openCreate" />
      </div>
    </div>

    <div class="grid">
      <Card v-for="ws in workspaces" :key="ws.id" class="ws-card" @click="router.push(`/workspace/${ws.id}`)">
        <template #title>
          <div class="card-title">
            <span class="workspace-icon"><i class="pi pi-briefcase" /></span>
            <span class="workspace-name">{{ ws.name }}</span>
            <i class="pi pi-arrow-up-right card-arrow" />
          </div>
        </template>
        <template #content>
          <p class="desc">{{ ws.description || 'No description.' }}</p>
          <div class="workspace-meta">
            <span><i class="pi pi-database" /> {{ ws.table_count }} table{{ ws.table_count === 1 ? '' : 's' }}</span>
            <span><i class="pi pi-calendar" /> Created {{ ws.created || '—' }}</span>
          </div>
        </template>
        <template #footer>
          <div class="card-actions">
            <Tag :value="ws.table_count ? 'Ready' : 'Setup needed'" :severity="ws.table_count ? 'success' : 'warn'" />
            <Button icon="pi pi-trash" severity="danger" text size="small" v-tooltip.bottom="'Delete workspace'" @click.stop="remove(ws)" />
          </div>
        </template>
      </Card>
    </div>

    <Dialog v-model:visible="showCreate" header="New workspace" modal :closable="!creating" :style="{ width: 'min(42rem, 94vw)' }">
      <div class="wizard-steps" aria-label="Workspace setup progress">
        <span v-for="(label, index) in wizardSteps" :key="label" :class="{ active: createStep === index + 1, done: createStep > index + 1 }">
          <b>{{ index + 1 }}</b>{{ label }}
        </span>
      </div>

      <section v-if="createStep === 1" class="wizard-panel">
        <div class="wizard-heading">
          <p class="eyebrow">Engagement details</p>
          <h2>Name this workspace</h2>
          <p>Use one workspace per audit engagement. You can add and replace source material later.</p>
        </div>
        <div class="field">
          <label for="ws-name">Name</label>
          <InputText id="ws-name" v-model="name" placeholder="e.g. FY26 Revenue Audit" autofocus @keyup.enter="nextStep" />
        </div>
        <div class="field">
          <label for="ws-desc">Description (optional)</label>
          <Textarea id="ws-desc" v-model="description" rows="3" placeholder="Scope, period, or a short engagement note" />
        </div>
      </section>

      <section v-else-if="createStep === 2" class="wizard-panel">
        <div class="wizard-heading">
          <p class="eyebrow">Privacy choice</p>
          <h2>Allow document AI?</h2>
          <p>Structured data rows always remain local. This setting controls whether confirmed document pages may be sent to your configured AI provider.</p>
        </div>
        <div class="choice-grid single-column">
          <button type="button" class="choice-card" :class="{ selected: documentAi === false }" :aria-pressed="documentAi === false" @click="documentAi = false">
            <i class="pi pi-lock" />
            <span><strong>Keep document AI off</strong><small>Document content stays on this device. You can opt in later from Documents.</small></span>
            <i class="pi pi-check-circle choice-check" />
          </button>
          <button type="button" class="choice-card" :class="{ selected: documentAi === true }" :aria-pressed="documentAi === true" @click="documentAi = true">
            <i class="pi pi-cloud-upload" />
            <span><strong>Enable document AI</strong><small>Only pages you confirm may be disclosed, with purpose and model activity logged for the engagement.</small></span>
            <i class="pi pi-check-circle choice-check" />
          </button>
        </div>
        <p class="privacy-note"><i class="pi pi-shield" /> This choice does not allow raw spreadsheet rows to leave the machine.</p>
      </section>

      <section v-else class="wizard-panel">
        <div class="wizard-heading">
          <p class="eyebrow">Source material</p>
          <h2>Bring in files now?</h2>
          <p>Select an audit folder or individual files after the workspace is created. Mixed data and document files will be staged and classified before import.</p>
        </div>
        <div class="choice-grid">
          <button type="button" class="choice-card vertical" :class="{ selected: importChoice === 'import' }" :aria-pressed="importChoice === 'import'" @click="importChoice = 'import'">
            <i class="pi pi-folder-open" />
            <span><strong>Import now</strong><small>Choose a folder or one or more files next.</small></span>
            <i class="pi pi-check-circle choice-check" />
          </button>
          <button type="button" class="choice-card vertical" :class="{ selected: importChoice === 'skip' }" :aria-pressed="importChoice === 'skip'" @click="importChoice = 'skip'">
            <i class="pi pi-forward" />
            <span><strong>Skip for now</strong><small>Open an empty workspace and import at any time.</small></span>
            <i class="pi pi-check-circle choice-check" />
          </button>
        </div>
      </section>

      <template #footer>
        <div class="wizard-footer">
          <Button v-if="createStep === 1" label="Cancel" severity="secondary" text :disabled="creating" @click="showCreate = false" />
          <Button v-else label="Back" icon="pi pi-arrow-left" severity="secondary" text :disabled="creating" @click="createStep -= 1" />
          <Button v-if="createStep < 3" label="Continue" icon="pi pi-arrow-right" iconPos="right" :disabled="createStep === 1 ? !name.trim() : documentAi === null" @click="nextStep" />
          <Button v-else :label="importChoice === 'import' ? 'Create and import' : 'Create workspace'" icon="pi pi-check" :loading="creating" :disabled="importChoice === null" @click="create" />
        </div>
      </template>
    </Dialog>
  </div>
</template>

<style scoped>
.home-hero {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin: 0.5rem 0 1.5rem;
  padding: 1.6rem 1.75rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-lg);
  background: linear-gradient(118deg, #fff 0%, var(--aw-teal-soft) 130%);
  box-shadow: var(--aw-shadow-sm);
}

h1 {
  margin: 0 0 0.4rem;
  font-size: var(--aw-text-2xl);
}

.home-hero p {
  margin: 0;
  max-width: 48rem;
}

.portfolio-strip { display: flex; align-items: stretch; margin-bottom: 1.2rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-md); background: #fff; box-shadow: var(--aw-shadow-sm); overflow: hidden; }
.portfolio-strip > span { display: flex; flex-direction: column; min-width: 10rem; padding: 0.8rem 1.2rem; border-right: 1px solid var(--aw-border); }
.portfolio-strip strong { font-size: 1.2rem; font-weight: 600; color: var(--aw-ink); }
.portfolio-strip small { margin-top: 0.15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.portfolio-strip .privacy-fact { margin-left: auto; flex-direction: row; align-items: center; gap: 0.6rem; border-right: 0; }
.privacy-fact > i { color: var(--aw-teal); }
.privacy-fact > span { display: flex; flex-direction: column; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 0.9rem;
}

.loading-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
:deep(.ws-card) { border: 1px solid var(--aw-border); border-radius: var(--aw-radius-md); box-shadow: var(--aw-shadow-sm); cursor: pointer; transition: transform .15s, border-color .15s, box-shadow .15s; }
:deep(.ws-card:hover) { transform: translateY(-2px); border-color: var(--aw-teal); box-shadow: var(--aw-shadow-md); }
:deep(.ws-card .p-card-body) { padding: 1.05rem 1.1rem 0.85rem; }
:deep(.ws-card .p-card-content) { padding-block: 0.7rem; }
:deep(.ws-card .p-card-footer) { padding-top: 0.55rem; border-top: 1px solid #edf1f5; }

.card-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}
.workspace-icon { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: 6px; background: var(--aw-teal-soft); color: var(--aw-teal); font-size: 0.9rem; }
.workspace-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 1rem; }
.card-arrow { color: #94a3b8; font-size: 0.85rem; }

.desc {
  margin: 0;
  min-height: 2.5rem;
  color: var(--p-surface-600);
}

.workspace-meta { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; color: var(--aw-muted); font-size: 0.74rem; }
.workspace-meta span { display: flex; align-items: center; gap: 0.3rem; }

.card-actions {
  display: flex;
  justify-content: space-between;
}

.empty {
  padding: 2rem 0;
}

.wizard-steps {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.5rem;
  margin-bottom: 1.35rem;
}

.wizard-steps span {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.55rem;
  border-bottom: 2px solid var(--p-surface-200);
  color: var(--p-surface-500);
  font-size: var(--aw-text-xs);
}

.wizard-steps span.active { color: var(--aw-teal); border-color: var(--aw-teal); }
.wizard-steps span.done { color: var(--p-green-600); border-color: var(--p-green-400); }
.wizard-steps b { display: grid; place-items: center; width: 1.35rem; height: 1.35rem; border: 1px solid currentColor; border-radius: 50%; }

.wizard-panel { display: grid; gap: 1rem; min-height: 19rem; align-content: start; }
.wizard-heading h2 { margin: 0.15rem 0 0.4rem; font-size: var(--aw-text-xl); }
.wizard-heading p { margin: 0; color: var(--aw-muted); line-height: 1.5; }
.wizard-heading .eyebrow { color: var(--aw-teal); }
.wizard-panel .field { display: grid; gap: 0.35rem; }
.wizard-panel .field label { color: #46576d; font-size: var(--aw-text-xs); font-weight: 700; }

.choice-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.choice-grid.single-column { grid-template-columns: 1fr; }
.choice-card {
  position: relative;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 0.8rem;
  width: 100%;
  padding: 1rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-md);
  background: #fff;
  color: var(--aw-ink);
  text-align: left;
  cursor: pointer;
  transition: border-color .15s, background .15s, box-shadow .15s;
}
.choice-card:hover { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.choice-card.selected { border-color: var(--aw-teal); background: var(--aw-teal-soft); box-shadow: 0 0 0 1px var(--aw-teal); }
.choice-card > i:first-child { color: var(--aw-teal); font-size: 1.35rem; }
.choice-card span { display: grid; gap: 0.25rem; }
.choice-card small { color: var(--aw-muted); line-height: 1.4; }
.choice-card .choice-check { color: transparent; }
.choice-card.selected .choice-check { color: var(--aw-teal); }
.choice-card.vertical { min-height: 9rem; grid-template-columns: 1fr auto; align-content: center; }
.choice-card.vertical > i:first-child { grid-row: 1; font-size: 1.6rem; }
.choice-card.vertical span { grid-column: 1 / -1; }
.choice-card.vertical .choice-check { position: absolute; top: 0.75rem; right: 0.75rem; }
.privacy-note { display: flex; align-items: center; gap: 0.5rem; margin: 0; padding: 0.7rem 0.8rem; border-radius: var(--aw-radius-sm); background: var(--p-surface-50); color: var(--aw-muted); font-size: var(--aw-text-xs); }
.privacy-note i { color: var(--aw-teal); }
.wizard-footer { display: flex; justify-content: space-between; width: 100%; }

@media (max-width: 720px) {
  .home-hero { flex-direction: column; }
  .portfolio-strip { overflow-x: auto; }
  .loading-grid { grid-template-columns: 1fr; }
  .choice-grid { grid-template-columns: 1fr; }
}
</style>
