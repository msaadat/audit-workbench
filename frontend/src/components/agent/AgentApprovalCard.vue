<script setup lang="ts">
import { reactive, ref } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import type { AgentApproval, AgentDecision } from '../../types'

// One editable approval batch. Every item starts approved (the agent only
// proposes what passed local validation); the auditor can reject items or
// open an item's exact spec and edit it as JSON before applying the batch.
const props = defineProps<{ approval: AgentApproval; busy: boolean }>()
const emit = defineEmits<{ decide: [AgentDecision[]] }>()

const kindLabel: Record<string, string> = {
  join: 'Proposed joins',
  rules: 'Proposed validation rules',
  tests: 'Suggested tests',
  context: 'Planning context updates',
  apm: 'Audit planning memorandum',
  rcm: 'Risk and control matrix',
  work_program: 'Audit program',
}

interface ItemState {
  action: 'approve' | 'reject'
  editing: boolean
  draft: string
  error: string | null
  edited: boolean
}

const items = reactive<Record<string, ItemState>>({})
for (const item of props.approval.items) {
  items[item.id] = {
    action: 'approve',
    editing: false,
    draft: JSON.stringify(item.spec, null, 2),
    error: null,
    edited: false,
  }
}
const submitting = ref(false)

function toggle(itemId: string) {
  const state = items[itemId]
  state.action = state.action === 'approve' ? 'reject' : 'approve'
}

function commitEdit(itemId: string) {
  const state = items[itemId]
  try {
    JSON.parse(state.draft)
    state.error = null
    state.edited = true
    state.editing = false
    state.action = 'approve'
  } catch (error) {
    state.error = `Not valid JSON: ${error}`
  }
}

function apply() {
  const decisions: AgentDecision[] = props.approval.items.map((item) => {
    const state = items[item.id]
    if (state.action === 'reject') return { item_id: item.id, action: 'reject' }
    if (state.edited)
      return { item_id: item.id, action: 'edit', spec: JSON.parse(state.draft) }
    return { item_id: item.id, action: 'approve' }
  })
  submitting.value = true
  emit('decide', decisions)
}
</script>

<template>
  <div class="approval">
    <div class="head">
      <i class="pi pi-pause-circle" />
      <strong>{{ kindLabel[approval.kind] ?? approval.kind }}</strong>
      <span class="grow" />
      <Tag :value="`${approval.items.length}`" severity="warn" />
    </div>
    <p class="hint">
      Review each proposal — approve, reject, or edit its exact configuration.
      Nothing is applied until you decide.
    </p>

    <div
      v-for="item in approval.items"
      :key="item.id"
      class="item"
      :class="{ rejected: items[item.id].action === 'reject' }"
    >
      <div class="item-head">
        <span class="item-title">{{ item.title }}</span>
        <span class="grow" />
        <Button
          :icon="items[item.id].action === 'approve' ? 'pi pi-check' : 'pi pi-times'"
          :severity="items[item.id].action === 'approve' ? 'success' : 'danger'"
          :label="items[item.id].action === 'approve' ? 'Approve' : 'Rejected'"
          size="small"
          text
          @click="toggle(item.id)"
        />
        <Button
          icon="pi pi-pencil"
          size="small"
          text
          severity="secondary"
          v-tooltip.left="'Edit the exact spec'"
          @click="items[item.id].editing = !items[item.id].editing"
        />
      </div>
      <small v-if="item.rationale" class="rationale">{{ item.rationale }}</small>
      <small v-if="items[item.id].edited" class="edited">
        <i class="pi pi-pencil" /> edited — the modified spec will be applied
      </small>
      <div v-if="items[item.id].editing" class="editor">
        <Textarea v-model="items[item.id].draft" rows="6" autoResize spellcheck="false" />
        <small v-if="items[item.id].error" class="error">{{ items[item.id].error }}</small>
        <div class="editor-actions">
          <Button label="Use edited spec" size="small" @click="commitEdit(item.id)" />
          <Button
            label="Discard"
            size="small"
            text
            severity="secondary"
            @click="items[item.id].editing = false"
          />
        </div>
      </div>
    </div>

    <Button
      class="apply"
      label="Apply decisions"
      icon="pi pi-play"
      :loading="busy || submitting"
      @click="apply"
    />
  </div>
</template>

<style scoped>
.approval {
  border: 1px solid #f0d9a8;
  background: #fffaf0;
  border-radius: 8px;
  padding: 0.7rem 0.8rem;
}
.head { display: flex; align-items: center; gap: 0.5rem; }
.head i { color: var(--p-amber-500); }
.head strong { font-size: 0.88rem; }
.grow { flex: 1; }
.hint { margin: 0.3rem 0 0.6rem; font-size: 0.74rem; color: var(--p-surface-500); }
.item {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 7px;
  padding: 0.45rem 0.6rem;
  margin-bottom: 0.45rem;
}
.item.rejected { opacity: 0.6; }
.item-head { display: flex; align-items: center; gap: 0.3rem; }
.item-title { font-size: 0.82rem; font-weight: 600; line-height: 1.3; }
.rationale { display: block; color: var(--p-surface-500); font-size: 0.72rem; margin-top: 0.15rem; }
.edited { display: block; color: var(--p-primary-600); font-size: 0.72rem; margin-top: 0.15rem; }
.editor { margin-top: 0.45rem; }
.editor :deep(textarea) { width: 100%; font-family: ui-monospace, monospace; font-size: 0.75rem; }
.editor-actions { display: flex; gap: 0.4rem; margin-top: 0.35rem; }
.error { color: var(--p-red-500); font-size: 0.72rem; }
.apply { width: 100%; margin-top: 0.25rem; }
</style>
