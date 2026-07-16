<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'

import type { AgentDecision, AssistantApprovalProjection, AssistantChat, AssistantChatMessage, AssistantInteractionProjection, AssistantRunProjection, AuditDocument, EvidenceRef } from '../../types'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'
import ChatArtifactCard from './ChatArtifactCard.vue'
import ChatRunCard from './ChatRunCard.vue'

const props = defineProps<{ workspaceId: string; chat: AssistantChat; documents: AuditDocument[] }>()
const emit = defineEmits<{
  shortcut: [string, string]
  retry: [AssistantChatMessage]
  changed: []
  respond: [string, AssistantInteractionProjection['interaction'], Record<string, unknown>]
  decide: [string, AssistantApprovalProjection['approval'], AgentDecision[]]
}>()
const scroller = ref<HTMLElement | null>(null)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
const shortcuts = [
  ['Full audit', 'full_audit_working_draft'], ['Planning', 'planning'],
  ['Data analysis', 'data_analysis'], ['Document testing', 'document_testing'], ['Report', 'report'],
]
const empty = computed(() => props.chat.messages.length === 0 && props.chat.runs.length === 0)

function nearBottom() {
  const node = scroller.value
  return !node || node.scrollHeight - node.scrollTop - node.clientHeight < 100
}
function scrollBottom() { nextTick(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight }) }
onMounted(scrollBottom)
watch(() => props.chat.transcript.length, () => { if (nearBottom()) scrollBottom() })

type TranscriptItem = AssistantChat['transcript'][number]
function isRun(item: TranscriptItem): item is AssistantRunProjection { return item.type === 'run' }
function isInteraction(item: TranscriptItem): item is AssistantInteractionProjection { return item.type === 'interaction' }
function isApproval(item: TranscriptItem): item is AssistantApprovalProjection { return item.type === 'approval' }
function openCitation(value: NonNullable<AssistantChatMessage['citations']>[number]) { anchor.value = value; anchorOpen.value = true }
function documentTitle(id: string) { return props.documents.find(item => item.id === id)?.title ?? 'Unavailable document' }
</script>

<template>
  <div ref="scroller" class="transcript">
    <div v-if="empty" class="empty-state">
      <span class="empty-icon"><i class="pi pi-sparkles" /></span>
      <strong>What should we work on?</strong>
      <p>Ask a question, or start with a guided audit workflow.</p>
      <div class="shortcuts">
        <Button v-for="([label, template]) in shortcuts" :key="template" :label="label" size="small" severity="secondary" outlined @click="emit('shortcut', label, template)" />
      </div>
    </div>

    <template v-for="item in chat.transcript" :key="item.id">
      <ChatRunCard v-if="isRun(item)" :workspaceId="workspaceId" :projection="item" @changed="emit('changed')" />
      <AgentInteractionCard v-else-if="isInteraction(item)" :interaction="item.interaction" :busy="false" :workspaceId="workspaceId" :runId="item.run_id" @respond="emit('respond', item.run_id, item.interaction, $event)" />
      <AgentApprovalCard v-else-if="isApproval(item)" :approval="item.approval" :busy="false" @decide="emit('decide', item.run_id, item.approval, $event)" />
      <div v-else class="message" :class="[item.role, item.kind, item.state]">
        <div class="bubble">
          <i v-if="item.state === 'pending'" class="pi pi-spin pi-spinner" />
          <i v-else-if="item.kind === 'error'" class="pi pi-exclamation-triangle" />
          <p>{{ item.content }}</p>
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
  </div>
  <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
</template>

<style scoped>
.transcript{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:.65rem;padding:.8rem .9rem}.empty-state{display:grid;justify-items:center;gap:.55rem;margin:auto;padding:1.5rem;text-align:center}.empty-icon{display:grid;place-items:center;width:3rem;height:3rem;border-radius:12px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:1.25rem}.empty-state p{margin:0;color:var(--aw-muted);font-size:.78rem}.shortcuts{display:flex;justify-content:center;flex-wrap:wrap;gap:.4rem;margin-top:.35rem}.message{max-width:92%}.message.user{align-self:flex-end}.message.assistant{align-self:flex-start}.bubble{position:relative;display:flex;gap:.4rem;padding:.55rem .7rem;border-radius:10px;background:var(--p-surface-100);font-size:.79rem;line-height:1.4}.user .bubble{background:var(--aw-teal);color:white;border-bottom-right-radius:3px}.assistant .bubble{background:var(--p-surface-100);border-bottom-left-radius:3px}.clarification .bubble{background:#fff7e6;border:1px solid #f0d9a8}.error .bubble,.failed .bubble{background:var(--p-red-50);color:var(--p-red-700)}.bubble p{margin:0;white-space:pre-wrap}.intent{align-self:flex-end;opacity:.65;text-transform:uppercase;font-size:.55rem}.trace{margin:.3rem 0;font-size:.68rem;color:var(--aw-muted)}.trace div{padding:.1rem .3rem}.citations{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.4rem}.warning{margin-top:.35rem;padding:.4rem;border-radius:6px;background:#fff7e6;color:#8a5a00;font-size:.68rem}
</style>
