<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../../api'
import type {
  AssistantAnswer,
  AssistantArtifact,
  AssistantStatus,
  RunPythonResult,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import ChartView from '../ChartView.vue'
import CodeEditor from '../CodeEditor.vue'
import PinDialog from '../PinDialog.vue'

interface Turn {
  question: string
  answer?: AssistantAnswer
  error?: string
  pending: boolean
}

// The AI-assisted creation surface: ask in plain English, the assistant loop
// returns artifacts. Code / library artifacts can be Saved as an analysis (into
// the rail) or Pinned straight to the dashboard.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ saved: [SavedAnalysis] }>()
const toast = useToast()

const status = ref<AssistantStatus | null>(null)
const question = ref('')
const turns = ref<Turn[]>([])
const busy = ref(false)
const showSettings = ref(false)
const savingSettings = ref(false)
const draftProvider = ref('')
const draftModel = ref('')
const showPin = ref(false)
const pinning = ref(false)
const pinTarget = ref<AssistantArtifact | null>(null)
const rerunning = ref<Record<string, boolean>>({})
const savingId = ref<string | null>(null)

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}

const SAMPLES = [
  'Flag invoices posted on a weekend',
  'Which customers have the highest total amount?',
  'Are there duplicate invoice numbers?',
  'Show the monthly trend of total amount',
]

const providerOptions = computed(() => status.value?.providers ?? [])
const activeProvider = computed(() => {
  const id = status.value?.provider ?? status.value?.backend
  return providerOptions.value.find((provider) => provider.id === id) ?? null
})
const draftProviderMeta = computed(() => (
  providerOptions.value.find((provider) => provider.id === draftProvider.value) ?? null
))

onMounted(loadStatus)

async function loadStatus() {
  try {
    status.value = await api.get<AssistantStatus>('/api/assistant/status')
  } catch {
    status.value = { configured: false, backend: 'groq', provider: 'groq', model: '', base_url: '', providers: [] }
  }
}

function openSettings() {
  draftProvider.value = status.value?.provider ?? status.value?.backend ?? 'groq'
  draftModel.value = status.value?.model ?? ''
  showSettings.value = true
}

watch(draftProvider, (provider, previous) => {
  if (!provider || provider === previous) return
  const meta = providerOptions.value.find((option) => option.id === provider)
  draftModel.value = meta?.default_model ?? ''
})

async function saveSettings() {
  savingSettings.value = true
  try {
    status.value = await api.patch<AssistantStatus>('/api/assistant/settings', {
      provider: draftProvider.value,
      model: draftModel.value,
    })
    showSettings.value = false
    toast.add({ severity: 'success', summary: 'Assistant settings saved', life: 2500 })
  } catch (error) {
    fail('Settings failed', error)
  } finally {
    savingSettings.value = false
  }
}

async function ask(text?: string) {
  const q = (text ?? question.value).trim()
  if (!q || busy.value) return
  question.value = ''
  const turn: Turn = { question: q, pending: true }
  turns.value.push(turn)
  busy.value = true
  try {
    turn.answer = await api.post<AssistantAnswer>(
      `/api/workspaces/${props.workspace.id}/assistant`,
      { question: q },
    )
  } catch (error) {
    turn.error = error instanceof ApiError ? error.message : String(error)
  } finally {
    turn.pending = false
    busy.value = false
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    ask()
  }
}

async function rerun(artifact: AssistantArtifact) {
  if (!artifact.code) return
  rerunning.value = { ...rerunning.value, [artifact.id]: true }
  try {
    const result = await api.post<RunPythonResult>(
      `/api/workspaces/${props.workspace.id}/run-python`,
      { code: artifact.code },
    )
    artifact.frame = result.frame
    artifact.total_rows = result.total_rows
    artifact.stdout = result.stdout
    artifact.spec = { code: artifact.code }
  } catch (error) {
    fail('Run failed', error)
  } finally {
    rerunning.value = { ...rerunning.value, [artifact.id]: false }
  }
}

// Only the two kinds the Analysis rail owns are saveable; query artifacts belong
// to the Query tab and can still be Pinned to the dashboard.
function saveable(artifact: AssistantArtifact): boolean {
  return artifact.kind === 'python' || artifact.kind === 'analytics'
}

async function saveAsAnalysis(artifact: AssistantArtifact) {
  savingId.value = artifact.id
  try {
    const body: Record<string, unknown> = {
      kind: artifact.kind,
      table: artifact.table,
      title: artifact.title,
      spec: artifact.kind === 'python' ? { code: artifact.code } : artifact.spec,
      viz: artifact.viz,
      source: 'ai',
    }
    const created = await api.post<SavedAnalysis>(
      `/api/workspaces/${props.workspace.id}/analyses`,
      body,
    )
    emit('saved', created)
    toast.add({ severity: 'success', summary: 'Saved to analyses', detail: artifact.title, life: 2500 })
  } catch (error) {
    fail('Save failed', error)
  } finally {
    savingId.value = null
  }
}

function openPin(artifact: AssistantArtifact) {
  pinTarget.value = artifact
  showPin.value = true
}

async function pin({ title, note }: { title: string; note: string }) {
  const artifact = pinTarget.value
  if (!artifact) return
  pinning.value = true
  try {
    const body: Record<string, unknown> = {
      kind: artifact.kind, title, note, spec: artifact.spec, viz: artifact.viz,
    }
    if (artifact.table) body.table = artifact.table
    await api.post(`/api/workspaces/${props.workspace.id}/tiles`, body)
    showPin.value = false
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', detail: title, life: 3000 })
  } catch (error) {
    fail('Pin failed', error)
  } finally {
    pinning.value = false
  }
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 7000 })
}

const toolLabel: Record<string, string> = {
  list_tables: 'Listed tables',
  describe_table: 'Profiled a table',
  query_table: 'Ran a query',
  run_analytics: 'Ran an analytics test',
  run_python: 'Ran Python',
}
</script>

<template>
  <div class="privacy-banner">
    <span><i class="pi pi-shield" /></span>
    <div><strong>Metadata-only AI analysis</strong><small>The model receives schemas and aggregate previews—not raw data rows. Generated Polars runs locally and remains editable.</small></div>
    <span class="grow" />
    <div v-if="status" class="provider-pill" :class="{ missing: !status.configured }">
      <i :class="status.configured ? 'pi pi-check-circle' : 'pi pi-key'" />
      <span>{{ activeProvider?.label ?? status.backend }}</span>
      <small>{{ status.model || 'No model selected' }}</small>
    </div>
    <Button icon="pi pi-cog" label="Settings" size="small" severity="secondary" outlined @click="openSettings" />
  </div>
  <div v-if="status && !status.configured" class="notice">
    <i class="pi pi-info-circle" />
    <div>
      <strong>The assistant isn't configured.</strong>
      <p>
        Add <code>{{ activeProvider?.api_key_env ?? 'GROQ_API_KEY' }}</code> to
        <code>.env</code>, then use Assistant settings for the provider and
        model. Only schema and aggregate statistics are ever sent to the model
        — never your raw data rows.
      </p>
    </div>
  </div>

  <div class="assistant">
    <div v-if="!turns.length" class="empty">
      <i class="pi pi-sparkles" />
      <p>Describe an analysis in plain English — the AI writes editable Polars you can run, save, and pin.</p>
      <div class="samples">
        <button
          v-for="sample in SAMPLES"
          :key="sample"
          class="sample"
          :disabled="!status?.configured"
          @click="ask(sample)"
        >
          {{ sample }}
        </button>
      </div>
    </div>

    <div v-for="(turn, index) in turns" :key="index" class="turn">
      <div class="bubble user"><i class="pi pi-user" /><span>{{ turn.question }}</span></div>

      <div v-if="turn.pending" class="bubble assistant pending">
        <i class="pi pi-spin pi-spinner" /> Thinking…
      </div>

      <div v-else-if="turn.error" class="bubble assistant error">
        <i class="pi pi-exclamation-triangle" /> {{ turn.error }}
      </div>

      <div v-else-if="turn.answer" class="bubble assistant">
        <div class="steps" v-if="turn.answer.steps.length">
          <span
            v-for="(step, si) in turn.answer.steps"
            :key="si"
            class="step"
            :class="{ bad: !step.ok }"
          >
            <i :class="step.ok ? 'pi pi-check' : 'pi pi-times'" />
            {{ toolLabel[step.tool] ?? step.tool }}
          </span>
        </div>

        <p class="answer">{{ turn.answer.answer }}</p>

        <div v-for="artifact in turn.answer.artifacts" :key="artifact.id" class="artifact">
          <div class="artifact-head">
            <strong>{{ artifact.title }}</strong>
            <Tag
              v-if="artifact.verdict"
              :value="artifact.verdict_text || artifact.verdict"
              :severity="verdictSeverity[artifact.verdict]"
            />
            <span class="grow" />
            <span class="muted rows" v-if="artifact.frame">
              {{ artifact.total_rows.toLocaleString() }} row{{ artifact.total_rows === 1 ? '' : 's' }}
            </span>
            <Button
              v-if="saveable(artifact)"
              label="Save"
              icon="pi pi-save"
              size="small"
              text
              :loading="savingId === artifact.id"
              @click="saveAsAnalysis(artifact)"
            />
            <Button label="Pin" icon="pi pi-thumbtack" severity="secondary" size="small" text @click="openPin(artifact)" />
          </div>

          <div v-if="artifact.stats?.length" class="stat-cards">
            <div v-for="stat in artifact.stats" :key="stat.label" class="stat-card">
              <div class="label">{{ stat.label }}</div>
              <div class="value">{{ stat.value }}</div>
            </div>
          </div>

          <div v-if="artifact.code !== undefined" class="code-block">
            <div class="code-head">
              <span><i class="pi pi-code" /> Python — editable</span>
              <Button
                label="Re-run"
                icon="pi pi-play"
                size="small"
                text
                :loading="rerunning[artifact.id]"
                @click="rerun(artifact)"
              />
            </div>
            <CodeEditor v-model="artifact.code" />
            <pre v-if="artifact.stdout" class="stdout">{{ artifact.stdout }}</pre>
          </div>

          <ChartView v-if="artifact.frame" :frame="artifact.frame" :viz="artifact.viz" height="300px" />
        </div>

        <p class="disclosure" v-tooltip.top="turn.answer.disclosure">
          <i class="pi pi-shield" /> Metadata-only — raw rows stayed on this machine.
        </p>
      </div>
    </div>
  </div>

  <div class="composer">
    <Textarea
      v-model="question"
      rows="2"
      autoResize
      :disabled="!status?.configured || busy"
      :placeholder="status?.configured
        ? 'Describe an analysis… (Enter to send, Shift+Enter for a new line)'
        : 'Configure the assistant key and provider settings to ask questions'"
      @keydown="onKeydown"
    />
    <Button
      icon="pi pi-send"
      label="Ask"
      :disabled="!status?.configured || !question.trim()"
      :loading="busy"
      @click="ask()"
    />
  </div>

  <PinDialog
    v-model:visible="showPin"
    :defaultTitle="pinTarget?.title ?? 'Result'"
    :saving="pinning"
    @pin="pin"
  />

  <Dialog v-model:visible="showSettings" modal header="Assistant settings" :style="{ width: '32rem' }">
    <div class="settings-form">
      <label>
        <span>Provider</span>
        <Select
          v-model="draftProvider"
          :options="providerOptions"
          optionLabel="label"
          optionValue="id"
          placeholder="Provider"
        />
      </label>
      <label>
        <span>Model</span>
        <InputText v-model="draftModel" placeholder="Model id" />
      </label>
      <div v-if="draftProviderMeta" class="settings-meta">
        <div><span>Endpoint</span><code>{{ draftProviderMeta.base_url }}</code></div>
        <div>
          <span>Key</span>
          <code>{{ draftProviderMeta.api_key_env }}</code>
          <Tag
            :value="draftProviderMeta.api_key_configured ? 'configured' : 'missing'"
            :severity="draftProviderMeta.api_key_configured ? 'success' : 'warn'"
          />
        </div>
      </div>
      <div v-if="draftProviderMeta?.models?.length" class="model-chips">
        <button
          v-for="model in draftProviderMeta.models"
          :key="model || '<blank>'"
          type="button"
          @click="draftModel = model"
        >
          {{ model || 'Loaded LM Studio model' }}
        </button>
      </div>
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="showSettings = false" />
      <Button label="Save" icon="pi pi-check" :loading="savingSettings" @click="saveSettings" />
    </template>
  </Dialog>
</template>

<style scoped>
.privacy-banner { display: flex; align-items: center; gap: .7rem; margin-bottom: .8rem; padding: .65rem .8rem; border: 1px solid #bfe5df; border-radius: 7px; background: #effaf8; }
.privacy-banner > span:not(.grow) { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: 5px; background: #d6f3ee; color: var(--aw-teal); }
.privacy-banner > div { display: flex; flex-direction: column; }
.privacy-banner strong { font-size: .8rem; color: #125e59; }
.privacy-banner small { margin-top: .1rem; color: #52716f; font-size: .7rem; }
.grow { flex: 1; }
.provider-pill { flex-direction: row !important; align-items: center; gap: .4rem; min-width: 0; color: #125e59; font-size: .8rem; }
.provider-pill small { max-width: 14rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.provider-pill.missing { color: #8a5a00; }
.notice {
  display: flex;
  gap: 0.75rem;
  background: var(--p-surface-50);
  border: 1px solid var(--p-surface-200);
  border-left: 3px solid var(--p-primary-400);
  border-radius: 8px;
  padding: 0.85rem 1rem;
  margin-bottom: 1rem;
}
.notice i { color: var(--p-primary-500); font-size: 1.1rem; margin-top: 0.15rem; }
.notice p { margin: 0.25rem 0 0; font-size: 0.85rem; color: var(--p-surface-600); }
.notice code { background: var(--p-surface-100); padding: 0 0.25rem; border-radius: 4px; }
.settings-form { display: grid; gap: 0.85rem; }
.settings-form label { display: grid; gap: 0.35rem; font-size: 0.82rem; font-weight: 700; color: var(--p-surface-700); }
.settings-meta { display: grid; gap: 0.45rem; padding: 0.75rem; border: 1px solid var(--p-surface-200); border-radius: 7px; background: var(--p-surface-50); }
.settings-meta > div { display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
.settings-meta span { min-width: 4.5rem; color: var(--p-surface-500); font-size: 0.78rem; }
.settings-meta code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; background: var(--p-surface-0); padding: 0.1rem 0.3rem; border-radius: 4px; }
.model-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.model-chips button { border: 1px solid var(--p-surface-200); background: var(--p-surface-0); border-radius: 999px; padding: 0.3rem 0.65rem; color: var(--p-primary-700); cursor: pointer; }
.model-chips button:hover { border-color: var(--p-primary-400); }

.assistant { min-height: 36vh; }
.empty { text-align: center; color: var(--p-surface-500); padding: 2.5rem 1rem; }
.empty i { font-size: 2rem; color: var(--p-primary-400); }
.empty p { margin: 0.6rem 0 1rem; }
.samples { display: flex; flex-wrap: wrap; gap: 0.5rem; justify-content: center; }
.sample {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 999px;
  padding: 0.4rem 0.9rem;
  font: inherit;
  font-size: 0.85rem;
  color: var(--p-primary-600);
  cursor: pointer;
}
.sample:hover:not(:disabled) { border-color: var(--p-primary-400); }
.sample:disabled { opacity: 0.5; cursor: not-allowed; }

.turn { margin-bottom: 1.25rem; }
.bubble { border-radius: 10px; padding: 0.75rem 1rem; margin-bottom: 0.5rem; }
.bubble.user {
  display: flex;
  gap: 0.55rem;
  align-items: baseline;
  background: var(--p-primary-50);
  color: var(--p-primary-800);
  font-weight: 500;
  max-width: 80%;
}
.bubble.assistant { background: var(--p-surface-0); border: 1px solid var(--p-surface-200); }
.bubble.pending, .bubble.error { color: var(--p-surface-500); }
.bubble.error { color: var(--p-red-500); }

.steps { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }
.step {
  font-size: 0.72rem;
  background: var(--p-surface-100);
  color: var(--p-surface-600);
  border-radius: 999px;
  padding: 0.15rem 0.55rem;
}
.step.bad { background: var(--p-red-50); color: var(--p-red-600); }
.answer { margin: 0; white-space: pre-wrap; line-height: 1.5; }

.artifact { border: 1px solid var(--p-surface-200); border-radius: 8px; padding: 0.75rem; margin-top: 0.85rem; }
.artifact-head { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; }
.artifact-head .grow { flex: 1; }
.rows { font-size: 0.8rem; }

.code-block { margin-bottom: 0.75rem; }
.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.78rem;
  color: var(--p-surface-500);
  margin-bottom: 0.25rem;
}
.stdout {
  background: var(--p-surface-900);
  color: var(--p-surface-0);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  font-size: 0.78rem;
  overflow-x: auto;
  margin: 0.4rem 0 0;
}

.stat-cards { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.65rem; }
.disclosure {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  margin: 0.75rem 0 0;
  font-size: 0.75rem;
  color: var(--p-surface-500);
  cursor: help;
}
.composer {
  display: flex;
  gap: 0.6rem;
  align-items: flex-end;
  position: sticky;
  bottom: -2px;
  padding: 0.75rem 0 0.4rem;
  background: var(--p-surface-0);
}
.composer :deep(textarea) { flex: 1; resize: none; }
</style>
