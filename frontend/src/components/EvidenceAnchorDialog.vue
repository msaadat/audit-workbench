<script setup lang="ts">
import { computed } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import type { AuditDocument, EvidenceRef } from '../types'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import UiAdvancedSection from './ui/UiAdvancedSection.vue'

const props = defineProps<{
  modelValue: boolean
  anchor: EvidenceRef | null
  /**
   * The document catalogue, when the opener has one. An anchor names its
   * source by id; without the catalogue this dialog can only show the id
   * back, which is the last screen of a chain that spent every earlier step
   * naming things the auditor recognises.
   */
  documents?: AuditDocument[]
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const nav = useWorkspaceNav()
const visible = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })

const sourceLabel = computed(() => {
  const anchor = props.anchor
  if (!anchor) return ''
  if (anchor.source_kind === 'document') {
    const found = (props.documents ?? []).find(item => item.id === anchor.source_id)
    if (found) return found.source || found.title
  }
  return `${anchor.source_kind} · ${anchor.source_id}`
})

async function openSource() {
  if (!props.anchor) return
  if (props.anchor.source_kind === 'document') {
    await nav.replace('documents', { doc: props.anchor.source_id, page: props.anchor.page || 1 })
  } else if (props.anchor.source_kind === 'doctest') {
    await nav.replace('doc-tests', { test: props.anchor.source_id, item: props.anchor.item_id || undefined })
  } else return
  visible.value = false
}

async function copyCitation() {
  if (!props.anchor) return
  const page = props.anchor.page ? `, page ${props.anchor.page}` : ''
  await navigator.clipboard.writeText(`${props.anchor.source_kind}:${props.anchor.source_id}${page} [${props.anchor.source_sha1 || 'legacy'}]`)
}
</script>

<template>
  <Dialog v-model:visible="visible" modal header="Evidence source" :style="{ width: 'min(52rem, 94vw)' }">
    <div v-if="anchor" class="anchor-body">
      <div class="anchor-facts">
        <span>
          <small>Source</small>
          <strong>{{ sourceLabel }}</strong>
          <!-- The id stays, one line down: it identifies the anchor, but it
               is not what the source is called. -->
          <code class="source-id">{{ anchor.source_kind }}:{{ anchor.source_id }}</code>
        </span>
        <span><small>Page</small><strong>{{ anchor.page || '—' }}</strong></span>
      </div>
      <div class="excerpt">
        <small>Anchored excerpt</small>
        <blockquote>{{ anchor.excerpt || 'No excerpt was retained for this legacy or non-document reference.' }}</blockquote>
      </div>
      <UiAdvancedSection title="Technical details" description="Immutable source and excerpt identifiers"><dl class="technical"><div><dt>Source hash</dt><dd><code>{{ anchor.source_sha1 || 'Legacy reference' }}</code></dd></div><div v-if="anchor.excerpt_hash"><dt>Excerpt hash</dt><dd><code>{{ anchor.excerpt_hash }}</code></dd></div></dl></UiAdvancedSection>
    </div>
    <template #footer>
      <Button label="Copy citation" icon="pi pi-copy" severity="secondary" @click="copyCitation" />
      <Button v-if="anchor && ['document', 'doctest'].includes(anchor.source_kind)" label="Open source" icon="pi pi-external-link" @click="openSource" />
    </template>
  </Dialog>
</template>

<style scoped>
.anchor-body { display: grid; gap: 1rem; }
.anchor-facts { display: grid; grid-template-columns: 1.4fr .5fr; gap: .75rem; }
.anchor-facts span, .excerpt { padding: .85rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
small { display: block; margin-bottom: .3rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
code { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); overflow-wrap: anywhere; }
blockquote { margin: .5rem 0; padding: .85rem 1rem; border-left: 3px solid var(--aw-teal); background: var(--aw-panel); white-space: pre-wrap; }
.technical { display:grid; gap:.45rem; margin:0 }.technical div { display:grid; grid-template-columns:7rem 1fr; gap:.6rem }.technical dt { color:var(--aw-muted); font-size:var(--aw-text-xs); font-weight:700 }.technical dd { margin:0; overflow-wrap:anywhere }
@media (max-width: 700px) { .anchor-facts { grid-template-columns: 1fr; } }
</style>
