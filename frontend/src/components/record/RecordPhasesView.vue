<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import SplitButton from 'primevue/splitbutton'
import type { MenuItem } from 'primevue/menuitem'

import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import { plural } from '../../format'
import type { EngagementOpenPoint, EngagementStage } from '../../types'
import UiStateIcon from '../ui/UiStateIcon.vue'
import type {
  RecordFact, RecordPhaseGroup, RecordRunNow, RecordStageItem, RecordStartRequest,
} from './recordViewModel'

/**
 * The record as phase tabs over one table.
 *
 * Each thing is said once. The tabs are both the overview of the engagement
 * and the way through it, so there is no second phase deck above them. The
 * "Now" row holds the two things a reader acts on — the review only a person
 * can do, and what the assistant is doing or should do next — so a live run is
 * not restated as a band and again on its row. The table states each work
 * product in one vocabulary: what it holds, and one status.
 */

const props = defineProps<{
  groups: RecordPhaseGroup[]
  starting: string
  /** The server's ranked open points: the first is what a person should do first. */
  openPoints: EngagementOpenPoint[]
  run: RecordRunNow | null
}>()

const emit = defineEmits<{
  start: [stage: EngagementStage, alternate?: RecordStartRequest]
  'open-point': [point: EngagementOpenPoint]
  watch: []
}>()

const nav = useWorkspaceNav()

/* --- which phase is on screen ------------------------------------------- */

/**
 * The phase the record opens on follows the work — the run in flight, else the
 * phase being worked, else one with a review — until the reader picks a tab.
 */
const defaultId = computed(() =>
  props.groups.find(g => g.live)?.id
  ?? props.groups.find(g => g.state === 'current')?.id
  ?? props.groups.find(g => g.openPoints > 0)?.id
  ?? props.groups[0]?.id
  ?? '',
)
const picked = ref('')
const activeGroup = computed(() =>
  props.groups.find(g => g.id === picked.value)
  ?? props.groups.find(g => g.id === defaultId.value)
  ?? props.groups[0]
  ?? null,
)

type TabMark = 'live' | 'done' | 'attention' | 'number'

function tabMark(group: RecordPhaseGroup): TabMark {
  if (group.live) return 'live'
  if (group.openPoints || group.items.some(item => item.state === 'attention')) return 'attention'
  if (group.total > 0 && group.held === group.total) return 'done'
  return 'number'
}

/** The tab's one line: the phase's size when settled, its fraction while worked. */
function tabLine(group: RecordPhaseGroup): string {
  if (group.state === 'current' || group.live) return `${group.held} of ${group.total} ready`
  return group.headline
}

/* --- Now ---------------------------------------------------------------- */

const needs = computed(() => props.openPoints[0] ?? null)
const moreNeeds = computed(() => Math.max(0, props.openPoints.length - 1))

/** The one stage the record proposes, when nothing is running. */
const nextItem = computed<RecordStageItem | null>(() => {
  if (props.run) return null
  for (const group of props.groups) {
    const lead = group.items.find(item => item.isLead)
    if (lead) return lead
  }
  return null
})
const nextPhase = computed(() =>
  props.groups.find(group => group.items.some(item => item === nextItem.value))?.title ?? '',
)

function startOptions(stage: EngagementStage): MenuItem[] {
  return (stage.start?.alternates ?? []).map(alternate => ({
    label: alternate.label,
    note: alternate.note,
    command: () => emit('start', stage, alternate),
  }))
}

/* --- the table ---------------------------------------------------------- */

function holdsFacts(item: RecordStageItem): RecordFact[] {
  return item.facts.filter(f => f.kind !== 'owed' && f.kind !== 'tool')
}
function toolFacts(item: RecordStageItem): RecordFact[] {
  return item.facts.filter(f => f.kind === 'tool' && f.to)
}

/**
 * A distribution drawn as one grey bar beside its words. Neutral on purpose:
 * a severity tier is a property of the plan, not something wrong with it, and
 * colouring it made a healthy matrix read as an alarm.
 */
function distribution(fact: RecordFact): Array<{ share: number; text: string }> {
  const parts = (fact.parts ?? []).map(part => ({ value: Number(part.figure), text: part.text }))
  const sum = parts.reduce((total, part) => total + (Number.isFinite(part.value) ? part.value : 0), 0)
  if (parts.length < 2 || sum <= 0) return []
  return parts
    .filter(part => part.value > 0)
    .map(part => ({ share: part.value / sum, text: part.text }))
}

function partsLabel(fact: RecordFact): string {
  return (fact.parts ?? []).map(part => `${part.figure} ${part.text}`).join(', ')
}

function statusLabel(item: RecordStageItem): string {
  if (item.live === 'running') return item.stage.held ? 'Running again' : 'Running'
  if (item.live === 'queued') return 'Queued'
  return ''
}
</script>

<template>
  <section class="phases-view" aria-label="Audit phases" data-testid="record-phases">
    <div class="tabs" role="tablist" aria-label="Engagement phases">
      <button
        v-for="group in groups"
        :key="group.id"
        type="button"
        role="tab"
        class="tab"
        :aria-selected="group.id === activeGroup?.id"
        :data-state="group.live ? 'live' : group.state"
        @click="picked = group.id"
      >
        <span class="tab-head">
          <i v-if="tabMark(group) === 'live'" class="mark aw-icon aw-icon-spin aw-icon-loader-circle" data-mark="live" aria-hidden="true" />
          <i v-else-if="tabMark(group) === 'attention'" class="mark aw-icon aw-icon-triangle-alert" data-mark="attention" aria-hidden="true" />
          <i v-else-if="tabMark(group) === 'done'" class="mark aw-icon aw-icon-circle-check" data-mark="done" aria-hidden="true" />
          <span v-else class="tab-num" aria-hidden="true">{{ group.index + 1 }}</span>
          <span class="tab-title">{{ group.title }}</span>
        </span>
        <span class="tab-line">
          <span>{{ tabLine(group) }}</span>
          <template v-if="group.openPoints">
            <span class="sep" aria-hidden="true">·</span>
            <span class="tab-need">{{ group.openPoints }} {{ group.openPoints === 1 ? 'needs' : 'need' }} you</span>
          </template>
        </span>
        <span class="tab-bar" aria-hidden="true"><i :style="{ width: `${Math.round(group.fill * 100)}%` }" /></span>
      </button>
    </div>

    <section v-if="needs || run || nextItem" class="now" aria-label="Now">
      <article v-if="needs" class="now-card" data-kind="needs">
        <i class="now-ico aw-icon aw-icon-triangle-alert" aria-hidden="true" />
        <div class="now-copy">
          <span class="now-label">Needs you</span>
          <span class="now-title">{{ needs.message }}</span>
          <span v-if="moreNeeds" class="now-detail">and {{ plural(moreNeeds, 'other review') }} after this</span>
        </div>
        <Button
          :label="needs.action || 'Open'"
          icon="aw-icon aw-icon-arrow-right"
          icon-pos="right"
          size="small"
          @click="emit('open-point', needs)"
        />
      </article>

      <article v-if="run" class="now-card" :data-kind="run.waiting ? 'waiting' : 'run'">
        <i
          class="now-ico aw-icon"
          :class="run.waiting ? 'aw-icon-circle-help' : 'aw-icon-spin aw-icon-loader-circle'"
          aria-hidden="true"
        />
        <div class="now-copy">
          <span class="now-label">{{ run.waiting ? run.status : 'Assistant is working' }}</span>
          <span class="now-title">{{ run.title }}</span>
          <span class="now-progress">
            <span
              v-if="run.fraction !== null"
              class="now-bar"
              role="progressbar"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="Math.round(run.fraction * 100)"
            ><i :style="{ width: `${Math.round(run.fraction * 100)}%` }" /></span>
            <span v-if="run.detail" class="now-detail">{{ run.detail }}</span>
          </span>
        </div>
        <Button
          :label="run.waiting ? 'Respond' : 'Watch'"
          size="small"
          :severity="run.waiting && !needs ? undefined : 'secondary'"
          :outlined="!run.waiting || Boolean(needs)"
          @click="emit('watch')"
        />
      </article>

      <article v-else-if="nextItem" class="now-card" data-kind="next">
        <i class="now-ico aw-icon aw-icon-arrow-right" aria-hidden="true" />
        <div class="now-copy">
          <span class="now-label">Next step</span>
          <span class="now-title">{{ nextItem.stage.headline || nextItem.stage.filed?.label }}</span>
          <span class="now-detail">{{ nextPhase }} · {{ nextItem.stage.filed?.label || nextItem.stage.capability }}</span>
        </div>
        <Button
          v-if="nextItem.stage.action === 'import'"
          label="Import"
          size="small"
          :severity="needs ? 'secondary' : undefined"
          :outlined="Boolean(needs)"
          @click="emit('start', nextItem.stage)"
        />
        <SplitButton
          v-else-if="nextItem.stage.start?.alternates.length"
          label="Run"
          size="small"
          :severity="needs ? 'secondary' : undefined"
          :outlined="Boolean(needs)"
          :disabled="starting === nextItem.stage.capability"
          :model="startOptions(nextItem.stage)"
          @click="emit('start', nextItem.stage)"
        >
          <template #item="{ item, props: itemProps }">
            <a class="alt" v-bind="itemProps.action">
              <span>{{ item.label }}</span>
              <small v-if="item.note">{{ item.note }}</small>
            </a>
          </template>
        </SplitButton>
        <Button
          v-else
          label="Run"
          size="small"
          :severity="needs ? 'secondary' : undefined"
          :outlined="Boolean(needs)"
          :loading="starting === nextItem.stage.capability"
          @click="emit('start', nextItem.stage)"
        />
      </article>
    </section>

    <section v-if="activeGroup" class="panel" role="tabpanel" :aria-label="activeGroup.title">
      <header class="panel-head">
        <h3>{{ activeGroup.title }}</h3>
        <span class="panel-sub">{{ plural(activeGroup.total, 'work product') }}</span>
      </header>

      <div class="table-wrap">
        <table class="ledger">
          <thead>
            <tr>
              <th class="c-name">Work product</th>
              <th>Holds</th>
              <th class="c-status">Status</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in activeGroup.items"
              :key="item.stage.id"
              :class="{ ghost: !item.stage.held }"
              :data-live="item.live || null"
            >
              <td class="c-name">
                <component
                  :is="item.destination ? RouterLink : 'span'"
                  :to="item.destination ? nav.to(item.destination) : undefined"
                  class="wp"
                >{{ item.stage.filed?.label || item.stage.capability }}</component>
              </td>

              <td class="c-holds">
                <div class="holds">
                  <template v-for="(fact, idx) in holdsFacts(item)" :key="`${fact.kind}:${idx}`">
                    <span v-if="idx" class="sep" aria-hidden="true">·</span>
                    <RouterLink v-if="fact.to" :to="nav.to(fact.to)" class="door">
                      <b v-if="fact.figure" class="fig">{{ fact.figure }}</b>{{ fact.figure ? ' ' : '' }}{{ fact.text }}
                    </RouterLink>
                    <span v-else-if="fact.kind === 'count' && distribution(fact).length" class="dist">
                      <span v-if="fact.figure"><b class="fig">{{ fact.figure }}</b> {{ fact.text.replace(/:$/, '') }}</span>
                      <span class="dist-bar" role="img" :aria-label="partsLabel(fact)">
                        <i
                          v-for="(part, at) in distribution(fact)"
                          :key="part.text"
                          :data-tier="at"
                          :style="{ width: `${part.share * 100}%` }"
                          :title="part.text"
                        />
                      </span>
                      <span class="dist-words">
                        <template v-for="(part, at) in fact.parts ?? []" :key="part.text">{{ at ? ' · ' : '' }}<b class="fig">{{ part.figure }}</b> {{ part.text }}</template>
                      </span>
                    </span>
                    <span v-else :class="fact.kind">
                      <b v-if="fact.figure" class="fig">{{ fact.figure }}</b>{{ fact.figure ? ' ' : '' }}{{ fact.text }}<template
                        v-for="(part, at) in fact.parts ?? []"
                        :key="part.text"
                      >{{ at ? ', ' : fact.text ? ' ' : '' }}<b class="fig">{{ part.figure }}</b> {{ part.text }}</template>
                    </span>
                  </template>
                  <template v-for="tool in toolFacts(item)" :key="`tool:${tool.text}`">
                    <span class="sep" aria-hidden="true">·</span>
                    <RouterLink :to="nav.to(tool.to!)" class="door tool">
                      <i class="aw-icon aw-icon-wrench" aria-hidden="true" />{{ tool.text }}
                    </RouterLink>
                  </template>
                  <template v-if="item.stage.action === 'import' && item.stage.held">
                    <span class="sep" aria-hidden="true">·</span>
                    <button type="button" class="textbtn" @click="emit('start', item.stage)">Import more</button>
                  </template>
                </div>
                <div v-if="item.attentionReason" class="note">{{ item.attentionReason }}</div>
              </td>

              <td class="c-status">
                <span v-if="item.live" class="live" :data-live="item.live">
                  <i
                    class="aw-icon"
                    :class="item.live === 'running' ? 'aw-icon-spin aw-icon-loader-circle' : 'aw-icon-clock'"
                    aria-hidden="true"
                  />{{ statusLabel(item) }}<template v-if="item.liveElapsed"> · {{ item.liveElapsed }}</template>
                </span>
                <button
                  v-else-if="item.stage.open_points.length"
                  type="button"
                  class="review"
                  :title="item.stage.open_points[0].message"
                  @click="emit('open-point', item.stage.open_points[0])"
                >
                  <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" />{{ item.stage.open_points[0].action || 'Review' }}
                  <i class="aw-icon aw-icon-arrow-right" aria-hidden="true" />
                </button>
                <UiStateIcon v-else :state="item.state" show-label />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<style scoped>
.phases-view { display: flex; flex-direction: column; gap: 1rem; }
.sep { margin: 0 .35rem; color: var(--aw-muted); }
.fig { font-family: var(--aw-font-mono); font-weight: 600; color: var(--aw-ink); font-variant-numeric: tabular-nums; }

/* --- tabs: the overview and the navigation, drawn once ------------------- */
.tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.tab {
  display: flex; flex-direction: column; gap: .375rem; min-width: 0;
  padding: .75rem 1rem .875rem;
  border: 0; border-radius: calc(var(--aw-radius-surface) - 3px);
  background: transparent;
  font: inherit; text-align: left; cursor: pointer;
  transition: background-color .14s ease;
}
.tab:hover { background: color-mix(in srgb, var(--aw-raised) 70%, transparent); }
.tab[aria-selected='true'] {
  background: var(--aw-raised);
  box-shadow: inset 0 0 0 1px var(--aw-border-strong);
}
.tab:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; }

.tab-head { display: flex; align-items: center; gap: .5rem; min-width: 0; }
.tab-title {
  color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tab[data-state='later'] .tab-title { color: var(--aw-ink-soft); }
.mark { flex: none; font-size: var(--aw-text-base); }
.mark[data-mark='done'] { color: var(--aw-ok); }
.mark[data-mark='attention'] { color: var(--aw-warn); }
.mark[data-mark='live'] { color: var(--aw-info); }
.tab-num {
  flex: none; display: grid; place-items: center;
  width: 1rem; height: 1rem; border-radius: 50%;
  border: 1.5px solid var(--aw-muted); box-sizing: border-box;
  color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: .625rem; font-weight: 700;
}
.tab[aria-selected='true'] .tab-num { border-color: var(--aw-ink-strong); background: var(--aw-ink-strong); color: var(--aw-panel); }

.tab-line {
  display: flex; align-items: center; min-width: 0;
  color: var(--aw-muted); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden;
}
.tab-line > span:first-child { overflow: hidden; text-overflow: ellipsis; }
.tab-need { color: var(--aw-warn-ink); font-weight: 600; }
.tab-bar { display: flex; height: 3px; border-radius: 2px; background: var(--aw-border); overflow: hidden; }
.tab-bar i { background: var(--aw-teal); border-radius: 2px; }
.tab[data-state='live'] .tab-bar i { background: var(--aw-info); }

/* --- now: what a person must do, and what the assistant is doing -------- */
.now { display: grid; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); gap: .75rem; }
.now-card {
  display: flex; align-items: center; gap: .875rem; min-width: 0;
  padding: 1rem 1.125rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.now-card[data-kind='needs'] { border-color: var(--aw-warn-line); }
.now-card[data-kind='run'] { border-color: var(--aw-info-line); }
.now-card[data-kind='waiting'] { border-color: var(--aw-info-line); }
.now-ico { flex: none; align-self: flex-start; margin-top: .1rem; font-size: 1.15rem; }
.now-card[data-kind='needs'] .now-ico { color: var(--aw-warn); }
.now-card[data-kind='run'] .now-ico,
.now-card[data-kind='waiting'] .now-ico { color: var(--aw-info); }
.now-card[data-kind='next'] .now-ico { color: var(--aw-teal); }
.now-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0; }
.now-label { font-size: var(--aw-text-xs); font-weight: 600; color: var(--aw-muted); }
.now-card[data-kind='needs'] .now-label { color: var(--aw-warn-ink); }
.now-card[data-kind='run'] .now-label,
.now-card[data-kind='waiting'] .now-label { color: var(--aw-info); }
.now-card[data-kind='next'] .now-label { color: var(--aw-teal-strong); }
.now-title { color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 600; line-height: 1.35; }
.now-detail { color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-variant-numeric: tabular-nums; }
.now-progress { display: flex; align-items: center; gap: .625rem; margin-top: .15rem; }
.now-bar { flex: 1; display: flex; height: 6px; border-radius: 3px; background: var(--aw-raised); overflow: hidden; }
.now-bar i { background: var(--aw-info); border-radius: 3px; transition: width .3s ease; }
.alt { display: flex; flex-direction: column; gap: .1rem; }
.alt small { color: var(--aw-muted); }

/* --- the selected phase: one table, one vocabulary ---------------------- */
.panel {
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel); overflow: hidden;
}
.panel-head {
  display: flex; align-items: baseline; gap: .625rem;
  padding: 1rem 1.25rem .75rem; border-bottom: 1px solid var(--aw-border);
}
.panel-head h3 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 600; }
.panel-sub { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.table-wrap { overflow-x: auto; }
.ledger { width: 100%; min-width: 36rem; border-collapse: collapse; font-size: var(--aw-text-sm); }
.ledger th {
  padding: .625rem .75rem; text-align: left;
  color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600;
  letter-spacing: .06em; text-transform: uppercase;
}
.ledger td { padding: .875rem .75rem; border-top: 1px solid var(--aw-border); vertical-align: top; }
.ledger th:first-child, .ledger td:first-child { padding-left: 1.25rem; }
.ledger th:last-child, .ledger td:last-child { padding-right: 1.25rem; }
.ledger tbody tr:hover { background: color-mix(in srgb, var(--aw-raised) 45%, transparent); }
.ledger tr[data-live] { background: color-mix(in srgb, var(--aw-info-soft) 55%, transparent); }
.c-name { width: 32%; }
.c-status { width: 13rem; }

.wp { color: var(--aw-ink-strong); font-weight: 600; text-decoration: none; }
a.wp:hover { color: var(--aw-teal-strong); text-decoration: underline; }
tr.ghost .wp { color: var(--aw-ink-soft); }

.holds { display: flex; flex-wrap: wrap; align-items: center; row-gap: .25rem; color: var(--aw-ink-soft); line-height: 1.45; }
tr.ghost .holds { color: var(--aw-muted); }
.door { color: var(--aw-ink-soft); text-decoration: underline; text-decoration-color: var(--aw-border-strong); text-underline-offset: 3px; }
.door:hover { color: var(--aw-teal-strong); text-decoration-color: currentColor; }
.door.tool { display: inline-flex; align-items: center; gap: .3rem; color: var(--aw-muted); }
.door.tool .aw-icon { font-size: var(--aw-text-xs); }
.textbtn {
  padding: 0; border: 0; background: none; font: inherit; cursor: pointer;
  color: var(--aw-teal-strong); text-decoration: underline; text-underline-offset: 3px;
}
.dist { display: inline-flex; align-items: center; flex-wrap: wrap; gap: .25rem .75rem; }
.dist-bar { display: inline-flex; width: 6rem; height: 6px; border-radius: 3px; overflow: hidden; background: var(--aw-raised); }
.dist-bar i[data-tier='0'] { background: var(--aw-ink-soft); }
.dist-bar i[data-tier='1'] { background: color-mix(in srgb, var(--aw-ink-soft) 55%, var(--aw-panel)); }
.dist-bar i[data-tier='2'] { background: color-mix(in srgb, var(--aw-ink-soft) 30%, var(--aw-panel)); }
.dist-bar i[data-tier='3'] { background: color-mix(in srgb, var(--aw-ink-soft) 15%, var(--aw-panel)); }
.dist-words { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.dist-words .fig { color: var(--aw-ink-soft); }
.note { margin-top: .25rem; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); }

.live { display: inline-flex; align-items: center; gap: .375rem; color: var(--aw-info); font-weight: 600; font-variant-numeric: tabular-nums; }
.live[data-live='queued'] { color: var(--aw-muted); }
.review {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: 0; border: 0; background: none; cursor: pointer;
  color: var(--aw-warn-ink); font: inherit; font-weight: 600;
}
.review .aw-icon-triangle-alert { color: var(--aw-warn); }
.review .aw-icon-arrow-right { font-size: var(--aw-text-xs); }
.review:hover { text-decoration: underline; text-underline-offset: 3px; }
.review:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; border-radius: 4px; }

@container (max-width: 52rem) {
  .tabs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
