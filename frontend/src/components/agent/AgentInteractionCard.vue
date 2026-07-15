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
</script>

<template>
  <div class="interaction" :class="interaction.type">
    <strong>{{ title }}</strong>
    <p>{{ interaction.prompt }}</p>
    <small v-if="interaction.policy_reason">{{ interaction.policy_reason }}</small>
    <pre v-if="detail">{{ JSON.stringify(detail, null, 2) }}</pre>

    <template v-if="interaction.type === 'clarification'">
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
      <pre v-if="interaction.payload.preview">{{ JSON.stringify(interaction.payload.preview, null, 2) }}</pre>
      <Textarea v-model="editedArgs" rows="3" autoResize placeholder="Optional edited action arguments (JSON)" />
      <div class="buttons">
        <Button label="Reject" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'reject' })" />
        <Button label="Approve" size="small" :loading="busy" @click="approve" />
      </div>
    </template>

    <template v-else-if="interaction.type === 'confirmation'">
      <pre v-if="interaction.payload.preview">{{ JSON.stringify(interaction.payload.preview, null, 2) }}</pre>
      <div class="buttons">
        <Button label="Keep it" severity="secondary" outlined size="small" :disabled="busy" @click="emit('respond', { decision: 'reject' })" />
        <Button label="Confirm removal" severity="danger" size="small" :loading="busy" @click="emit('respond', { decision: 'approve' })" />
      </div>
    </template>

    <template v-else>
      <pre v-if="interaction.payload">{{ JSON.stringify(interaction.payload, null, 2) }}</pre>
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
.interaction p, .interaction small { margin: 0; }
.interaction > small { color: var(--p-text-muted-color); }
.choice { display: grid; text-align: left; gap: .15rem; padding: .55rem; border: 1px solid var(--aw-border, #d5dde7); border-radius: .4rem; background: white; cursor: pointer; }
.choice.selected { border-color: var(--aw-teal, #0b625c); box-shadow: 0 0 0 1px var(--aw-teal, #0b625c); }
.choice small { color: var(--p-text-muted-color); }
pre { max-height: 10rem; overflow: auto; margin: 0; padding: .5rem; border-radius: .35rem; background: rgba(255,255,255,.75); font-size: .7rem; white-space: pre-wrap; }
.buttons { display: flex; justify-content: flex-end; gap: .45rem; }
</style>
