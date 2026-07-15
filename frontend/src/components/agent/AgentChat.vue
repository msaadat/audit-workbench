<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../../api'
import { useAssistantContext } from '../../composables/useAssistantContext'
import type {
  AgentMessage,
  AssistantAnswer,
  AssistantArtifact,
  RunPythonResult,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import ChartView from '../ChartView.vue'
import CodeEditor from '../CodeEditor.vue'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import PinDialog from '../PinDialog.vue'
import DocumentContextPicker from './DocumentContextPicker.vue'

interface Turn {
  question: string
  answer?: AssistantAnswer
  error?: string
  pending: boolean
}

// The drawer's conversation surface (successor of the standalone Ask AI tab):
// Chat is the durable command queue: it launches a command when no run exists,
// queues follow-up work while one runs, and starts a linked run after terminal.
const props = defineProps<{
  workspace: WorkspaceSummary
  messages: AgentMessage[]
  runActive: boolean
  runFinished: boolean
  configured: boolean
  pendingQuestion: string | null
}>()
const emit = defineEmits<{ steer: [string]; 'settings-changed': [] }>()
const toast = useToast()
const context = useAssistantContext(props.workspace.id)

const draft = ref('')
const turns = ref<Turn[]>([])
const busy = ref(false)
const showPin = ref(false)
const pinning = ref(false)
const pinTarget = ref<AssistantArtifact | null>(null)
const rerunning = ref<Record<string, boolean>>({})
const savingId = ref<string | null>(null)
const pickerOpen = ref(false)
const anchorOpen = ref(false)
const activeAnchor = ref<AssistantAnswer['citations'][number] | null>(null)
const documentAiEnabled = ref(Boolean(props.workspace.settings?.doc_llm_optin))
const piiMasking = ref(Boolean(props.workspace.settings?.doc_pii_masking))

watch(() => props.workspace.settings?.doc_llm_optin, (value) => { documentAiEnabled.value = Boolean(value) })
watch(() => props.workspace.settings?.doc_pii_masking, (value) => { piiMasking.value = Boolean(value) })

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}
const toolLabel: Record<string, string> = {
  list_tables: 'Listed tables',
  describe_table: 'Profiled a table',
  query_table: 'Ran a query',
  run_analytics: 'Ran an analytics test',
  run_python: 'Ran Python',
}

const placeholder = computed(() => {
  if (!props.configured) return 'Configure an LLM key to talk to the agent'
  if (props.pendingQuestion) return 'Use the focused interaction above to respond'
  if (props.runActive) return 'Queue the next command — it will not interrupt this run'
  if (props.runFinished) return 'Start a linked follow-up command'
  return 'Give the audit assistant a command… (Enter to send)'
})

function send(_followUp = false) {
  const text = draft.value.trim()
  if (!text || busy.value) return
  draft.value = ''
  emit('steer', text)
  // Keep the local Q&A renderer available for legacy history without routing
  // new chat around the durable command queue.
  if (false) void ask(text)
}

async function ask(question: string) {
  const turn: Turn = { question, pending: true }
  turns.value.push(turn)
  busy.value = true
  try {
    turn.answer = await api.post<AssistantAnswer>(
      `/api/workspaces/${props.workspace.id}/assistant`,
      {
        question,
        document_ids: context.documentIds.value,
        mask_pii: piiMasking.value,
      },
    )
  } catch (error) {
    turn.error = error instanceof ApiError ? error.message : String(error)
  } finally {
    turn.pending = false
    busy.value = false
  }
}

async function enableDocumentAi() {
  try {
    const settings = await api.patch<NonNullable<WorkspaceSummary['settings']>>(
      `/api/workspaces/${props.workspace.id}/settings`, { doc_llm_optin: true },
    )
    documentAiEnabled.value = settings.doc_llm_optin
    emit('settings-changed')
  } catch (error) {
    fail('Could not enable Document AI', error)
  }
}

async function setMasking(value: boolean) {
  const previous = piiMasking.value
  piiMasking.value = value
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/settings`, { doc_pii_masking: value })
    emit('settings-changed')
  } catch (error) {
    piiMasking.value = previous
    fail('Could not update masking', error)
  }
}

function showCitation(citation: AssistantAnswer['citations'][number]) {
  activeAnchor.value = citation
  anchorOpen.value = true
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
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

function saveable(artifact: AssistantArtifact): boolean {
  return artifact.kind === 'python' || artifact.kind === 'analytics'
}

async function saveAsAnalysis(artifact: AssistantArtifact) {
  savingId.value = artifact.id
  try {
    await api.post<SavedAnalysis>(`/api/workspaces/${props.workspace.id}/analyses`, {
      kind: artifact.kind,
      table: artifact.table,
      title: artifact.title,
      spec: artifact.kind === 'python' ? { code: artifact.code } : artifact.spec,
      viz: artifact.viz,
      source: 'ai',
    })
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
</script>

<template>
  <div class="chat">
    <div class="chat-history">
      <div v-if="messages.length" class="run-messages">
        <div
          v-for="(message, index) in messages"
          :key="index"
          class="run-message"
          :class="message.role"
        >
          <i :class="message.role === 'user' ? 'pi pi-user' : 'pi pi-sparkles'" />
          <span>{{ message.content }}</span>
        </div>
      </div>

      <div v-for="(turn, index) in turns" :key="index" class="turn">
        <div class="bubble user"><i class="pi pi-user" /><span>{{ turn.question }}</span></div>
        <div v-if="turn.pending" class="bubble agent pending">
          <i class="pi pi-spin pi-spinner" /> Thinking…
        </div>
        <div v-else-if="turn.error" class="bubble agent error">
          <i class="pi pi-exclamation-triangle" /> {{ turn.error }}
        </div>
        <div v-else-if="turn.answer" class="bubble agent">
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

          <div v-if="turn.answer.document_context?.trimmed" class="context-warning">
            <i class="pi pi-exclamation-triangle" />
            <span>
              <strong>Some document context was trimmed</strong>
              <small v-for="item in turn.answer.document_context.manifest.filter(value => value.trimmed)" :key="item.document_id">
                {{ item.title }}: included {{ item.included_pages.length }} of {{ item.total_pages }} pages<span v-if="item.truncated_pages.length">; page {{ item.truncated_pages.join(', ') }} was shortened</span>
              </small>
            </span>
          </div>

          <div v-if="turn.answer.citations?.length" class="answer-citations">
            <Button
              v-for="citation in turn.answer.citations"
              :key="citation.id"
              :label="`${context.documents.value.find(doc => doc.id === citation.source_id)?.title || 'Document'} · p. ${citation.page}`"
              icon="pi pi-link"
              size="small"
              severity="secondary"
              outlined
              @click="showCitation(citation)"
            />
          </div>

          <div v-for="artifact in turn.answer.artifacts" :key="artifact.id" class="artifact">
            <div class="artifact-head">
              <strong>{{ artifact.title }}</strong>
              <Tag
                v-if="artifact.verdict"
                :value="artifact.verdict_text || artifact.verdict"
                :severity="verdictSeverity[artifact.verdict]"
              />
              <span class="grow" />
              <Button
                v-if="saveable(artifact)"
                icon="pi pi-save"
                size="small"
                text
                v-tooltip.top="'Save to analyses'"
                :loading="savingId === artifact.id"
                @click="saveAsAnalysis(artifact)"
              />
              <Button
                icon="pi pi-thumbtack"
                severity="secondary"
                size="small"
                text
                v-tooltip.top="'Pin to dashboard'"
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
              <CodeEditor v-model="artifact.code" />
              <pre v-if="artifact.stdout" class="stdout">{{ artifact.stdout }}</pre>
            </div>
            <ChartView v-if="artifact.frame" :frame="artifact.frame" :viz="artifact.viz" height="240px" />
          </div>
        </div>
      </div>
    </div>

    <div class="composer">
      <div v-if="pendingQuestion" class="pending-question">
        <i class="pi pi-comments" />
        <span><strong>Planning question</strong>{{ pendingQuestion }}</span>
      </div>
      <div v-if="false && context.documents.value.length && !runActive" class="context-chips">
        <span v-for="doc in context.documents.value" :key="doc.id" class="context-chip">
          <i class="pi pi-file" /><span>{{ doc.title }}</span>
          <button :aria-label="`Remove ${doc.title} from context`" @click="context.remove(doc.id)"><i class="pi pi-times" /></button>
        </span>
        <button class="clear-context" @click="context.clear">Clear</button>
      </div>
      <Textarea
        v-model="draft"
        rows="2"
        autoResize
        :disabled="!configured || busy"
        :placeholder="placeholder"
        @keydown="onKeydown"
      />
      <div class="composer-actions">
        <Button
          v-if="false"
          icon="pi pi-paperclip"
          size="small"
          severity="secondary"
          text
          v-tooltip.top="runActive ? 'Document context is available for ordinary chat questions only' : 'Add document context'"
          :disabled="runActive"
          @click="pickerOpen = true"
        />
        <Button
          :icon="runActive ? 'pi pi-clock' : 'pi pi-send'"
          size="small"
          v-tooltip.top="runActive ? 'Queue follow-up command' : 'Send command'"
          :disabled="!configured || !draft.trim()"
          :loading="busy"
          @click="send()"
        />
      </div>
    </div>
  </div>

  <PinDialog
    v-model:visible="showPin"
    :defaultTitle="pinTarget?.title ?? 'Result'"
    :saving="pinning"
    @pin="pin"
  />
  <DocumentContextPicker
    v-model:visible="pickerOpen"
    :workspaceId="workspace.id"
    :selectedIds="context.documentIds.value"
    :documentAiEnabled="documentAiEnabled"
    :piiMasking="piiMasking"
    @apply="context.replace"
    @enable="enableDocumentAi"
    @masking="setMasking"
  />
  <EvidenceAnchorDialog v-model="anchorOpen" :anchor="activeAnchor" />
</template>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-height: 100%;
  min-height: 0;
}
.chat-history {
  flex: 0 1 auto;
  min-height: 0;
  max-height: 14rem;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding-right: 0.15rem;
}
.run-messages { display: flex; flex-direction: column; gap: 0.35rem; }
.run-message {
  display: flex;
  gap: 0.45rem;
  align-items: baseline;
  font-size: 0.82rem;
  padding: 0.4rem 0.6rem;
  border-radius: 8px;
  background: var(--p-primary-50);
  color: var(--p-primary-800);
}
.run-message.agent { background: var(--p-surface-100); color: var(--p-surface-700); }

.turn { display: flex; flex-direction: column; gap: 0.4rem; }
.bubble { border-radius: 8px; padding: 0.55rem 0.7rem; font-size: 0.85rem; }
.bubble.user {
  display: flex;
  gap: 0.45rem;
  align-items: baseline;
  background: var(--p-primary-50);
  color: var(--p-primary-800);
  font-weight: 500;
}
.bubble.agent { background: var(--p-surface-0); border: 1px solid var(--p-surface-200); }
.bubble.pending { color: var(--p-surface-500); }
.bubble.error { color: var(--p-red-500); }
.steps { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.45rem; }
.step {
  font-size: 0.68rem;
  background: var(--p-surface-100);
  color: var(--p-surface-600);
  border-radius: 999px;
  padding: 0.12rem 0.5rem;
}
.step.bad { background: var(--p-red-50); color: var(--p-red-600); }
.answer { margin: 0; white-space: pre-wrap; line-height: 1.45; }
.answer-citations { display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.55rem; }
.context-warning { display:flex; gap:.5rem; margin-top:.55rem; padding:.55rem; border:1px solid var(--p-orange-200); border-radius:7px; color:var(--p-orange-800); background:var(--p-orange-50); font-size:.72rem; }.context-warning span { display:grid; gap:.18rem; }.context-warning small { color:var(--p-orange-700); }

.artifact { border: 1px solid var(--p-surface-200); border-radius: 8px; padding: 0.6rem; margin-top: 0.6rem; }
.artifact-head { display: flex; align-items: center; gap: 0.45rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
.artifact-head strong { font-size: 0.82rem; }
.grow { flex: 1; }
.stat-cards { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.5rem; }
.stat-card {
  background: var(--p-surface-50);
  border: 1px solid var(--p-surface-200);
  border-radius: 6px;
  padding: 0.3rem 0.55rem;
}
.stat-card .label { font-size: 0.65rem; color: var(--p-surface-500); }
.stat-card .value { font-size: 0.85rem; font-weight: 600; }
.code-block { margin-bottom: 0.5rem; }
.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.72rem;
  color: var(--p-surface-500);
  margin-bottom: 0.2rem;
}
.stdout {
  background: var(--p-surface-900);
  color: var(--p-surface-0);
  border-radius: 6px;
  padding: 0.45rem 0.6rem;
  font-size: 0.72rem;
  overflow-x: auto;
  margin: 0.35rem 0 0;
}
.composer { flex: 0 0 auto; display: flex; gap: 0.45rem; align-items: flex-end; flex-wrap: wrap; }
.context-chips { flex:0 0 100%; display:flex; align-items:center; gap:.3rem; overflow-x:auto; padding:.1rem 0; }.context-chip { display:inline-flex; align-items:center; gap:.3rem; max-width:12rem; padding:.25rem .4rem; border:1px solid var(--p-primary-200); border-radius:999px; color:var(--p-primary-800); background:var(--p-primary-50); font-size:.68rem; }.context-chip > span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.context-chip button,.clear-context { border:0; padding:0; color:inherit; background:transparent; cursor:pointer; }.clear-context { color:var(--p-surface-500); font-size:.68rem; }
.pending-question { flex: 0 0 100%; display: flex; gap: 0.5rem; padding: 0.55rem 0.65rem; border: 1px solid var(--p-primary-200); border-radius: 7px; background: var(--p-primary-50); font-size: 0.78rem; }
.pending-question span { display: flex; flex-direction: column; gap: 0.15rem; }
.composer :deep(textarea) { flex: 1; resize: none; font-size: 0.85rem; }
.composer-actions { display: flex; flex-direction: column; gap: 0.3rem; }
</style>
