<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Textarea from 'primevue/textarea'
import type { AgentInteraction } from '../../types'
import { api } from '../../api'
import { diffLines, type DiffLine } from './textDiff'

const props = defineProps<{ interaction: AgentInteraction; busy: boolean; workspaceId: string; runId: string }>()
const emit = defineEmits<{ respond: [Record<string, unknown>]; feedback: [string] }>()
const text = ref('')
const selected = ref('')
const editedArgs = ref('')
const feedbackOpen = ref(false)
const feedbackNote = ref('')
const detail = ref<unknown>(null)

onMounted(async () => {
  const ref = (props.interaction.payload.comparison_sidecar || props.interaction.payload.sidecar) as { sha1?: string } | undefined
  if (!ref?.sha1) return
  try {
    detail.value = await api.get(`/api/workspaces/${props.workspaceId}/agent/runs/${props.runId}/sidecars/${ref.sha1}`)
  } catch {
    detail.value = null
  }
})

const title = computed(() => ({
  clarification: 'Information needed', target_choice: 'Choose a target',
  confirmation: 'Destructive action', proposal_approval: 'Review proposal',
  conflict_resolution: 'Resolve a conflict', stage_review: 'Next stage',
}[props.interaction.type]))


const resolved = computed(() => props.interaction.status !== 'pending')
const resolvedSummary = computed(() => {
  const response = props.interaction.response
  if (!response) return 'Resolved'
  if (typeof response.text === 'string' && response.text.trim()) return `Answered: ${response.text.trim()}`
  const choiceValue = typeof response.choice === 'string' && response.choice
    ? response.choice
    : typeof response.option_id === 'string' && response.option_id
      ? response.option_id
      : ''
  if (choiceValue) {
    const option = props.interaction.options?.find(item => (item.ref || item.id || item.value) === choiceValue)
    return `Selected: ${option?.title || option?.label || choiceValue}`
  }
  if (typeof response.decision === 'string') {
    return { approve: 'Approved', reject: 'Rejected', skip: 'Skipped', retry: 'Retried' }[response.decision] ?? `Decision: ${response.decision}`
  }
  return 'Resolved'
})

function choose(option: AgentInteraction['options'][number]) {
  selected.value = option.ref || option.id || option.value || ''
}

function answerClarificationOption(option: AgentInteraction['options'][number]) {
  const optionId = option.ref || option.id || option.value || ''
  const label = option.title || option.label || optionId
  emit('respond', { choice: optionId, option_id: optionId, text: label })
}

function approve() {
  const response: Record<string, unknown> = { decision: 'approve' }
  if (editedArgs.value.trim()) {
    try { response.args = JSON.parse(editedArgs.value) } catch { return }
  }
  emit('respond', response)
}

function submitProposalFeedback() {
  const note = feedbackNote.value.trim()
  if (!note) return
  feedbackOpen.value = false
  feedbackNote.value = ''
  const preview = props.interaction.payload?.preview as Record<string, unknown> | undefined
  const target = typeof preview?.target === 'string' ? preview.target : ''
  const subject = typeof preview?.title === 'string' && preview.title
    ? preview.title
    : typeof preview?.action_type === 'string' && preview.action_type
      ? preview.action_type
      : props.interaction.prompt
  const contextParts = [
    `Proposal rejected (run ${props.runId})`,
    subject,
    target && target !== subject ? target : '',
  ].filter(Boolean)
  emit('respond', { decision: 'reject', reason: note })
  emit('feedback', `[${contextParts.join(' · ')}]: ${note}`)
}

const proposalDiff = computed<DiffLine[]>(() => {
  const preview = props.interaction.payload?.preview as Record<string, unknown> | undefined
  const previewArgs = preview && typeof preview.args === 'object' && preview.args !== null
    ? (preview.args as Record<string, unknown>)
    : undefined
  const sidecarDetail = detail.value && typeof detail.value === 'object'
    ? (detail.value as Record<string, unknown>)
    : undefined
  const sidecarArgs = sidecarDetail && typeof sidecarDetail.args === 'object' && sidecarDetail.args !== null
    ? (sidecarDetail.args as Record<string, unknown>)
    : undefined
  const source = [previewArgs, sidecarArgs, preview].find(
    (obj): obj is Record<string, unknown> => Boolean(
      obj
      && (typeof obj.old_str === 'string' || typeof obj.before === 'string')
      && (typeof obj.new_str === 'string' || typeof obj.after === 'string'),
    ),
  )
  if (!source) return []
  const before = typeof source.old_str === 'string' ? source.old_str : String(source.before)
  const after = typeof source.new_str === 'string' ? source.new_str : String(source.after)
  return diffLines(before, after)
})

function readablePreview(value: unknown) {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object') return String(value ?? '')
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => ['string', 'number', 'boolean'].includes(typeof item))
    .slice(0, 8)
  return entries.length
    ? entries.map(([key, item]) => `${key.replaceAll('_', ' ')}: ${item}`).join('\n')
    : 'Review the proposed action before approving.'
}
</script>

<template>
  <div class="interaction" :class="[interaction.type, { resolved }]">
    <strong>{{ title }}</strong>
    <p>{{ interaction.prompt }}</p>
    <small v-if="interaction.policy_reason">{{ interaction.policy_reason }}</small>
    <details v-if="detail && !resolved" class="technical"><summary>Technical comparison</summary><pre>{{ JSON.stringify(detail, null, 2) }}</pre></details>

    <div v-if="resolved" class="resolution"><i class="aw-icon aw-icon-circle-check" /> {{ resolvedSummary }}</div>

    <template v-else-if="interaction.type === 'clarification'">
      <div v-if="interaction.options?.length" class="clarification-options">
        <button
          v-for="option in interaction.options"
          :key="option.ref || option.id || option.value || option.label"
          type="button"
          class="choice"
          :disabled="busy"
          @click="answerClarificationOption(option)"
        >
          <strong>{{ option.title || option.label || option.id }}</strong>
          <small v-if="option.reason">{{ option.reason }}</small>
        </button>
      </div>
      <Textarea
        v-model="text"
        rows="2"
        autoResize
        :placeholder="interaction.options?.length ? 'Or write another answer…' : 'Provide the missing detail…'"
      />
      <Button label="Continue" size="small" :loading="busy" :disabled="!text.trim()" @click="emit('respond', { text })" />
    </template>

    <template v-else-if="interaction.type === 'target_choice'">
      <button
        v-for="option in interaction.options" :key="option.ref || option.id"
        class="choice" :class="{ selected: selected === (option.ref || option.id) }"
        @click="choose(option)"
      >
        <strong>{{ option.title || option.label || option.id }}</strong>
        <small>{{ option.reason }}<span v-if="option.score"> · {{ Math.round(option.score * 100) }}%</span></small>
      </button>
      <Button label="Use selected target" size="small" :loading="busy" :disabled="!selected" @click="emit('respond', { choice: selected })" />
    </template>

    <!-- Three answers, not free text: the run is waiting, and the decision is
         continue, skip this stage, or stop here. -->
    <template v-else-if="interaction.type === 'stage_review'">
      <div class="buttons">
        <Button label="Stop" severity="danger" outlined size="small" :disabled="busy" @click="emit('respond', { choice: 'stop' })" />
        <Button label="Skip this stage" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { choice: 'skip' })" />
        <Button label="Continue" size="small" :loading="busy" @click="emit('respond', { choice: 'continue' })" />
      </div>
    </template>

    <template v-else-if="interaction.type === 'proposal_approval'">
      <div v-if="proposalDiff.length" class="diff-view" role="region" aria-label="Proposed edit diff">
        <div v-for="(line, idx) in proposalDiff" :key="idx" class="diff-line" :class="line.kind">
          <span class="diff-mark" aria-hidden="true">{{ line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : line.kind === 'skip' ? '·' : ' ' }}</span>
          <span>{{ line.text }}</span>
        </div>
      </div>
      <div v-else-if="interaction.payload.preview" class="proposal-preview">{{ readablePreview(interaction.payload.preview) }}</div>
      <details class="technical"><summary>Advanced specification (JSON)</summary><Textarea v-model="editedArgs" rows="3" autoResize placeholder="Optional edited action arguments (JSON)" /></details>
      <form v-if="feedbackOpen" class="feedback-form" @submit.prevent="submitProposalFeedback">
        <Textarea v-model="feedbackNote" rows="2" autoResize placeholder="Tell the agent what to adjust…" :disabled="busy" />
        <div class="buttons">
          <Button label="Cancel" severity="secondary" outlined size="small" :disabled="busy" @click="feedbackOpen = false" />
          <Button type="submit" label="Reject & send feedback" severity="secondary" outlined size="small" :loading="busy" :disabled="!feedbackNote.trim()" />
        </div>
      </form>
      <div class="buttons">
        <Button label="Reject" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'reject' })" />
        <Button label="Request changes" severity="secondary" outlined size="small" :disabled="busy" @click="feedbackOpen = !feedbackOpen" />
        <Button label="Approve" size="small" :loading="busy" @click="approve" />
      </div>
    </template>

    <template v-else-if="interaction.type === 'confirmation'">
      <details v-if="interaction.payload.preview" class="technical"><summary>Removal details</summary><pre>{{ JSON.stringify(interaction.payload.preview, null, 2) }}</pre></details>
      <div class="buttons">
        <Button label="Keep it" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'reject' })" />
        <Button label="Confirm removal" severity="danger" outlined size="small" :loading="busy" @click="emit('respond', { decision: 'approve' })" />
      </div>
    </template>

    <template v-else>
      <details v-if="interaction.payload" class="technical"><summary>Conflict details</summary><pre>{{ JSON.stringify(interaction.payload, null, 2) }}</pre></details>
      <div class="buttons">
        <Button label="Keep current and skip" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'skip' })" />
        <Button label="Revalidate current version" size="small" :loading="busy" @click="emit('respond', { decision: 'retry' })" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.interaction { display: grid; gap: .6rem; padding: .75rem; border: 1px solid var(--aw-warn-line); background: var(--aw-warn-soft); border-radius: var(--aw-radius-control); }
.interaction.confirmation { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); }
.interaction.resolved { border-color: var(--aw-border); background: var(--aw-canvas); }
.resolution { display: flex; align-items: center; gap: .4rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.resolution i { color: var(--aw-teal); }
.interaction p, .interaction small { margin: 0; }
.interaction > small { color: var(--aw-muted); }
.clarification-options { display: grid; gap: .4rem; }
.choice { display: grid; text-align: left; gap: .15rem; padding: .55rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: white; cursor: pointer; }
.choice:hover:not(:disabled) { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.choice:disabled { opacity: .6; cursor: default; }
.choice.selected { border-color: var(--aw-teal); box-shadow: 0 0 0 1px var(--aw-teal); }
.choice small { color: var(--aw-muted); }
pre { max-height: 10rem; overflow: auto; margin: 0; padding: .5rem; border-radius: var(--aw-radius-control); background: rgba(255,255,255,.75); font-size: var(--aw-text-xs); white-space: pre-wrap; }
.technical summary { cursor: pointer; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.technical > :not(summary) { width: 100%; margin-top: .4rem; }
.proposal-preview { padding: .55rem; border-radius: var(--aw-radius-control); background: white; color: var(--aw-ink); font-size: var(--aw-text-sm); white-space: pre-wrap; }
.diff-view { max-height: 14rem; overflow: auto; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); font-size: var(--aw-text-xs); line-height: 1.45; }
.diff-line { display: grid; grid-template-columns: 1.1rem minmax(0, 1fr); gap: .3rem; padding: .1rem .45rem; white-space: pre-wrap; word-break: break-word; }
.diff-line.del { background: var(--aw-danger-soft); color: var(--aw-danger); }
.diff-line.add { background: var(--aw-teal-soft); color: var(--aw-ink-strong); }
.diff-line.skip { color: var(--aw-muted); font-style: italic; background: var(--aw-raised); }
.diff-mark { user-select: none; font-family: var(--aw-font-mono, ui-monospace, monospace); font-weight: 700; }
.feedback-form { display: grid; gap: .4rem; }
.feedback-form :deep(textarea) { width: 100%; }
.buttons { display: flex; justify-content: flex-end; gap: .45rem; }
.observation{display:grid;grid-template-columns:minmax(0,1fr) minmax(10rem,14rem);gap:.5rem;align-items:center;padding:.45rem;background:white;border-radius:var(--aw-radius-control)}.observation span{display:grid}.observation small{color:var(--aw-muted)}
</style>
