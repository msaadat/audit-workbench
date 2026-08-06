<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'

import { api } from '../../api'
import type { AuditDocument } from '../../types'

const props = defineProps<{
  visible: boolean
  workspaceId: string
  selectedIds: string[]
}>()
const emit = defineEmits<{
  'update:visible': [boolean]
  apply: [AuditDocument[]]
}>()

const documents = ref<AuditDocument[]>([])
const draftIds = ref<string[]>([])
const search = ref('')
const loading = ref(false)

const filtered = computed(() => {
  const term = search.value.trim().toLowerCase()
  return documents.value.filter((doc) => !term || `${doc.title} ${doc.source} ${doc.category}`.toLowerCase().includes(term))
})

watch(() => props.visible, async (visible) => {
  if (!visible) return
  draftIds.value = [...props.selectedIds]
  search.value = ''
  loading.value = true
  try {
    documents.value = (await api.get<{ items: AuditDocument[] }>(
      `/api/workspaces/${props.workspaceId}/documents`,
    )).items
    draftIds.value = draftIds.value.filter((id) => documents.value.some((doc) => doc.id === id))
  } finally {
    loading.value = false
  }
})

function apply() {
  emit('apply', documents.value.filter((doc) => draftIds.value.includes(doc.id)))
  emit('update:visible', false)
}
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    header="Add document context"
    :style="{ width: 'min(42rem, 94vw)' }"
    @update:visible="emit('update:visible', $event)"
  >
    <InputText v-model="search" class="search" placeholder="Search documents" />
    <div class="document-list" :class="{ loading }">
      <label v-for="doc in filtered" :key="doc.id" class="document-card">
        <Checkbox v-model="draftIds" :value="doc.id" />
        <span class="file-icon"><i class="pi pi-file" /></span>
        <span class="identity">
          <strong>{{ doc.title }}</strong>
          <small>{{ doc.source }} · {{ doc.pages || 0 }} page{{ doc.pages === 1 ? '' : 's' }}</small>
        </span>
        <Tag :value="doc.text_state.replace('_', ' ')" :severity="doc.text_state === 'extracted' ? 'success' : doc.text_state === 'failed' ? 'danger' : 'warn'" />
      </label>
      <p v-if="!loading && !filtered.length" class="empty">No documents match your search.</p>
    </div>
    <p class="budget-note"><i class="pi pi-info-circle" /> Whole documents are attached. If they exceed the safe context budget, the answer will identify any trimmed pages.</p>

    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="emit('update:visible', false)" />
      <Button :label="`Use ${draftIds.length} document${draftIds.length === 1 ? '' : 's'}`" icon="pi pi-paperclip" @click="apply" />
    </template>
  </Dialog>
</template>

<style scoped>
.search { width:100%; margin:0 0 .65rem; }
.document-list { display:grid; gap:.55rem; max-height:24rem; overflow:auto; padding:.1rem; }.document-list.loading { opacity:.55; }
.document-card { display:flex; align-items:center; gap:.65rem; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); cursor:pointer; background:var(--aw-panel); }.document-card:hover { border-color:var(--aw-teal); }
.file-icon { display:grid; place-items:center; width:2.15rem; height:2.15rem; border-radius:var(--aw-radius-control); color:var(--aw-info); background:var(--aw-info-soft); }.identity { display:grid; min-width:0; flex:1; gap:.12rem; }.identity strong,.identity small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.identity small,.empty,.budget-note { color:var(--aw-muted); font-size:var(--aw-text-xs); }.empty { padding:2rem; text-align:center; }.budget-note { display:flex; gap:.4rem; margin:.75rem 0 0; }
</style>
