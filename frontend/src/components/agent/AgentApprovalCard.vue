<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import type { AgentApproval, AgentDecision } from '../../types'
import { diffLines, type DiffLine } from './textDiff'

// One editable approval batch. Every item starts approved (the agent only
// proposes what passed local validation); the auditor can reject items, request
// changes with feedback, or open an item's exact spec to edit as JSON.
const props = defineProps<{ approval: AgentApproval; busy: boolean; runId?: string }>()
const emit = defineEmits<{ decide: [AgentDecision[]]; feedback: [string] }>()

const kindLabel: Record<string, string> = {
  join: 'Proposed joins',
  rules: 'Proposed validation rules',
  tests: 'Suggested tests',
  context: 'Planning context updates',
  apm: 'Audit planning memorandum',
  rcm: 'Risk and control matrix',
  rcm_revision: 'Matrix revision',
  apm_revision: 'Memorandum revision',
  work_program: 'Audit program',
}

// A revision proposes one decision per item. The decision is the headline,
// so it is drawn as a tag beside the title rather than buried in the spec.
const decisionLabel: Record<string, string> = {
  revise: 'Revise',
  merge: 'Merge',
  retire: 'Retire',
  add: 'Add',
}

// Fields a revision item carries that the summary grid must not dump: section
// bodies run to pages, and the change list is drawn as its own diff below.
const REVISION_DETAIL_KEYS = new Set(['markdown', 'before', 'before_fields', 'changes', 'row', 'reason', 'decision'])

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
const feedbackOpen = ref(false)
const feedbackNote = ref('')
// Reset the local loading state when the parent's busy flag clears, so a
// failed decide call doesn't leave the Apply button spinning forever.
watch(() => props.busy, (value) => { if (!value) submitting.value = false })

const resolved = computed(() => props.approval.status !== 'pending')
const resolvedSummary = computed(() => {
  const counts = { approved: 0, rejected: 0, edited: 0 }
  for (const item of props.approval.items) {
    if (item.decision && item.decision in counts) counts[item.decision as keyof typeof counts] += 1
  }
  const parts = Object.entries(counts).filter(([, value]) => value > 0).map(([key, value]) => `${value} ${key}`)
  return parts.length ? `Decided: ${parts.join(', ')}` : 'Resolved'
})

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

function submitFeedback() {
  const note = feedbackNote.value.trim()
  if (!note) return
  const decisions: AgentDecision[] = props.approval.items.map((item) => ({
    item_id: item.id,
    action: 'reject',
  }))
  const kind = kindLabel[props.approval.kind] ?? props.approval.kind
  const titles = props.approval.items.map(item => item.title).filter(Boolean)
  const titleSummary = titles.length > 3
    ? `${titles.slice(0, 3).join(', ')} (+${titles.length - 3} more)`
    : titles.join(', ')
  const contextParts = [
    `Changes requested on ${kind}`,
    props.runId ? `run ${props.runId}` : '',
    titleSummary,
  ].filter(Boolean)
  submitting.value = true
  feedbackOpen.value = false
  feedbackNote.value = ''
  emit('decide', decisions)
  emit('feedback', `[${contextParts.join(' · ')}]: ${note}`)
}

function revisionKind(): boolean {
  return props.approval.kind === 'rcm_revision' || props.approval.kind === 'apm_revision'
}

// The fields a revise decision rewrites, or an added row's fields, paired with
// any prior field value so the auditor reads a before -> after diff.
function revisionChanges(spec: Record<string, unknown>): Array<{ label: string; before?: string; value: string }> {
  const source = (spec.changes ?? spec.row) as Record<string, unknown> | undefined
  const beforeFields = (spec.before_fields && typeof spec.before_fields === 'object'
    ? spec.before_fields
    : {}) as Record<string, unknown>
  if (!source || typeof source !== 'object') return []
  return Object.entries(source)
    .filter(([key, value]) => key !== 'control_attributes' && typeof value === 'string' && value)
    .map(([key, value]) => {
      const prev = typeof beforeFields[key] === 'string' && beforeFields[key] !== value
        ? String(beforeFields[key])
        : undefined
      return { label: key.replaceAll('_', ' '), before: prev, value: String(value) }
    })
}

function sectionDiff(spec: Record<string, unknown>): DiffLine[] {
  if (props.approval.kind !== 'apm_revision') return []
  if (typeof spec.before !== 'string' || typeof spec.markdown !== 'string') return []
  if (!spec.before.trim()) return []
  return diffLines(spec.before, spec.markdown)
}

function codePreview(spec: Record<string, unknown>): string {
  if (typeof spec.code === 'string' && spec.code.trim()) return spec.code
  const nested = spec.spec as Record<string, unknown> | undefined
  if (nested && typeof nested.code === 'string' && nested.code.trim()) return nested.code
  return ''
}

function readableSpec(spec: Record<string, unknown>): Array<{ label: string; value: string }> {
  return Object.entries(spec)
    .filter(([key]) => !revisionKind() || !REVISION_DETAIL_KEYS.has(key))
    .filter(([, value]) => value === null || ['string', 'number', 'boolean'].includes(typeof value) || (Array.isArray(value) && value.every(item => ['string', 'number'].includes(typeof item))))
    .slice(0, 6)
    .map(([key, value]) => ({
      label: key.replaceAll('_', ' '),
      value: Array.isArray(value) ? value.join(', ') : value === null ? 'Not set' : String(value),
    }))
}
</script>

<template>
  <div class="approval" :class="{ resolved }">
    <div class="head">
      <i :class="resolved ? 'aw-icon aw-icon-circle-check' : 'aw-icon aw-icon-circle-pause'" />
      <strong>{{ kindLabel[approval.kind] ?? approval.kind }}</strong>
      <span class="grow" />
      <Tag v-if="!resolved" :value="`${approval.items.length}`" severity="warn" />
    </div>

    <template v-if="resolved">
      <p class="hint">{{ resolvedSummary }}</p>
      <ul class="decided">
        <li v-for="item in approval.items" :key="item.id">
          <i :class="item.decision === 'rejected' ? 'aw-icon aw-icon-x' : 'aw-icon aw-icon-check'" />
          {{ item.title }}<small v-if="item.decision"> · {{ item.decision }}</small>
        </li>
      </ul>
    </template>

    <template v-else>
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
        <Tag
          v-if="revisionKind() && decisionLabel[String(item.spec.decision)]"
          :value="decisionLabel[String(item.spec.decision)]"
          severity="secondary"
        />
        <span class="item-title">{{ item.title }}</span>
        <span class="grow" />
        <Button
          :icon="items[item.id].action === 'approve' ? 'aw-icon aw-icon-check' : 'aw-icon aw-icon-x'"
          :severity="items[item.id].action === 'approve' ? 'success' : 'danger'"
          :label="items[item.id].action === 'approve' ? 'Approve' : 'Rejected'"
          size="small"
          text
          @click="toggle(item.id)"
        />
        <Button
          icon="aw-icon aw-icon-pencil"
          size="small"
          text
          severity="secondary"
          v-tooltip.left="'Advanced specification'"
          @click="items[item.id].editing = !items[item.id].editing"
        />
      </div>
      <small v-if="item.rationale" class="rationale">{{ item.rationale }}</small>
      <dl v-if="readableSpec(item.spec).length" class="spec-summary">
        <div v-for="field in readableSpec(item.spec)" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div>
      </dl>
      <ul v-if="revisionKind() && revisionChanges(item.spec).length" class="changes">
        <li v-for="change in revisionChanges(item.spec)" :key="change.label">
          <span class="aw-label">{{ change.label }}</span>
          <del v-if="change.before" class="diff-del-inline">{{ change.before }}</del>
          <ins v-if="change.before" class="diff-add-inline">{{ change.value }}</ins>
          <template v-else>{{ change.value }}</template>
        </li>
      </ul>
      <div v-if="sectionDiff(item.spec).length" class="diff-view" role="region" aria-label="Proposed text diff">
        <div v-for="(line, idx) in sectionDiff(item.spec)" :key="idx" class="diff-line" :class="line.kind">
          <span class="diff-mark" aria-hidden="true">{{ line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : line.kind === 'skip' ? '·' : ' ' }}</span>
          <span>{{ line.text }}</span>
        </div>
      </div>
      <pre v-else-if="approval.kind === 'apm_revision' && item.spec.markdown" class="section-preview">{{ item.spec.markdown }}</pre>
      <pre v-else-if="codePreview(item.spec)" class="code-preview">{{ codePreview(item.spec) }}</pre>
      <small v-if="items[item.id].edited" class="edited">
        <i class="aw-icon aw-icon-pencil" /> edited — the modified spec will be applied
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

    <form v-if="feedbackOpen" class="feedback-form" @submit.prevent="submitFeedback">
      <Textarea
        v-model="feedbackNote"
        rows="2"
        autoResize
        placeholder="Tell the agent what to adjust — this rejects the batch and sends your feedback…"
        :disabled="busy || submitting"
      />
      <div class="feedback-actions">
        <Button label="Cancel" severity="secondary" outlined size="small" :disabled="busy || submitting" @click="feedbackOpen = false" />
        <Button type="submit" label="Reject & send feedback" severity="secondary" outlined size="small" :loading="busy || submitting" :disabled="!feedbackNote.trim()" />
      </div>
    </form>

    <div class="footer-actions">
      <Button
        label="Request changes"
        icon="aw-icon aw-icon-pencil"
        severity="secondary"
        outlined
        size="small"
        :disabled="busy || submitting"
        @click="feedbackOpen = !feedbackOpen"
      />
      <Button
        class="apply"
        label="Apply decisions"
        icon="aw-icon aw-icon-play"
        :loading="busy || submitting"
        @click="apply"
      />
    </div>
    </template>
  </div>
</template>

<style scoped>
.approval {
  border: 1px solid var(--aw-warn-line);
  background: var(--aw-warn-soft);
  border-radius: var(--aw-radius-control);
  padding: 0.7rem 0.8rem;
}
.approval.resolved { border-color: var(--aw-border); background: var(--aw-canvas); }
.approval.resolved .head i { color: var(--aw-teal); }
.decided { margin: 0.2rem 0 0; padding: 0; list-style: none; }
.decided li { display: flex; align-items: center; gap: 0.4rem; padding: 0.15rem 0; font-size: var(--aw-text-sm); }
.decided li i { font-size: var(--aw-text-2xs); color: var(--aw-muted); }
.decided li small { color: var(--aw-muted); }
.head { display: flex; align-items: center; gap: 0.5rem; }
.head i { color: var(--aw-warn); }
.head strong { font-size: var(--aw-text-base); }
.grow { flex: 1; }
.hint { margin: 0.3rem 0 0.6rem; font-size: var(--aw-text-xs); color: var(--aw-muted); }
.item {
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.45rem 0.6rem;
  margin-bottom: 0.45rem;
}
.item.rejected { opacity: 0.6; }
.item-head { display: flex; align-items: center; gap: 0.3rem; }
.item-title { font-size: var(--aw-text-sm); font-weight: 600; line-height: 1.3; }
.rationale { display: block; color: var(--aw-muted); font-size: var(--aw-text-xs); margin-top: 0.15rem; }
.spec-summary { display: grid; grid-template-columns: 1fr 1fr; gap: .25rem .6rem; margin: .45rem 0 0; padding: .45rem; border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.spec-summary div { min-width: 0; }
.spec-summary dt { color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700; text-transform: capitalize; }
.spec-summary dd { margin: .08rem 0 0; overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.edited { display: block; color: var(--aw-teal); font-size: var(--aw-text-xs); margin-top: 0.15rem; }
.changes { margin: .4rem 0 0; padding: 0; list-style: none; font-size: var(--aw-text-xs); }
.changes li { display: grid; gap: .15rem; padding: .2rem 0; }
.changes .aw-label { margin-right: .3rem; color: var(--aw-muted); }
.diff-del-inline { padding: .15rem .3rem; border-radius: var(--aw-radius-control); background: var(--aw-danger-soft); color: var(--aw-danger); text-decoration: line-through; }
.diff-add-inline { padding: .15rem .3rem; border-radius: var(--aw-radius-control); background: var(--aw-teal-soft); color: var(--aw-ink-strong); text-decoration: none; }
.diff-view {
  max-height: 14rem;
  margin: .45rem 0 0;
  overflow: auto;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
  font-size: var(--aw-text-xs);
  line-height: 1.45;
}
.diff-line { display: grid; grid-template-columns: 1.1rem minmax(0, 1fr); gap: .3rem; padding: .1rem .45rem; white-space: pre-wrap; word-break: break-word; }
.diff-line.del { background: var(--aw-danger-soft); color: var(--aw-danger); }
.diff-line.add { background: var(--aw-teal-soft); color: var(--aw-ink-strong); }
.diff-line.skip { color: var(--aw-muted); font-style: italic; background: var(--aw-raised); }
.diff-mark { user-select: none; font-family: var(--aw-font-mono, ui-monospace, monospace); font-weight: 700; }
.section-preview, .code-preview {
  max-height: 12rem;
  margin: .45rem 0 0;
  padding: .45rem;
  overflow: auto;
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
  color: var(--aw-ink);
  font-family: inherit;
  font-size: var(--aw-text-xs);
  white-space: pre-wrap;
}
.code-preview { font-family: var(--aw-font-mono, ui-monospace, monospace); }
.editor { margin-top: 0.45rem; }
.editor :deep(textarea) { width: 100%; font-family: ui-monospace, monospace; font-size: var(--aw-text-sm); }
.editor-actions { display: flex; gap: 0.4rem; margin-top: 0.35rem; }
.error { color: var(--aw-danger); font-size: var(--aw-text-xs); }
.feedback-form { display: grid; gap: .4rem; margin: .45rem 0; }
.feedback-form :deep(textarea) { width: 100%; }
.feedback-actions { display: flex; justify-content: flex-end; gap: .4rem; }
.footer-actions { display: flex; align-items: center; gap: .45rem; margin-top: 0.35rem; }
.apply { flex: 1; }
</style>
