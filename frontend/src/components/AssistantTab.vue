<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import type {
  AssistantAnswer,
  AssistantArtifact,
  AssistantStatus,
  RunPythonResult,
  WorkspaceSummary,
} from '../types'
import ChartView from './ChartView.vue'
import PinDialog from './PinDialog.vue'

interface Turn {
  question: string
  answer?: AssistantAnswer
  error?: string
  pending: boolean
}

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const status = ref<AssistantStatus | null>(null)
const question = ref('')
const turns = ref<Turn[]>([])
const busy = ref(false)
const showPin = ref(false)
const pinning = ref(false)
const pinTarget = ref<AssistantArtifact | null>(null)
const rerunning = ref<Record<string, boolean>>({})

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}

const SAMPLES = [
  'Which customers have the highest total amount?',
  'Run a Benford analysis on the amount column',
  'Are there duplicate invoice numbers?',
  'Show the monthly trend of total amount',
]

onMounted(async () => {
  try {
    status.value = await api.get<AssistantStatus>('/api/assistant/status')
  } catch {
    status.value = { configured: false, model: '', base_url: '' }
  }
})

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
  // Enter sends; Shift+Enter inserts a newline.
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
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Run failed', detail, life: 7000 })
  } finally {
    rerunning.value = { ...rerunning.value, [artifact.id]: false }
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
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Pin failed', detail, life: 6000 })
  } finally {
    pinning.value = false
  }
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
  <div v-if="status && !status.configured" class="notice">
    <i class="pi pi-info-circle" />
    <div>
      <strong>The assistant isn't configured.</strong>
      <p>
        Set a <code>GROQ_API_KEY</code> environment variable (optionally
        <code>GROQ_MODEL</code> / <code>GROQ_BASE_URL</code>) and restart the
        backend to enable natural-language analysis. Only schema and aggregate
        statistics are ever sent to the model — never your raw data rows.
      </p>
    </div>
  </div>

  <div class="assistant">
    <div v-if="!turns.length" class="empty">
      <i class="pi pi-comments" />
      <p>Ask a question about this workspace's data in plain English.</p>
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
              label="Pin"
              icon="pi pi-thumbtack"
              severity="secondary"
              size="small"
              text
              @click="openPin(artifact)"
            />
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
            <Textarea v-model="artifact.code" class="code" spellcheck="false" autoResize />
            <pre v-if="artifact.stdout" class="stdout">{{ artifact.stdout }}</pre>
          </div>

          <ChartView
            v-if="artifact.frame"
            :frame="artifact.frame"
            :viz="artifact.viz"
            height="300px"
          />
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
        ? 'Ask about this data… (Enter to send, Shift+Enter for a new line)'
        : 'Set GROQ_API_KEY to enable the assistant'"
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
</template>

<style scoped>
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

.notice i {
  color: var(--p-primary-500);
  font-size: 1.1rem;
  margin-top: 0.15rem;
}

.notice p {
  margin: 0.25rem 0 0;
  font-size: 0.85rem;
  color: var(--p-surface-600);
}

.notice code {
  background: var(--p-surface-100);
  padding: 0 0.25rem;
  border-radius: 4px;
}

.assistant {
  min-height: 40vh;
}

.empty {
  text-align: center;
  color: var(--p-surface-500);
  padding: 2.5rem 1rem;
}

.empty i {
  font-size: 2rem;
  color: var(--p-primary-400);
}

.empty p {
  margin: 0.6rem 0 1rem;
}

.samples {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  justify-content: center;
}

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

.sample:hover:not(:disabled) {
  border-color: var(--p-primary-400);
}

.sample:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.turn {
  margin-bottom: 1.25rem;
}

.bubble {
  border-radius: 10px;
  padding: 0.75rem 1rem;
  margin-bottom: 0.5rem;
}

.bubble.user {
  display: flex;
  gap: 0.55rem;
  align-items: baseline;
  background: var(--p-primary-50);
  color: var(--p-primary-800);
  font-weight: 500;
  max-width: 80%;
}

.bubble.assistant {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
}

.bubble.pending,
.bubble.error {
  color: var(--p-surface-500);
}

.bubble.error {
  color: var(--p-red-500);
}

.steps {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.6rem;
}

.step {
  font-size: 0.72rem;
  background: var(--p-surface-100);
  color: var(--p-surface-600);
  border-radius: 999px;
  padding: 0.15rem 0.55rem;
}

.step.bad {
  background: var(--p-red-50);
  color: var(--p-red-600);
}

.answer {
  margin: 0;
  white-space: pre-wrap;
  line-height: 1.5;
}

.artifact {
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.75rem;
  margin-top: 0.85rem;
}

.artifact-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.5rem;
}

.artifact-head .grow {
  flex: 1;
}

.rows {
  font-size: 0.8rem;
}

.code-block {
  margin-bottom: 0.75rem;
}

.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.78rem;
  color: var(--p-surface-500);
  margin-bottom: 0.25rem;
}

.code {
  width: 100%;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 0.82rem;
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

.stat-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.65rem;
}

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
  bottom: 0;
  padding-top: 0.75rem;
  background: var(--p-surface-50);
}

.composer :deep(textarea) {
  flex: 1;
  resize: none;
}
</style>
