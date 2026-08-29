<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'

import { TERMINAL_STATUSES, useAgentRun } from '../../composables/useAgentRun'
import type { AgentDecision, AssistantApprovalProjection, AssistantChat, AssistantChatMessage, AssistantContextProjection, AssistantInteractionProjection, AssistantMilestoneProjection, AssistantRunProjection, AssistantSuggestion, AuditDocument, EvidenceRef } from '../../types'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import MarkdownView from '../MarkdownView.vue'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'
import AgentMilestoneCard from './AgentMilestoneCard.vue'
import AgentContextCard from './AgentContextCard.vue'
import AgentThinking from './AgentThinking.vue'
import ChatArtifactCard from './ChatArtifactCard.vue'
import ChatRunCard from './ChatRunCard.vue'
import { plural } from '../../format'

const props = defineProps<{ workspaceId: string; chat: AssistantChat; documents: AuditDocument[]; actionBusy?: boolean; busy?: boolean; needsSources?: boolean }>()
const emit = defineEmits<{
  shortcut: [string, string]
  command: [string]
  suggestion: [AssistantSuggestion]
  retry: [AssistantChatMessage]
  changed: []
  import: []
  respond: [string, AssistantInteractionProjection['interaction'], Record<string, unknown>]
  decide: [string, AssistantApprovalProjection['approval'], AgentDecision[]]
}>()
const agent = useAgentRun(props.workspaceId)
const scroller = ref<HTMLElement | null>(null)
const inner = ref<HTMLElement | null>(null)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
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
function isContext(item: TranscriptItem): item is AssistantContextProjection { return item.type === 'context' }
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
// Live model text belongs to the run this transcript is showing, and only while
// that run is the one working — a stream left over from another run's call would
// be attributed to this one.
const liveStream = computed(() => {
  const stream = agent.state.stream
  if (!stream || !working.value) return null
  return agent.state.run?.id === working.value.run_id ? stream.text : null
})
// Same attribution rule as liveStream: a structured progress read belongs to
// the run this transcript is showing, and only while that run is the one
// working.
const liveProgress = computed(() => {
  const progress = agent.state.progress
  if (!progress || !working.value) return null
  return agent.state.run?.id === working.value.run_id ? progress.items : null
})
// What this engagement still needs is already computed deterministically for
// the empty-chat starting state; the same facts are worth repeating once a
// command finishes and nothing is left waiting on the auditor — that is
// exactly when "what's next" is a real question again. A run still active, or
// one that settled with something still needing a decision (an approval, a
// blocker), answers that question on its own, so the nudge stays out of its way.
const canOfferNextStep = computed(() => {
  // With nothing imported, every suggestion would be for work that cannot run.
  if (empty.value || props.busy || props.needsSources) return false
  const unresolved = props.chat.runs.some(
    item => !TERMINAL_STATUSES.includes(item.status) || item.pending_attention,
  )
  if (unresolved) return false
  return (props.chat.suggestions?.length ?? 0) > 0 || (props.chat.guided_workflows?.length ?? 0) > 0
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
function documentTitle(id: string) {
  // The file as it arrived, not the slug intake derived from it.
  const found = props.documents.find(item => item.id === id)
  return found?.source || found?.title || 'Unavailable document'
}
function messageTime(value: string) {
  const time = new Date(value)
  return Number.isNaN(time.getTime()) ? undefined : time.toLocaleString()
}
</script>

<template>
  <div ref="scroller" class="transcript" @scroll.passive="onScroll">
    <div ref="inner" class="transcript-inner">
    <!-- Nothing imported yet: every suggestion and workflow below would need
         source material to run, so the console offers the one step that has to
         come first. Same action, icon and framing the audit file's onboarding had
         onboarding card, so the two surfaces agree on what step one is. -->
    <div v-if="empty && needsSources" class="empty-state">
      <span class="empty-icon"><i class="pi pi-folder-open" /></span>
      <strong>Start with the audit folder</strong>
      <p>Drop files anywhere, or pick files or a folder. The workbench stages and classifies supported data and documents before importing them.</p>
      <Button label="Import files" icon="pi pi-upload" @click="emit('import')" />
      <p class="empty-followup">Once the files are in, I can plan the engagement, build the RCM, and run the tests.</p>
    </div>

    <div v-else-if="empty" class="empty-state">
      <span class="empty-icon"><i class="pi pi-sparkles" /></span>
      <strong>What should we work on?</strong>
      <p>Ask me anything about this engagement, or tell me what to do next.</p>
      <section class="empty-section">
        <div v-if="chat.suggestions?.length" class="suggestions">
          <button v-for="item in chat.suggestions" :key="item.capability" class="suggestion" @click="emit('suggestion', item)">
            <strong>{{ item.label }}</strong>
            <small v-if="item.reason">{{ item.reason }}</small>
          </button>
        </div>
        <small v-else class="empty-section-note"> </small>
      </section>
      <section v-if="chat.guided_workflows.length" class="empty-section">
        <span class="empty-section-title">Guided workflows</span>
        <div class="shortcuts">
          <Button v-for="workflow in chat.guided_workflows" :key="workflow.command" :label="workflow.label" size="small" severity="secondary" outlined @click="emit('shortcut', workflow.label, workflow.command)" />
        </div>
      </section>
    </div>

    <template v-for="item in chat.transcript" :key="item.id">
      <!-- The left rail owns the plan. The transcript shows only the live
           activity line, durable milestones, attention, and the run receipt. -->
      <template v-if="isRun(item)">
        <!-- The opening "here is what I'll do" line is its own transcript
             entry anchored at the run's start (see `_plan_line_entry` in
             assistant_chats.py), so it reads as spoken before anything the
             run went on to do rather than being dragged down beside the
             finished receipt. A run owned by another chat is context, not
             this conversation's work: it stays a compact receipt here. -->
        <ChatRunCard :workspaceId="workspaceId" :projection="item" :showAttention="item.foreign === true" @changed="emit('changed')" @command="emit('command', $event)" />
      </template>
      <AgentContextCard v-else-if="isContext(item)" :context="item.context" />
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
        <details v-if="item.tool_trace?.length" class="trace"><summary>{{ plural(item.tool_trace.length, 'local tool step') }}</summary><div v-for="(step,index) in item.tool_trace" :key="index"><i :class="step.ok ? 'pi pi-check' : 'pi pi-times'" /> {{ step.tool }}</div></details>
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
        :stream="liveStream"
        :progress="liveProgress"
      />
    </div>
    <div v-else-if="busy" class="message assistant">
      <div class="bubble typing">
        <AgentThinking :intent="pendingIntent" />
      </div>
    </div>
    <div v-else-if="canOfferNextStep" class="next-steps">
      <span class="next-steps-label">What's next?</span>
      <div v-if="chat.suggestions?.length" class="suggestions">
        <button v-for="item in chat.suggestions" :key="item.capability" class="suggestion" @click="emit('suggestion', item)">
          <strong>{{ item.label }}</strong>
          <small v-if="item.reason">{{ item.reason }}</small>
        </button>
      </div>
      <div v-if="chat.guided_workflows.length" class="shortcuts">
        <Button v-for="workflow in chat.guided_workflows" :key="workflow.command" :label="workflow.label" size="small" severity="secondary" outlined @click="emit('shortcut', workflow.label, workflow.command)" />
      </div>
    </div>
    </div>
  </div>
  <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" :documents="documents" />
</template>

<style scoped>
.transcript{flex:1;min-height:0;overflow:auto}.transcript-inner{display:flex;flex-direction:column;gap:.65rem;min-height:100%;width:100%;max-width:46rem;margin-inline:auto;padding:.8rem .9rem}.empty-state{display:grid;justify-items:center;gap:.55rem;margin:auto;padding:1.5rem;text-align:center}.empty-icon{display:grid;place-items:center;width:3rem;height:3rem;border-radius:var(--aw-radius-surface);background:var(--aw-teal-soft);color:var(--aw-teal);font-size:var(--aw-text-xl)}.empty-state p{margin:0;color:var(--aw-muted);font-size:var(--aw-text-sm)}.empty-section{display:grid;justify-items:center;gap:.35rem;width:100%}.empty-section-title{font-size:var(--aw-text-xs);font-weight:700;color:var(--aw-muted)}.empty-section-note{font-size:var(--aw-text-xs);color:var(--aw-muted)}.shortcuts{display:flex;justify-content:center;flex-wrap:wrap;gap:.4rem}.suggestions{display:grid;gap:.35rem;width:100%;max-width:20rem}.suggestion{display:grid;gap:.1rem;padding:.5rem .6rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);background:var(--aw-panel);text-align:left;cursor:pointer;color:inherit}.suggestion:hover{border-color:var(--aw-teal);background:var(--aw-teal-soft)}.suggestion strong{font-size:var(--aw-text-sm);font-weight:500}.suggestion small{font-size:var(--aw-text-xs);color:var(--aw-muted)}.message{max-width:92%}.message.user{align-self:flex-end}.message.assistant{align-self:flex-start}.bubble{position:relative;display:flex;align-items:center;gap:.4rem;padding:.55rem .7rem;border-radius:var(--aw-radius-surface);font-size:var(--aw-text-sm);line-height:1.4}.message.user .bubble{background:var(--aw-teal);color:white;border-bottom-right-radius:3px}.message.assistant .bubble{padding:0;background:transparent;border-radius:0}.message.user.clarification .bubble{background:var(--aw-warn-soft);border:1px solid var(--aw-warn-line);color:inherit}.message.user.error .bubble,.message.user.failed .bubble{background:var(--aw-danger-soft);color:var(--aw-danger)}.bubble p{margin:0;white-space:pre-wrap}.bubble-markdown{min-width:0;font-size:var(--aw-text-sm)}.bubble-markdown :deep(> :first-child){margin-top:0}.bubble-markdown :deep(> :last-child){margin-bottom:0}.bubble-markdown :deep(h1){font-size:var(--aw-text-md);margin:.5rem 0 .3rem}.bubble-markdown :deep(h2){font-size:var(--aw-text-base);margin:.5rem 0 .25rem}.bubble-markdown :deep(h3),.bubble-markdown :deep(h4){font-size:var(--aw-text-sm);margin:.45rem 0 .2rem}.bubble-markdown :deep(table){font-size:var(--aw-text-xs);margin:.45rem 0}.bubble-markdown :deep(th),.bubble-markdown :deep(td){padding:.3rem .4rem}.bubble.typing{padding:.5rem .7rem;background:transparent}
.empty-state p{max-width:30rem}
.empty-followup{color:var(--aw-muted-strong);font-size:var(--aw-text-xs)}
.run-log{display:grid;gap:.3rem;max-width:92%;align-self:flex-start;padding:.1rem 0}
.next-steps{display:grid;gap:.4rem;max-width:92%;align-self:flex-start;padding:.3rem 0}
.next-steps-label{font-size:var(--aw-text-xs);font-weight:700;color:var(--aw-muted)}
.next-steps .suggestions{max-width:100%}
.next-steps .shortcuts{justify-content:flex-start}
.intent{align-self:flex-end;opacity:.65;font-size:var(--aw-text-2xs)}.trace{margin:.3rem 0;font-size:var(--aw-text-xs);color:var(--aw-muted)}.trace summary{cursor:pointer}.trace div{padding:.1rem .3rem}.citations{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.4rem}.warning{margin-top:.35rem;padding:.4rem;border-radius:var(--aw-radius-control);background:var(--aw-warn-soft);color:var(--aw-warn-ink);font-size:var(--aw-text-xs)}
</style>
