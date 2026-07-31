<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'

import type { AgentDecision, AssistantApprovalProjection, AssistantChat, AssistantChatMessage, AssistantInteractionProjection, AssistantMilestoneProjection, AssistantRunProjection, AssistantSuggestion, AuditDocument, EvidenceRef } from '../../types'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import MarkdownView from '../MarkdownView.vue'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'
import AgentMilestoneCard from './AgentMilestoneCard.vue'
import AgentThinking from './AgentThinking.vue'
import ChatArtifactCard from './ChatArtifactCard.vue'
import ChatRunCard from './ChatRunCard.vue'

const props = defineProps<{ workspaceId: string; chat: AssistantChat; documents: AuditDocument[]; actionBusy?: boolean; busy?: boolean }>()
const emit = defineEmits<{
  shortcut: [string, string]
  command: [string]
  suggestion: [AssistantSuggestion]
  retry: [AssistantChatMessage]
  changed: []
  respond: [string, AssistantInteractionProjection['interaction'], Record<string, unknown>]
  decide: [string, AssistantApprovalProjection['approval'], AgentDecision[]]
}>()
const scroller = ref<HTMLElement | null>(null)
const inner = ref<HTMLElement | null>(null)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
// Templates stay available, but they are no longer the first thing offered:
// what this engagement actually needs next leads, because a menu of five fixed
// procedures frames the agent as a form to fill in.
const shortcuts = [
  ['Full audit', 'full_audit_working_draft'], ['Planning', 'planning'],
  ['Data analysis', 'data_analysis'], ['Document tests', 'document_test_preparation'], ['Report', 'report'],
]
const showTemplates = ref(false)
// Transcript covers stored messages, run projections, and the optimistic
// pending message, so it is the single source of truth for emptiness.
// A foreign run is appended for visibility only; a chat with nothing of its own
// is still empty, and should still offer somewhere to start.
const empty = computed(() => props.chat.transcript.every(item => item.type === 'run' && item.foreign === true))

// "Stick to bottom" tracks the user's own scrolling: any content growth
// (new items, run cards updating in place, artifacts rendering) keeps the
// view pinned unless the user has scrolled up to read history.
const stick = ref(true)
let observer: ResizeObserver | null = null
function nearBottom() {
  const node = scroller.value
  return !node || node.scrollHeight - node.scrollTop - node.clientHeight < 100
}
function onScroll() { stick.value = nearBottom() }
function scrollBottom() { nextTick(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight }) }
onMounted(() => {
  scrollBottom()
  if (inner.value && 'ResizeObserver' in window) {
    observer = new ResizeObserver(() => { if (stick.value) scrollBottom() })
    observer.observe(inner.value)
  }
})
onBeforeUnmount(() => { observer?.disconnect(); observer = null })
watch(() => props.chat.id, () => { stick.value = true; scrollBottom() })
watch(() => props.chat.transcript.length, () => { if (stick.value) scrollBottom() })

type TranscriptItem = AssistantChat['transcript'][number]
function isRun(item: TranscriptItem): item is AssistantRunProjection { return item.type === 'run' }
function isInteraction(item: TranscriptItem): item is AssistantInteractionProjection { return item.type === 'interaction' }
function isApproval(item: TranscriptItem): item is AssistantApprovalProjection { return item.type === 'approval' }
function isMilestone(item: TranscriptItem): item is AssistantMilestoneProjection { return item.type === 'milestone' }
// Narrower than the card's notion of "active": a run that is paused, waiting on
// the auditor, or interrupted is not working, and animating a phase line for it
// claims progress that isn't happening.
const WORKING_RUN_STATUSES = new Set(['queued', 'interpreting', 'executing', 'verifying'])
function isRunWorking(item: AssistantRunProjection) { return WORKING_RUN_STATUSES.has(item.status) }
// What the agent is doing right now belongs after everything it has already
// said. Rendering it at the run's own anchor stranded a live, ticking phase
// line above a growing column of milestones, so the one part of the transcript
// still changing sat furthest from where the reader was looking.
const working = computed<AssistantRunProjection | null>(() => {
  for (let index = props.chat.transcript.length - 1; index >= 0; index -= 1) {
    const item = props.chat.transcript[index]
    if (isRun(item) && !item.foreign && isRunWorking(item)) return item
  }
  return null
})
// The word pool follows what the user actually asked for; the trailing pending
// message is the request currently in flight.
const pendingIntent = computed(() => {
  for (let index = props.chat.transcript.length - 1; index >= 0; index -= 1) {
    const item = props.chat.transcript[index]
    if (item.type === 'message' && item.role === 'user') {
      return (item.requested_intent === 'ask' || item.requested_intent === 'act') ? item.requested_intent : 'auto'
    }
  }
  return 'auto'
})
function openCitation(value: NonNullable<AssistantChatMessage['citations']>[number]) { anchor.value = value; anchorOpen.value = true }
function documentTitle(id: string) { return props.documents.find(item => item.id === id)?.title ?? 'Unavailable document' }
function messageTime(value: string) {
  const time = new Date(value)
  return Number.isNaN(time.getTime()) ? undefined : time.toLocaleString()
}
</script>

<template>
  <div ref="scroller" class="transcript" @scroll.passive="onScroll">
    <div ref="inner" class="transcript-inner">
    <div v-if="empty" class="empty-state">
      <span class="empty-icon"><i class="pi pi-sparkles" /></span>
      <strong>What should we work on?</strong>
      <p>Ask me anything about this engagement, or tell me what to do next.</p>
      <div v-if="chat.suggestions?.length" class="suggestions">
        <button v-for="item in chat.suggestions" :key="item.capability" class="suggestion" @click="emit('suggestion', item)">
          <strong>{{ item.label }}</strong>
          <small v-if="item.reason">{{ item.reason }}</small>
        </button>
      </div>
      <button class="templates-toggle" @click="showTemplates = !showTemplates">
        {{ showTemplates ? 'Hide' : 'Or start a guided workflow' }}
        <i :class="showTemplates ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
      </button>
      <div v-if="showTemplates" class="shortcuts">
        <Button v-for="([label, template]) in shortcuts" :key="template" :label="label" size="small" severity="secondary" outlined @click="emit('shortcut', label, template)" />
      </div>
    </div>

    <template v-for="item in chat.transcript" :key="item.id">
      <!-- The left rail owns the plan. The transcript shows only the live
           activity line, durable milestones, attention, and the run receipt. -->
      <!-- A run owned by another chat is context, not this conversation's
           work: it stays a compact receipt here. -->
      <ChatRunCard v-if="isRun(item)" :workspaceId="workspaceId" :projection="item" :showAttention="item.foreign === true" @changed="emit('changed')" @command="emit('command', $event)" />
      <AgentMilestoneCard v-else-if="isMilestone(item)" :milestone="item.milestone" />
      <AgentInteractionCard v-else-if="isInteraction(item)" :interaction="item.interaction" :busy="actionBusy ?? false" :workspaceId="workspaceId" :runId="item.run_id" @respond="emit('respond', item.run_id, item.interaction, $event)" />
      <AgentApprovalCard v-else-if="isApproval(item)" :approval="item.approval" :busy="actionBusy ?? false" @decide="emit('decide', item.run_id, item.approval, $event)" />
      <div v-else class="message" :class="[item.role, item.kind, item.state]">
        <div class="bubble" :title="messageTime(item.created_at)">
          <i v-if="item.state === 'pending'" class="pi pi-spin pi-spinner" />
          <i v-else-if="item.kind === 'error'" class="pi pi-exclamation-triangle" />
          <MarkdownView v-if="item.role === 'assistant' && item.kind === 'text'" class="bubble-markdown" :markdown="item.content" />
          <p v-else>{{ item.content }}</p>
          <small v-if="item.role === 'user' && item.requested_intent !== 'auto'" class="intent">{{ item.requested_intent }}</small>
        </div>
        <Button v-if="item.state === 'failed' && item.role === 'user'" label="Retry" icon="pi pi-refresh" text size="small" @click="emit('retry', item)" />
        <details v-if="item.tool_trace?.length" class="trace"><summary>{{ item.tool_trace.length }} local tool step(s)</summary><div v-for="(step,index) in item.tool_trace" :key="index"><i :class="step.ok ? 'pi pi-check' : 'pi pi-times'" /> {{ step.tool }}</div></details>
        <div v-if="item.document_manifest?.trimmed" class="warning"><i class="pi pi-exclamation-triangle" /> Some attached document text was trimmed to the safe context budget.</div>
        <div v-if="item.citations?.length" class="citations">
          <Button v-for="citation in item.citations" :key="citation.id" :label="`${documentTitle(citation.source_id)} · p. ${citation.page}`" icon="pi pi-link" size="small" severity="secondary" outlined :disabled="citation.available === false || !documents.some(doc => doc.id === citation.source_id)" @click="openCitation(citation)" />
        </div>
        <template v-for="artifactId in item.artifact_ids" :key="artifactId">
          <ChatArtifactCard v-if="chat.artifacts[artifactId]" :workspaceId="workspaceId" :artifact="chat.artifacts[artifactId]" />
        </template>
      </div>
    </template>

    <!-- Timed from the model call when there is one, so a long provider round
         trip is what the counter actually reflects. -->
    <div v-if="working" class="run-log">
      <AgentThinking
        :label="working.current_activity"
        :startedAt="working.activity?.model_started_at ?? working.activity?.started_at ?? working.started"
      />
    </div>
    <div v-else-if="busy" class="message assistant">
      <div class="bubble typing">
        <AgentThinking :intent="pendingIntent" />
      </div>
    </div>
    </div>
  </div>
  <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
</template>

<style scoped>
.transcript{flex:1;min-height:0;overflow:auto}.transcript-inner{display:flex;flex-direction:column;gap:.65rem;min-height:100%;padding:.8rem .9rem}.empty-state{display:grid;justify-items:center;gap:.55rem;margin:auto;padding:1.5rem;text-align:center}.empty-icon{display:grid;place-items:center;width:3rem;height:3rem;border-radius:12px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:1.25rem}.empty-state p{margin:0;color:var(--aw-muted);font-size:.78rem}.shortcuts{display:flex;justify-content:center;flex-wrap:wrap;gap:.4rem;margin-top:.35rem}.suggestions{display:grid;gap:.35rem;width:100%;max-width:20rem;margin-top:.35rem}.suggestion{display:grid;gap:.1rem;padding:.5rem .6rem;border:1px solid var(--aw-border);border-radius:8px;background:var(--p-surface-0);text-align:left;cursor:pointer;color:inherit}.suggestion:hover{border-color:var(--aw-teal);background:var(--aw-teal-soft)}.suggestion strong{font-size:.78rem;font-weight:500}.suggestion small{font-size:.68rem;color:var(--aw-muted)}.templates-toggle{display:inline-flex;align-items:center;gap:.3rem;margin-top:.2rem;border:0;background:transparent;color:var(--aw-muted);font-size:.7rem;cursor:pointer}.templates-toggle i{font-size:.55rem}.message{max-width:92%}.message.user{align-self:flex-end}.message.assistant{align-self:flex-start}.bubble{position:relative;display:flex;align-items:center;gap:.4rem;padding:.55rem .7rem;border-radius:10px;background:var(--p-surface-100);font-size:.79rem;line-height:1.4}.user .bubble{background:var(--aw-teal);color:white;border-bottom-right-radius:3px}.assistant .bubble{background:var(--p-surface-100);border-bottom-left-radius:3px}.clarification .bubble{background:#fff7e6;border:1px solid #f0d9a8}.error .bubble,.failed .bubble{background:var(--p-red-50);color:var(--p-red-700)}.bubble p{margin:0;white-space:pre-wrap}.bubble-markdown{min-width:0;font-size:.79rem}.bubble-markdown :deep(> :first-child){margin-top:0}.bubble-markdown :deep(> :last-child){margin-bottom:0}.bubble-markdown :deep(h1){font-size:.95rem;margin:.5rem 0 .3rem}.bubble-markdown :deep(h2){font-size:.88rem;margin:.5rem 0 .25rem}.bubble-markdown :deep(h3),.bubble-markdown :deep(h4){font-size:.82rem;margin:.45rem 0 .2rem}.bubble-markdown :deep(table){font-size:.72rem;margin:.45rem 0}.bubble-markdown :deep(th),.bubble-markdown :deep(td){padding:.3rem .4rem}.bubble.typing{padding:.5rem .7rem;background:transparent}
.run-log{display:grid;gap:.3rem;max-width:92%;align-self:flex-start;padding:.1rem 0}
.intent{align-self:flex-end;opacity:.65;text-transform:uppercase;font-size:.55rem}.trace{margin:.3rem 0;font-size:.68rem;color:var(--aw-muted)}.trace summary{cursor:pointer}.trace div{padding:.1rem .3rem}.citations{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.4rem}.warning{margin-top:.35rem;padding:.4rem;border-radius:6px;background:#fff7e6;color:#8a5a00;font-size:.68rem}
</style>
