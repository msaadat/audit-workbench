<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'

import { api } from '../../api'
import type { AnalysisMemo, SavedAnalysis, WorkspaceSummary } from '../../types'
import MemoView from './MemoView.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import { plural } from '../../format'

// The Summary screen is the memo: an auditor's account of the EDA performed,
// the exceptions noted, and what remains — with the results it cites rendered
// where it cites them.
//
// It is deliberately not a gallery of every procedure. That is the Procedures
// screen, and a wall of charts is a worse answer to "what did the analysis
// find" than four paragraphs that say so. Nothing is recomputed to draw this
// page except the handful of results the memo actually embeds.
const props = defineProps<{ workspace: WorkspaceSummary; analyses: SavedAnalysis[] }>()
const emit = defineEmits<{ open: [analysisId: string]; regenerate: [] }>()

const memo = ref<AnalysisMemo | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    memo.value = await api.get<AnalysisMemo>(
      `/api/workspaces/${props.workspace.id}/analyses/memo`,
    )
  } catch {
    memo.value = null
  } finally {
    loading.value = false
  }
}
watch(() => [props.workspace.id, props.analyses.length], () => void load(), { immediate: true })

const hasMemo = computed(() => Boolean(memo.value?.markdown?.trim()))
const written = computed(() => {
  const at = memo.value?.generated_at
  return at ? new Date(at).toLocaleString() : null
})
</script>

<template>
  <section class="analysis-summary">
    <template v-if="hasMemo">
      <!-- A stale memo is still shown: it is what the analysis concluded at the
           time, and hiding it would lose that. The banner is what stops it
           being read as current. -->
      <Message v-if="memo!.stale" severity="warn" :closable="false">
        The procedures have changed since this summary was written. Regenerate it
        to describe the current results.
      </Message>

      <header class="summary-head">
        <span class="muted">
          Written {{ written }}<span v-if="memo!.cited_analysis_ids.length">
            · cites {{ plural(memo!.cited_analysis_ids.length, 'result') }}</span>
        </span>
        <Button
          label="Regenerate"
          icon="pi pi-refresh"
          size="small"
          outlined
          @click="emit('regenerate')"
        />
      </header>

      <MemoView
        :workspace="workspace"
        :markdown="memo!.markdown"
        :analyses="analyses"
        @open="id => emit('open', id)"
      />
    </template>

    <UiEmptyState
      v-else-if="loading"
      icon="pi pi-hourglass"
      title="Loading the summary"
      description="Reading the analysis summary recorded for this engagement."
    />

    <UiEmptyState
      v-else
      icon="pi pi-file-edit"
      title="No analysis summary yet"
      description="Once the procedures have run, the assistant can write up what the analysis found — the population, the exceptions, and the work still outstanding."
    >
      <Button label="Write the summary" icon="pi pi-sparkles" @click="emit('regenerate')" />
    </UiEmptyState>
  </section>
</template>

<style scoped>
.analysis-summary { display: flex; flex-direction: column; gap: var(--aw-space-4); }
.summary-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aw-space-3);
  flex-wrap: wrap;
}
.muted { color: var(--aw-muted); font-size: var(--aw-text-sm); }
</style>
