<script setup lang="ts">
import { ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import { useAssistantChat } from '../../composables/useAssistantChat'
import type { AssistantArtifact, SavedAnalysis } from '../../types'
import ChartView from '../ChartView.vue'
import CodeEditor from '../CodeEditor.vue'
import PinDialog from '../PinDialog.vue'

const props = defineProps<{ workspaceId: string; artifact: AssistantArtifact }>()
const chat = useAssistantChat(props.workspaceId)
const toast = useToast()
const code = ref(props.artifact.code ?? '')
const saving = ref(false)
const rerunning = ref(false)
const pinOpen = ref(false)
const pinning = ref(false)

watch(() => props.artifact.code, value => { code.value = value ?? '' })

const severity: Record<string, 'success' | 'warn' | 'danger' | 'info'> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}

async function persistCode() {
  if (props.artifact.code === code.value) return props.artifact
  return chat.updateArtifact(props.artifact, { code: code.value })
}

async function rerun() {
  rerunning.value = true
  try {
    const saved = await persistCode() ?? props.artifact
    await chat.rerunArtifact(saved)
  } catch (error) { fail('Run failed', error) }
  finally { rerunning.value = false }
}

async function saveAnalysis() {
  saving.value = true
  try {
    const artifact = await persistCode() ?? props.artifact
    await api.post<SavedAnalysis>(`/api/workspaces/${props.workspaceId}/analyses`, {
      kind: artifact.kind, table: artifact.table, title: artifact.title,
      spec: artifact.kind === 'python' ? { code: artifact.code } : artifact.spec,
      viz: artifact.viz, source: 'ai',
    })
    toast.add({ severity: 'success', summary: 'Saved to analyses', life: 2200 })
  } catch (error) { fail('Save failed', error) }
  finally { saving.value = false }
}

async function pin({ title, note }: { title: string; note: string }) {
  pinning.value = true
  try {
    const artifact = await persistCode() ?? props.artifact
    await api.post(`/api/workspaces/${props.workspaceId}/tiles`, {
      kind: artifact.kind, table: artifact.table, title, note,
      spec: artifact.kind === 'python' ? { code: artifact.code } : artifact.spec,
      viz: artifact.viz,
    })
    pinOpen.value = false
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', life: 2200 })
  } catch (error) { fail('Pin failed', error) }
  finally { pinning.value = false }
}

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
</script>

<template>
  <article class="artifact-card">
    <header>
      <strong>{{ artifact.title }}</strong>
      <Tag v-if="artifact.verdict" :value="artifact.verdict_text || artifact.verdict" :severity="severity[artifact.verdict]" />
      <span class="grow" />
      <Button v-if="artifact.kind !== 'query'" icon="pi pi-save" text size="small" :loading="saving" v-tooltip.top="'Save to analyses'" @click="saveAnalysis" />
      <Button icon="pi pi-thumbtack" text size="small" severity="secondary" v-tooltip.top="'Pin to dashboard'" @click="pinOpen = true" />
    </header>
    <div v-if="artifact.stats?.length" class="stats">
      <span v-for="stat in artifact.stats" :key="stat.label"><small>{{ stat.label }}</small><strong>{{ stat.value }}</strong></span>
    </div>
    <div v-if="artifact.code !== undefined" class="code">
      <div><span><i class="pi pi-code" /> Editable Polars</span><Button label="Save & re-run" icon="pi pi-play" text size="small" :loading="rerunning" @click="rerun" /></div>
      <CodeEditor v-model="code" />
      <pre v-if="artifact.stdout">{{ artifact.stdout }}</pre>
    </div>
    <p v-if="artifact.last_error" class="error"><i class="pi pi-exclamation-triangle" /> {{ artifact.last_error }}</p>
    <ChartView v-if="artifact.frame" :frame="artifact.frame" :viz="artifact.viz" height="230px" />
  </article>
  <PinDialog v-model:visible="pinOpen" :defaultTitle="artifact.title" :saving="pinning" @pin="pin" />
</template>

<style scoped>
.artifact-card{margin-top:.55rem;border:1px solid var(--aw-border);border-radius:9px;background:var(--p-surface-0);overflow:hidden}.artifact-card header{display:flex;align-items:center;gap:.4rem;padding:.5rem .65rem;border-bottom:1px solid var(--p-surface-100);font-size:.78rem}.grow{flex:1}.stats{display:flex;flex-wrap:wrap;gap:.4rem;padding:.55rem}.stats span{display:grid;gap:.1rem;padding:.35rem .5rem;border-radius:6px;background:var(--p-surface-50);font-size:.72rem}.stats small{color:var(--aw-muted)}.code>div{display:flex;align-items:center;justify-content:space-between;padding:.3rem .55rem;font-size:.72rem;color:var(--aw-muted)}.code pre{margin:0;padding:.5rem;white-space:pre-wrap;background:#101827;color:#dbeafe;font-size:.68rem}.error{margin:.45rem;color:var(--p-red-600);font-size:.72rem}
</style>
