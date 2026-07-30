<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Textarea from 'primevue/textarea'
import type { AgentInteraction } from '../../types'
import { api } from '../../api'

const props = defineProps<{ interaction: AgentInteraction; busy: boolean; workspaceId: string; runId: string }>()
const emit = defineEmits<{ respond: [Record<string, unknown>] }>()
const text = ref('')
const selected = ref('')
const editedArgs = ref('')
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
  conflict_resolution: 'Resolve a conflict',
}[props.interaction.type]))


const resolved = computed(() => props.interaction.status !== 'pending')
const resolvedSummary = computed(() => {
  const response = props.interaction.response
  if (!response) return 'Resolved'
  if (typeof response.text === 'string' && response.text.trim()) return `Answered: ${response.text.trim()}`
  if (typeof response.choice === 'string' && response.choice) {
    const option = props.interaction.options?.find(item => (item.ref || item.id || item.value) === response.choice)
    return `Selected: ${option?.title || option?.label || response.choice}`
  }
  if (typeof response.decision === 'string') {
    return { approve: 'Approved', reject: 'Rejected', skip: 'Skipped', retry: 'Retried' }[response.decision] ?? `Decision: ${response.decision}`
  }
  return 'Resolved'
})

function choose(option: AgentInteraction['options'][number]) {
  selected.value = option.ref || option.id || option.value || ''
}

function approve() {
  const response: Record<string, unknown> = { decision: 'approve' }
  if (editedArgs.value.trim()) {
    try { response.args = JSON.parse(editedArgs.value) } catch { return }
  }
  emit('respond', response)
}

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

    <div v-if="resolved" class="resolution"><i class="pi pi-check-circle" /> {{ resolvedSummary }}</div>

    <template v-else-if="interaction.type === 'clarification'">
      <Textarea v-model="text" rows="2" autoResize placeholder="Provide the missing detail…" />
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

    <template v-else-if="interaction.type === 'proposal_approval'">
      <div v-if="interaction.payload.preview" class="proposal-preview">{{ readablePreview(interaction.payload.preview) }}</div>
      <details class="technical"><summary>Advanced specification</summary><Textarea v-model="editedArgs" rows="3" autoResize placeholder="Optional edited action arguments (JSON)" /></details>
      <div class="buttons">
        <Button label="Reject" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'reject' })" />
        <Button label="Approve" size="small" :loading="busy" @click="approve" />
      </div>
    </template>

    <template v-else-if="interaction.type === 'confirmation'">
      <details v-if="interaction.payload.preview" class="technical"><summary>Removal details</summary><pre>{{ JSON.stringify(interaction.payload.preview, null, 2) }}</pre></details>
      <div class="buttons">
        <Button label="Keep it" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'reject' })" />
        <Button label="Confirm removal" severity="danger" size="small" :loading="busy" @click="emit('respond', { decision: 'approve' })" />
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
.interaction { display: grid; gap: .6rem; padding: .75rem; border: 1px solid var(--p-amber-300); background: var(--p-amber-50); border-radius: .55rem; }
.interaction.confirmation { border-color: var(--p-red-300); background: var(--p-red-50); }
.interaction.resolved { border-color: var(--aw-border, #d5dde7); background: var(--p-surface-50); }
.resolution { display: flex; align-items: center; gap: .4rem; color: var(--aw-muted); font-size: .75rem; }
.resolution i { color: var(--aw-teal, #0b625c); }
.interaction p, .interaction small { margin: 0; }
.interaction > small { color: var(--p-text-muted-color); }
.choice { display: grid; text-align: left; gap: .15rem; padding: .55rem; border: 1px solid var(--aw-border, #d5dde7); border-radius: .4rem; background: white; cursor: pointer; }
.choice.selected { border-color: var(--aw-teal, #0b625c); box-shadow: 0 0 0 1px var(--aw-teal, #0b625c); }
.choice small { color: var(--p-text-muted-color); }
pre { max-height: 10rem; overflow: auto; margin: 0; padding: .5rem; border-radius: .35rem; background: rgba(255,255,255,.75); font-size: .7rem; white-space: pre-wrap; }
.technical summary { cursor: pointer; color: var(--aw-muted); font-size: .72rem; font-weight: 700; }
.technical > :not(summary) { width: 100%; margin-top: .4rem; }
.proposal-preview { padding: .55rem; border-radius: .4rem; background: white; color: var(--aw-ink); font-size: .75rem; white-space: pre-wrap; }
.buttons { display: flex; justify-content: flex-end; gap: .45rem; }
.observation{display:grid;grid-template-columns:minmax(0,1fr) minmax(10rem,14rem);gap:.5rem;align-items:center;padding:.45rem;background:white;border-radius:.4rem}.observation span{display:grid}.observation small{color:var(--p-text-muted-color)}
</style>
