<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'

import type { AgentDecision, AssistantApprovalProjection, AssistantChat, AssistantChatMessage, AssistantInteractionProjection, AssistantRunProjection, AuditDocument, EvidenceRef } from '../../types'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import MarkdownView from '../MarkdownView.vue'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'
import ChatArtifactCard from './ChatArtifactCard.vue'
import ChatRunCard from './ChatRunCard.vue'

const props = defineProps<{ workspaceId: string; chat: AssistantChat; documents: AuditDocument[]; actionBusy?: boolean; busy?: boolean }>()
const emit = defineEmits<{
  shortcut: [string, string]
  retry: [AssistantChatMessage]
  changed: []
  respond: [string, AssistantInteractionProjection['interaction'], Record<string, unknown>]
  decide: [string, AssistantApprovalProjection['approval'], AgentDecision[]]
}>()
const scroller = ref<HTMLElement | null>(null)
const inner = ref<HTMLElement | null>(null)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
const shortcuts = [
  ['Full audit', 'full_audit_working_draft'], ['Planning', 'planning'],
  ['Data analysis', 'data_analysis'], ['Document testing', 'document_testing'], ['Report', 'report'],
]
// Transcript covers stored messages, run projections, and the optimistic
// pending message, so it is the single source of truth for emptiness.
const empty = computed(() => props.chat.transcript.length === 0)

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
      <p>Ask a question, or start with a guided audit workflow.</p>
      <div class="shortcuts">
        <Button v-for="([label, template]) in shortcuts" :key="template" :label="label" size="small" severity="secondary" outlined @click="emit('shortcut', label, template)" />
      </div>
    </div>

    <template v-for="item in chat.transcript" :key="item.id">
      <ChatRunCard v-if="isRun(item)" :workspaceId="workspaceId" :projection="item" :showAttention="item.foreign === true" @changed="emit('changed')" />
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

    <div v-if="busy" class="message assistant">
      <div class="bubble typing" aria-label="Assistant is working">
        <span class="dot" /><span class="dot" /><span class="dot" />
      </div>
    </div>
    </div>
  </div>
  <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
</template>

<style scoped>
.transcript{flex:1;min-height:0;overflow:auto}.transcript-inner{display:flex;flex-direction:column;gap:.65rem;min-height:100%;padding:.8rem .9rem}.empty-state{display:grid;justify-items:center;gap:.55rem;margin:auto;padding:1.5rem;text-align:center}.empty-icon{display:grid;place-items:center;width:3rem;height:3rem;border-radius:12px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:1.25rem}.empty-state p{margin:0;color:var(--aw-muted);font-size:.78rem}.shortcuts{display:flex;justify-content:center;flex-wrap:wrap;gap:.4rem;margin-top:.35rem}.message{max-width:92%}.message.user{align-self:flex-end}.message.assistant{align-self:flex-start}.bubble{position:relative;display:flex;gap:.4rem;padding:.55rem .7rem;border-radius:10px;background:var(--p-surface-100);font-size:.79rem;line-height:1.4}.user .bubble{background:var(--aw-teal);color:white;border-bottom-right-radius:3px}.assistant .bubble{background:var(--p-surface-100);border-bottom-left-radius:3px}.clarification .bubble{background:#fff7e6;border:1px solid #f0d9a8}.error .bubble,.failed .bubble{background:var(--p-red-50);color:var(--p-red-700)}.bubble p{margin:0;white-space:pre-wrap}.bubble-markdown{min-width:0;font-size:.79rem}.bubble-markdown :deep(> :first-child){margin-top:0}.bubble-markdown :deep(> :last-child){margin-bottom:0}.bubble-markdown :deep(h1){font-size:.95rem;margin:.5rem 0 .3rem}.bubble-markdown :deep(h2){font-size:.88rem;margin:.5rem 0 .25rem}.bubble-markdown :deep(h3),.bubble-markdown :deep(h4){font-size:.82rem;margin:.45rem 0 .2rem}.bubble-markdown :deep(table){font-size:.72rem;margin:.45rem 0}.bubble-markdown :deep(th),.bubble-markdown :deep(td){padding:.3rem .4rem}.bubble.typing{padding:.65rem .8rem}.typing .dot{width:.38rem;height:.38rem;border-radius:50%;background:var(--aw-muted);animation:typing-bounce 1.2s infinite ease-in-out}.typing .dot:nth-child(2){animation-delay:.15s}.typing .dot:nth-child(3){animation-delay:.3s}@keyframes typing-bounce{0%,60%,100%{opacity:.35;transform:translateY(0)}30%{opacity:1;transform:translateY(-.18rem)}}.intent{align-self:flex-end;opacity:.65;text-transform:uppercase;font-size:.55rem}.trace{margin:.3rem 0;font-size:.68rem;color:var(--aw-muted)}.trace summary{cursor:pointer}.trace div{padding:.1rem .3rem}.citations{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.4rem}.warning{margin-top:.35rem;padding:.4rem;border-radius:6px;background:#fff7e6;color:#8a5a00;font-size:.68rem}
</style>
