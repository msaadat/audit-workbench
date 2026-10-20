<script setup lang="ts">
import { computed } from 'vue'

import type { AnalysisDetail, AnalysisExceptions, SavedAnalysis } from '../../types'
import ChartView from '../ChartView.vue'
import FrameTable from '../FrameTable.vue'
import ExhibitTable from './ExhibitTable.vue'
import type { Embed } from './memoExhibits'
import { testedColumns } from './memoExhibits'

/**
 * What one exhibit shows: the flagged rows, a chart, a result table or its
 * figures, as the embed asked for, and the caption the memo wrote for it.
 * The data is fetched by the memo, once per cited result, and handed down.
 */
const props = defineProps<{
  embed: Embed
  number: number
  analysis?: SavedAnalysis
  detail?: AnalysisDetail
  flagged?: AnalysisExceptions
  failed: boolean
}>()

const tested = computed(() =>
  props.flagged?.frame ? testedColumns(props.analysis, props.flagged.frame.columns) : [])
const stats = computed(() => props.analysis?.last_result?.stats ?? [])
</script>

<template>
  <div class="exhibit-body">
    <div v-if="failed" class="exhibit-state error">
      <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" /> Could not load this result
    </div>

    <dl v-else-if="embed.as === 'stats'" class="exhibit-stats">
      <div v-for="stat in stats" :key="stat.label">
        <dt>{{ stat.label }}</dt>
        <dd>{{ stat.value }}</dd>
      </div>
    </dl>

    <template v-else-if="embed.as === 'exception_table'">
      <ExhibitTable v-if="flagged?.frame" :frame="flagged.frame" :tested="tested" />
      <p v-else-if="flagged" class="exhibit-state">This result recorded no flagged rows.</p>
      <div v-else class="exhibit-state"><i class="aw-icon aw-icon-spin aw-icon-loader-circle" aria-hidden="true" /></div>
    </template>

    <template v-else>
      <ChartView
        v-if="embed.as === 'chart' && detail?.frame"
        :frame="detail.frame"
        :viz="detail.viz"
        height="240px"
      />
      <FrameTable v-else-if="detail?.frame" :frame="detail.frame" scrollHeight="260px" />
      <div v-else class="exhibit-state"><i class="aw-icon aw-icon-spin aw-icon-loader-circle" aria-hidden="true" /></div>
    </template>

    <figcaption v-if="embed.caption" class="exhibit-caption">
      <span>Exhibit {{ number }}. {{ embed.caption }}</span>
      <span v-if="tested.length && flagged?.frame" class="exhibit-caption-note">
        The tested columns first{{ flagged.frame.columns.length > tested.length ? ` · ${flagged.frame.columns.length - tested.length} more columns` : '' }}
      </span>
    </figcaption>
  </div>
</template>

<style scoped>
.exhibit-state {
  display: flex; align-items: center; justify-content: center; gap: .4rem;
  min-height: 5rem; margin: 0;
  color: var(--aw-muted); font-family: var(--aw-font-sans); font-size: var(--aw-text-sm);
}
.exhibit-state.error { color: var(--aw-danger); }
.exhibit-stats { display: flex; flex-wrap: wrap; gap: var(--aw-space-4); margin: 0; padding: .75rem .875rem; font-family: var(--aw-font-sans); }
.exhibit-stats dt { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.exhibit-stats dd { margin: 0; font-size: var(--aw-text-md); font-weight: 600; }
.exhibit-caption {
  display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: .25rem 1rem;
  padding: .5rem .875rem;
  border-top: 1px solid var(--aw-border);
  color: var(--aw-ink-soft); font-family: var(--aw-font-serif); font-size: var(--aw-text-sm); font-style: italic;
}
.exhibit-caption-note { color: var(--aw-muted); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); font-style: normal; }
</style>
