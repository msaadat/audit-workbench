<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useDecisions } from '../composables/useDecisions'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import { capabilityClause } from '../components/agent/capabilityLabels'
import type { AgentDecision, DecisionItem, DecisionKind } from '../types'
import AgentApprovalCard from '../components/agent/AgentApprovalCard.vue'
import AgentInteractionCard from '../components/agent/AgentInteractionCard.vue'

/**
 * Everything waiting on the auditor, in one queue.
 *
 * Resolution deliberately does not get its own endpoints: approvals and
 * interactions post to the same agent routes the console posts to, and a
 * blocker's suggestions are ordinary chat commands. Anything this surface
 * cannot resolve in place deep-links to the surface that owns it, so the queue
 * can summarize without ever becoming a second source of truth.
 */

const props = defineProps<{ id: string }>()
const toast = useToast()
const nav = useWorkspaceNav()
const { workspace } = inject(workspaceContextKey)!
const decisions = useDecisions(props.id)
const agent = useAgentRun(props.id)
const chats = useAssistantChat(props.id)

const filter = ref<DecisionKind | 'all'>('all')
const selectedId = ref<string | null>(null)
const busy = ref(false)

/** Where a decision opens, in the auditor's words rather than the route's. */
const TARGET_LABELS: Record<string, string> = {
  console: 'the console',
  rcm: 'the RCM',
  apm: 'the planning memorandum',
  'doc-tests': 'document tests',
  'data-tests': 'data tests',
  documents: 'documents',
  findings: 'findings',
  report: 'the report',
  dashboard: 'the dashboard',
  data: 'tables',
  query: 'query',
  analysis: 'analysis',
  planning: 'planning',
  validation: 'tables',
  decisions: 'decisions',
}

const KIND_LABELS: Record<DecisionKind, string> = {
  approval: 'Approvals',
  interaction: 'Questions',
  blocker: 'Blockers',
  doc_test_item: 'Test items',
  observation: 'Dispositions',
  quality: 'Quality',
}

const visible = computed(() => decisions.items.value.filter(
  item => filter.value === 'all' || item.kind === filter.value,
))
const selected = computed(() =>
  visible.value.find(item => item.id === selectedId.value) ?? visible.value[0] ?? null)

const segments = computed(() => {
  const counts = decisions.state.payload?.by_kind
  const present = (Object.keys(KIND_LABELS) as DecisionKind[])
    .filter(kind => (counts?.[kind] ?? 0) > 0)
    .map(kind => ({ key: kind as DecisionKind | 'all', label: KIND_LABELS[kind], count: counts?.[kind] ?? 0 }))
  return [{ key: 'all' as const, label: 'All', count: decisions.total.value }, ...present]
})

/**
 * The inline cards need the live record, not the queue's summary of it. Only
 * offer them when the store holds the very run the decision came from —
 * otherwise the console is the honest destination.
 */
const liveRun = computed(() => agent.state.run)
const approvalFor = computed(() => {
  const item = selected.value
  if (!item || item.kind !== 'approval') return null
  if (liveRun.value?.id !== item.source_ref.run_id) return null
  return liveRun.value?.approvals.find(entry => entry.id === item.source_ref.approval_id) ?? null
})
const interactionFor = computed(() => {
  const item = selected.value
  if (!item || item.kind !== 'interaction') return null
  if (liveRun.value?.id !== item.source_ref.run_id) return null
  return liveRun.value?.interactions?.find(entry => entry.id === item.source_ref.interaction_id) ?? null
})

function consequence(item: DecisionItem) {
  const { next, downstream } = item.unblocks
  if (!next.length) return ''
  const steps = downstream === 1 ? '1 later step' : `${downstream} later steps`
  return `Resolving this releases ${capabilityClause(next)} — ${steps} wait on it.`
}

function age(value: string | null) {
  if (!value) return ''
  const ms = Date.now() - Date.parse(value)
  if (!Number.isFinite(ms) || ms < 0) return ''
  const minutes = Math.floor(ms / 60000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`
}

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

function open(item: DecisionItem) {
  void nav.replaceTarget(item.target)
}

async function decide(decisionList: AgentDecision[]) {
  const item = selected.value
  const approval = approvalFor.value
  if (!item || !approval) return
  busy.value = true
  try {
    // The same endpoint the console posts to — this surface adds no new path.
    await api.post(
      `/api/workspaces/${props.id}/agent/runs/${item.source_ref.run_id}/approvals/${approval.id}`,
      { decisions: decisionList },
    )
    await decisions.load()
  } catch (error) { fail('Decision failed', error) }
  finally { busy.value = false }
}

async function respond(response: Record<string, unknown>) {
  const item = selected.value
  const interaction = interactionFor.value
  if (!item || !interaction) return
  busy.value = true
  try {
    await api.post(
      `/api/workspaces/${props.id}/agent/runs/${item.source_ref.run_id}/interactions/${interaction.id}/respond`,
      response,
    )
    await decisions.load()
  } catch (error) { fail('Response failed', error) }
  finally { busy.value = false }
}

// A blocker's suggestions are chat commands: answering one steers the agent
// through the ordinary command path, exactly as typing it into the composer.
async function steer(command: string) {
  busy.value = true
  try {
    await chats.send(command, 'act', agent.launchMode.value, { source: 'composer' })
    await nav.replace('console')
  } catch (error) { fail('Could not send that to the agent', error) }
  finally { busy.value = false }
}

function move(delta: number) {
  const list = visible.value
  if (!list.length) return
  const index = list.findIndex(item => item.id === selected.value?.id)
  const next = Math.min(Math.max((index < 0 ? 0 : index) + delta, 0), list.length - 1)
  selectedId.value = list[next].id
}

function onKey(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  // Never steal keys from a field the auditor is typing in.
  if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
  if (event.key === 'j') { event.preventDefault(); move(1) }
  else if (event.key === 'k') { event.preventDefault(); move(-1) }
  else if (event.key === 'Enter' && selected.value) { event.preventDefault(); open(selected.value) }
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  decisions.watchWorkspace()
  await Promise.all([decisions.load(), agent.init(), chats.init()])
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
// Filtering away the selection should land on something, not nothing.
watch(visible, list => {
  if (!list.some(item => item.id === selectedId.value)) selectedId.value = list[0]?.id ?? null
})
</script>

<template>
  <div class="ui-surface decisions">
    <div class="queue">
      <header class="queue-head">
        <h2>Decisions</h2>
        <p v-if="decisions.state.payload?.run.waiting" class="waiting">
          <i class="pi pi-pause-circle" /> The agent is paused until you answer.
        </p>
        <p v-else-if="!decisions.total.value && !decisions.state.loading" class="waiting clear">
          <i class="pi pi-check-circle" /> Nothing is waiting on you.
        </p>
      </header>

      <nav v-if="segments.length > 1" class="segments" aria-label="Filter decisions">
        <button
          v-for="segment in segments"
          :key="segment.key"
          :class="{ on: filter === segment.key }"
          @click="filter = segment.key as DecisionKind | 'all'"
        >{{ segment.label }} <b>{{ segment.count }}</b></button>
      </nav>

      <div class="list">
        <button
          v-for="item in visible"
          :key="item.id"
          class="row"
          :class="{ sel: item.id === selected?.id }"
          @click="selectedId = item.id"
        >
          <span class="stripe" :data-severity="item.severity" />
          <span class="text">
            <b>{{ item.title }}</b>
            <small>{{ item.context }}</small>
          </span>
          <span class="meta">
            <em v-if="item.unblocks.downstream">{{ item.unblocks.downstream }}&nbsp;blocked</em>
            <em v-if="age(item.created_at)">{{ age(item.created_at) }}</em>
          </span>
        </button>
        <p v-if="!visible.length && decisions.state.loading" class="empty"><i class="pi pi-spin pi-spinner" /> Loading…</p>
        <p v-else-if="!visible.length" class="empty">Nothing here.</p>
      </div>

      <footer class="keys">
        <span><kbd>J</kbd><kbd>K</kbd> move</span>
        <span><kbd>↵</kbd> open</span>
      </footer>
    </div>

    <div class="detail">
      <template v-if="selected">
        <p class="eyebrow">{{ KIND_LABELS[selected.kind] }}</p>
        <h3>{{ selected.title }}</h3>
        <p v-if="selected.context" class="context">{{ selected.context }}</p>
        <p v-if="consequence(selected)" class="consequence">
          <i class="pi pi-arrow-right" />{{ consequence(selected) }}
        </p>

        <AgentApprovalCard v-if="approvalFor" :approval="approvalFor" :busy="busy" @decide="decide" />
        <AgentInteractionCard
          v-else-if="interactionFor"
          :interaction="interactionFor"
          :busy="busy"
          :workspaceId="props.id"
          :runId="selected.source_ref.run_id ?? ''"
          @respond="respond"
        />

        <div v-if="selected.source_ref.suggestions?.length" class="suggestions">
          <p class="label">Answer the agent</p>
          <Button
            v-for="suggestion in selected.source_ref.suggestions"
            :key="suggestion.command"
            :label="suggestion.label"
            size="small"
            outlined
            :disabled="busy"
            @click="steer(suggestion.command)"
          />
        </div>

        <div class="actions">
          <Button
            v-if="!approvalFor && !interactionFor"
            :label="`Open in ${TARGET_LABELS[selected.target.tab] ?? selected.target.tab}`"
            icon="pi pi-arrow-up-right"
            size="small"
            @click="open(selected)"
          />
        </div>
      </template>
      <div v-else class="detail-empty">
        <span class="icon"><i class="pi pi-check-circle" /></span>
        <strong>{{ workspace.name }} has nothing waiting</strong>
        <p>Approvals, questions, blocked work, review items, and dispositions all arrive here.</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.decisions { background: var(--aw-canvas); }
.queue { flex: 0 0 24rem; display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--aw-border); background: var(--aw-panel); }
.queue-head { padding: 0.9rem 1rem 0.6rem; }
.queue-head h2 { margin: 0; font-size: var(--aw-text-lg); }
.waiting { display: flex; align-items: center; gap: 0.35rem; margin: 0.3rem 0 0; color: var(--aw-warn); font-size: var(--aw-text-xs); }
.waiting.clear { color: var(--aw-ok); }

.segments { display: flex; flex-wrap: wrap; gap: 0.25rem; padding: 0 1rem 0.6rem; }
.segments button { padding: 0.2rem 0.5rem; border: 1px solid var(--aw-border); border-radius: 999px; background: var(--aw-panel); color: #46587a; font-size: 0.7rem; cursor: pointer; }
.segments button.on { border-color: var(--aw-navy-900); background: var(--aw-navy-900); color: #fff; }
.segments b { font-variant-numeric: tabular-nums; }

.list { flex: 1; min-height: 0; overflow-y: auto; border-top: 1px solid var(--aw-border); }
.row { display: flex; gap: 0.5rem; width: 100%; padding: 0.6rem 0.85rem; border: 0; border-bottom: 1px solid var(--aw-border); background: transparent; text-align: left; cursor: pointer; }
.row:hover { background: var(--aw-raised); }
.row.sel { background: var(--aw-teal-soft); box-shadow: inset 3px 0 0 var(--aw-teal); }
.stripe { flex: 0 0 3px; border-radius: 2px; background: var(--aw-border-strong); }
.stripe[data-severity='critical'] { background: var(--aw-danger); }
.stripe[data-severity='warning'] { background: var(--aw-warn); }
.stripe[data-severity='info'] { background: var(--aw-teal); }
.text { flex: 1; min-width: 0; display: grid; gap: 0.1rem; }
.text b { font-size: var(--aw-text-sm); font-weight: 600; }
.text small { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.meta { display: grid; gap: 0.15rem; justify-items: end; }
.meta em { color: var(--aw-muted); font-size: 0.62rem; font-style: normal; white-space: nowrap; }

.keys { display: flex; gap: 0.9rem; padding: 0.45rem 0.85rem; border-top: 1px solid var(--aw-border); color: var(--aw-muted); font-size: 0.66rem; }
kbd { display: inline-block; min-width: 1rem; margin-right: 0.2rem; padding: 0 0.25rem; border: 1px solid var(--aw-border); border-bottom-width: 2px; border-radius: 4px; background: var(--aw-canvas); font-family: var(--aw-font-mono); font-size: 0.6rem; text-align: center; }

.detail { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; padding: 1.1rem 1.35rem; }
.detail h3 { margin: 0.1rem 0 0; font-size: var(--aw-text-xl); line-height: 1.25; text-wrap: balance; }
.context { margin: 0.5rem 0 0; max-width: 62ch; color: #253c5c; line-height: 1.6; }
.consequence { display: flex; align-items: baseline; gap: 0.4rem; margin: 0.8rem 0 1rem; padding: 0.55rem 0.7rem; border-left: 3px solid var(--aw-teal); border-radius: 0 var(--aw-radius-sm) var(--aw-radius-sm) 0; background: var(--aw-teal-soft); color: var(--aw-teal); font-size: var(--aw-text-sm); }
.consequence i { font-size: 0.6rem; }

.suggestions { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; margin-top: 1rem; }
.suggestions .label { width: 100%; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
.actions { margin-top: 1.1rem; }
.empty { padding: 1.2rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }

.detail-empty { display: grid; place-items: center; align-content: center; gap: 0.4rem; min-height: 60%; text-align: center; }
.detail-empty .icon { display: grid; place-items: center; width: 3rem; height: 3rem; border-radius: var(--aw-radius-sm); background: var(--aw-ok-soft); color: var(--aw-ok); font-size: 1.3rem; }
.detail-empty p { max-width: 34ch; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

@container workspace-panel (max-width: 50rem) {
  .decisions { flex-direction: column; }
  .queue { flex: 0 0 auto; max-height: 50%; border-right: 0; border-bottom: 1px solid var(--aw-border); }
}
</style>
