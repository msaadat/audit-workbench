<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type { AgentDecision, WorkspaceSummary } from '../../types'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentChat from './AgentChat.vue'
import AgentSummary from './AgentSummary.vue'
import AgentTaskList from './AgentTaskList.vue'

// The persistent right-side agent panel: launch controls, the live task
// plan, approval gates, steering chat, run history, and the final summary.
// It stays mounted while the user moves between tabs, driven by the shared
// useAgentRun store (SSE events → live updates).
const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ 'settings-changed': [] }>()
const toast = useToast()

const agent = useAgentRun(props.workspace.id)
const { state, isActive, pendingApproval, launchMode } = agent

const runKind = ref<'analysis' | 'planning'>('analysis')
const objective = ref('')
const showLaunch = ref(false)
const showHistory = ref(false)
const deciding = ref(false)
const drawerWidth = ref(416)
const resizing = ref(false)

const MIN_DRAWER_WIDTH = 320
const MAX_DRAWER_WIDTH = 640
const WIDTH_STORAGE_KEY = `audit-workbench:agent-drawer-width:${props.workspace.id}`

const kindOptions = [
  { label: 'Data analysis', value: 'analysis' },
  { label: 'Planning', value: 'planning' },
]

const statusSeverity: Record<string, string> = {
  completed: 'success',
  failed: 'danger',
  cancelled: 'secondary',
  paused: 'secondary',
  interrupted: 'warn',
  awaiting_approval: 'warn',
  awaiting_input: 'warn',
}

const statusLabel = computed(() =>
  (state.run?.status ?? '').replaceAll('_', ' '),
)
const launchVisible = computed(
  () => showLaunch.value || (!state.run && !isActive.value),
)
const runFinished = computed(() =>
  ['completed', 'failed', 'cancelled'].includes(state.run?.status ?? ''),
)

onMounted(() => {
  const savedWidth = Number(window.localStorage.getItem(WIDTH_STORAGE_KEY))
  if (Number.isFinite(savedWidth) && savedWidth > 0) {
    drawerWidth.value = clampWidth(savedWidth)
  }
  void agent.init()
})

onUnmounted(stopResize)

function clampWidth(width: number) {
  // Leave enough room for the workspace navigation and useful tab content.
  const viewportMaximum = Math.max(MIN_DRAWER_WIDTH, window.innerWidth - 480)
  return Math.min(Math.max(width, MIN_DRAWER_WIDTH), MAX_DRAWER_WIDTH, viewportMaximum)
}

function startResize(event: PointerEvent) {
  if (!state.drawerOpen || window.innerWidth <= 900) return
  event.preventDefault()
  resizing.value = true
  document.body.classList.add('agent-drawer-resizing')
  window.addEventListener('pointermove', resize)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
}

function resize(event: PointerEvent) {
  drawerWidth.value = clampWidth(window.innerWidth - event.clientX)
}

function stopResize() {
  if (!resizing.value) return
  resizing.value = false
  document.body.classList.remove('agent-drawer-resizing')
  window.removeEventListener('pointermove', resize)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
  window.localStorage.setItem(WIDTH_STORAGE_KEY, String(drawerWidth.value))
}

function resizeWithKeyboard(event: KeyboardEvent) {
  if (!state.drawerOpen) return
  const delta = event.key === 'ArrowLeft' ? 16 : event.key === 'ArrowRight' ? -16 : 0
  if (!delta) return
  event.preventDefault()
  drawerWidth.value = clampWidth(drawerWidth.value + delta)
  window.localStorage.setItem(WIDTH_STORAGE_KEY, String(drawerWidth.value))
}

async function start() {
  try {
    await agent.startRun(launchMode.value, {
      objective: objective.value.trim() || undefined,
    }, runKind.value)
    showLaunch.value = false
    showHistory.value = false
  } catch (error) {
    fail('Could not start the agent', error)
  }
}

async function decide(decisions: AgentDecision[]) {
  if (!pendingApproval.value) return
  deciding.value = true
  try {
    await agent.decide(pendingApproval.value.id, decisions)
  } catch (error) {
    fail('Could not apply decisions', error)
  } finally {
    deciding.value = false
  }
}

async function control(action: 'pause' | 'resume' | 'cancel') {
  try {
    await agent[action]()
  } catch (error) {
    fail(`Could not ${action} the run`, error)
  }
}

async function steer(content: string) {
  try {
    await agent.sendMessage(content)
  } catch (error) {
    fail('Message failed', error)
  }
}

async function openHistoryRun(runId: string) {
  try {
    await agent.openRun(runId)
    showHistory.value = false
    showLaunch.value = false
  } catch (error) {
    fail('Could not open the run', error)
  }
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 7000 })
}
</script>

<template>
  <aside
    class="agent-drawer"
    :class="{ collapsed: !state.drawerOpen, resizing }"
    :style="state.drawerOpen ? { flexBasis: `${drawerWidth}px` } : undefined"
  >
    <div
      v-if="state.drawerOpen"
      class="resize-handle"
      role="separator"
      aria-label="Resize audit assistant panel"
      aria-orientation="vertical"
      :aria-valuenow="drawerWidth"
      :aria-valuemin="MIN_DRAWER_WIDTH"
      :aria-valuemax="MAX_DRAWER_WIDTH"
      tabindex="0"
      @pointerdown="startResize"
      @keydown="resizeWithKeyboard"
    />
    <div class="drawer-head">
      <i v-if="state.drawerOpen" class="pi pi-sparkles" />
      <Button
        v-else
        icon="pi pi-sparkles"
        text
        size="small"
        severity="secondary"
        aria-label="Expand audit assistant panel"
        v-tooltip.left="'Expand audit assistant panel'"
        @click="agent.toggleDrawer()"
      />
      <strong v-if="state.drawerOpen">Audit assistant</strong>
      <Tag
        v-if="state.run && state.drawerOpen"
        :value="statusLabel"
        :severity="statusSeverity[state.run.status] ?? 'info'"
      />
      <span v-if="isActive" class="live-dot" :class="{ off: !state.connected }"
            v-tooltip.bottom="state.connected ? 'Live' : 'Reconnecting…'" />
      <span class="grow" />
      <Button
        v-if="state.drawerOpen"
        icon="pi pi-history"
        text
        size="small"
        severity="secondary"
        v-tooltip.bottom="'Run history'"
        @click="showHistory = !showHistory; void agent.loadRuns()"
      />
      <Button
        v-if="state.drawerOpen"
        icon="pi pi-angle-right"
        text
        size="small"
        severity="secondary"
        aria-label="Collapse audit assistant panel"
        v-tooltip.left="'Collapse panel'"
        @click="agent.toggleDrawer()"
      />
    </div>

    <div v-if="state.drawerOpen" class="drawer-body">
      <div v-if="showHistory" class="section history">
        <p class="section-title">Run history</p>
        <p v-if="!state.runs.length" class="muted">No runs yet.</p>
        <button
          v-for="run in state.runs"
          :key="run.id"
          class="history-item"
          :class="{ active: state.run?.id === run.id }"
          @click="openHistoryRun(run.id)"
        >
          <span>{{ run.created.slice(0, 16).replace('T', ' ') }}</span>
          <Tag :value="run.status" :severity="statusSeverity[run.status] ?? 'info'" />
          <small>{{ run.kind === 'planning' ? 'planning' : 'data analysis' }} · {{ run.mode }} · {{ run.task_counts.completed }}/{{ run.task_counts.total }} tasks</small>
        </button>
      </div>

      <div v-if="launchVisible" class="section launch">
        <p class="section-title">Run the audit assistant</p>
        <p class="muted">
          Choose planning to draft the APM, RCM, and audit program from imported
          documents, or data analysis to test loaded tables.
        </p>
        <div v-if="state.status && !state.status.configured" class="warn-note">
          <i class="pi pi-key" />
          The agent's LLM is not configured — set a provider key in
          <code>.env</code> (or <code>AGENT_PROVIDER</code>/<code>AGENT_MODEL</code>).
        </div>
        <SelectButton
          v-model="runKind"
          :options="kindOptions"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          size="small"
        />
        <Textarea
          v-model="objective"
          rows="2"
          autoResize
          placeholder="Optional context — objective, period, known risks…"
        />
        <Button
          label="Run assistant"
          icon="pi pi-play"
          :loading="state.starting"
          :disabled="!state.status?.configured || (runKind === 'analysis' && !workspace.tables.length)"
          @click="start"
        />
        <p v-if="state.status?.configured" class="muted model-note">
          <i class="pi pi-microchip-ai" /> {{ state.status.provider || state.status.backend }}
          · {{ state.status.model }}
        </p>
      </div>

      <template v-if="state.run && !launchVisible">
        <div class="section controls">
          <Tag :value="state.run.mode" severity="secondary" />
          <span class="grow" />
          <Button
            v-if="isActive && state.run.status !== 'paused'"
            icon="pi pi-pause"
            text
            size="small"
            v-tooltip.bottom="'Pause after the current step'"
            @click="control('pause')"
          />
          <Button
            v-if="state.run.status === 'paused' || state.run.status === 'interrupted'"
            icon="pi pi-play"
            text
            size="small"
            v-tooltip.bottom="'Resume'"
            @click="control('resume')"
          />
          <Button
            v-if="isActive || state.run.status === 'interrupted'"
            icon="pi pi-stop-circle"
            text
            size="small"
            severity="danger"
            v-tooltip.bottom="'Cancel the run'"
            @click="control('cancel')"
          />
          <Button
            v-if="!isActive"
            label="New run"
            icon="pi pi-plus"
            size="small"
            outlined
            @click="showLaunch = true"
          />
        </div>

        <div v-if="state.run.error" class="section error-note">
          <i class="pi pi-exclamation-triangle" /> {{ state.run.error }}
        </div>

        <div v-if="state.run.discovery.domain" class="section discovery">
          <p class="section-title">Understanding</p>
          <p class="domain">
            <strong>{{ state.run.discovery.domain }}</strong>
            <Tag
              :value="`${state.run.discovery.confidence ?? 'low'} confidence`"
              severity="secondary"
            />
          </p>
          <ul v-if="state.run.discovery.assumptions?.length" class="assumptions">
            <li v-for="assumption in state.run.discovery.assumptions" :key="assumption">
              {{ assumption }}
            </li>
          </ul>
        </div>

        <div v-if="pendingApproval" class="section">
          <AgentApprovalCard
            :key="pendingApproval.id"
            :approval="pendingApproval"
            :busy="deciding"
            @decide="decide"
          />
        </div>

        <div class="section">
          <p class="section-title">Plan</p>
          <AgentTaskList :stages="state.run.plan.stages" />
        </div>

        <details v-if="state.run.warnings.length" class="section warnings">
          <summary>{{ state.run.warnings.length }} warning{{ state.run.warnings.length === 1 ? '' : 's' }}</summary>
          <ul>
            <li v-for="(warning, index) in state.run.warnings" :key="index">{{ warning }}</li>
          </ul>
        </details>

        <div v-if="state.run.summary_markdown" class="section">
          <AgentSummary
            :markdown="state.run.summary_markdown"
            :findings="state.run.findings"
            :workspaceId="workspace.id"
            :runId="state.run.id"
          />
        </div>
      </template>
    </div>

    <div v-if="state.drawerOpen" class="drawer-foot">
      <AgentChat
        :workspace="workspace"
        :messages="state.run?.messages ?? []"
        :runActive="isActive"
        :runFinished="runFinished"
        :configured="!!state.status?.configured"
        :pendingQuestion="state.run?.interview?.pending_question ?? null"
        @steer="steer"
        @settings-changed="emit('settings-changed')"
      />
      <p class="disclosure">
        <i class="pi pi-shield" /> Rows stay metadata-only. Attached document text is disclosed only with engagement permission.
      </p>
    </div>
  </aside>
</template>

<style scoped>
.agent-drawer {
  flex: 0 0 26rem;
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  border-left: 1px solid var(--aw-border, #d5dde7);
  background: var(--p-surface-0);
}
.agent-drawer.collapsed { flex-basis: 3.25rem; }
.agent-drawer.resizing { transition: none; user-select: none; }
:global(body.agent-drawer-resizing) { cursor: col-resize; user-select: none; }
.resize-handle {
  position: absolute;
  z-index: 2;
  inset: 0 auto 0 -0.3rem;
  width: 0.6rem;
  cursor: col-resize;
  touch-action: none;
  outline: none;
}
.resize-handle::after {
  content: '';
  position: absolute;
  inset: 0 auto 0 0.25rem;
  width: 2px;
  background: transparent;
  transition: background 0.15s;
}
.resize-handle:hover::after,
.resize-handle:focus-visible::after,
.agent-drawer.resizing .resize-handle::after { background: var(--aw-teal, #0b625c); }
.drawer-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.7rem 0.9rem;
  border-bottom: 1px solid var(--aw-border, #d5dde7);
}
.collapsed .drawer-head {
  flex-direction: column;
  height: 100%;
  padding: 0.8rem 0.35rem;
  border-bottom: 0;
}
.drawer-head > i { color: var(--aw-teal, #0b625c); }
.drawer-head strong { font-size: 0.9rem; }
.grow { flex: 1; }
.live-dot {
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 50%;
  background: #22c55e;
  animation: pulse 1.6s infinite;
}
.live-dot.off { background: var(--p-amber-500); animation: none; }
@keyframes pulse { 50% { opacity: 0.35; } }

.drawer-body { flex: 1; min-height: 0; overflow-y: auto; padding: 0.8rem 0.9rem; }
.section { margin-bottom: 1rem; }
.section-title {
  margin: 0 0 0.4rem;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--p-surface-500);
}
.muted { color: var(--p-surface-500); font-size: 0.78rem; margin: 0 0 0.6rem; }

.launch { display: flex; flex-direction: column; gap: 0.6rem; }
.launch :deep(textarea) { width: 100%; font-size: 0.82rem; }
.mode-hint { margin: 0; font-size: 0.72rem; }
.model-note { margin: 0; font-size: 0.72rem; display: flex; align-items: center; gap: 0.35rem; }
.warn-note {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  font-size: 0.78rem;
  background: #fff7e6;
  border: 1px solid #f0d9a8;
  border-radius: 7px;
  padding: 0.5rem 0.65rem;
}
.warn-note code { background: var(--p-surface-100); border-radius: 4px; padding: 0 0.25rem; }

.controls { display: flex; align-items: center; gap: 0.4rem; }
.error-note {
  display: flex;
  gap: 0.5rem;
  color: var(--p-red-600);
  font-size: 0.8rem;
  background: var(--p-red-50);
  border-radius: 7px;
  padding: 0.5rem 0.65rem;
}
.discovery .domain { display: flex; align-items: center; gap: 0.5rem; margin: 0 0 0.3rem; }
.assumptions { margin: 0.2rem 0 0; padding-left: 1.1rem; font-size: 0.76rem; color: var(--p-surface-600); }

.warnings summary { cursor: pointer; font-size: 0.78rem; color: var(--p-amber-600, #b45309); }
.warnings ul { margin: 0.35rem 0 0; padding-left: 1.1rem; font-size: 0.74rem; color: var(--p-surface-600); }

.history { border-bottom: 1px dashed var(--aw-border, #d5dde7); padding-bottom: 0.6rem; }
.history-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  font: inherit;
  font-size: 0.78rem;
  color: inherit;
  text-align: left;
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 7px;
  padding: 0.4rem 0.6rem;
  margin-bottom: 0.35rem;
  cursor: pointer;
}
.history-item:hover { border-color: var(--p-primary-300); }
.history-item.active { border-color: var(--aw-teal, #0b625c); }
.history-item small { color: var(--p-surface-500); margin-left: auto; }

.drawer-foot {
  flex: 0 0 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  max-height: 40%;
  border-top: 1px solid var(--aw-border, #d5dde7);
  padding: 0.6rem 0.9rem 0.5rem;
  overflow: hidden;
}
.disclosure {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  margin: 0.45rem 0 0;
  font-size: 0.68rem;
  color: var(--p-surface-500);
}

@media (max-width: 900px) {
  .agent-drawer { width: 100%; flex-basis: auto !important; }
  .agent-drawer.collapsed { height: 3.25rem; }
  .collapsed .drawer-head { flex-direction: row; padding: 0.55rem 0.9rem; }
  .resize-handle { display: none; }
}
</style>
