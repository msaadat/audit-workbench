<script setup lang="ts">
import { computed } from 'vue'

import type { CycleEdgeKind, CycleGraph } from '../../types'
import { FIELDS_TOP, METRICS, layoutCycle, type LaidOutField, type LaidOutNode } from './cycleLayout'

/**
 * The cycle as one strip that scrolls to the right.
 *
 * HTML nodes over a single absolutely-positioned SVG edge layer, which is what
 * makes this read as a relationship diagram rather than a box-and-arrow sketch:
 * an arrow endpoint is the centre of a *field row*, computed from that row's
 * index by the same arithmetic that positions it.
 *
 * Every position comes from `cycleLayout`; nothing is derived here. The strip
 * has two states and draws both — before any schema exists the document nodes
 * carry no fields and what shows is the flow, the populations and the table
 * joins; the field edges appear when a ruleset does.
 */

const props = withDefaults(
  defineProps<{ graph: CycleGraph; showAllFields?: boolean }>(),
  { showAllFields: false },
)

const layout = computed(() =>
  layoutCycle(props.graph, { showAllFields: props.showAllFields }),
)

const KINDS: CycleEdgeKind[] = ['join', 'assert', 'anchor', 'table_join']

const KIND_LABEL: Record<CycleEdgeKind, string> = {
  join: 'links, identifier = identifier',
  assert: 'must agree',
  anchor: 'population row to its document',
  table_join: 'table join',
}

function path(points: Array<{ x: number; y: number }>): string {
  return points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ')
}

function box(node: { x: number; y: number; width: number; height?: number }) {
  return {
    left: `${node.x}px`,
    top: `${node.y}px`,
    width: `${node.width}px`,
    ...(node.height !== undefined ? { height: `${node.height}px` } : {}),
  }
}

/** Which glyph a field row carries: what kind of thing the schema says it is. */
function glyph(node: LaidOutNode, field: LaidOutField): 'key' | 'control' | 'party' | 'dot' {
  if (node.kind === 'population') return field.linked ? 'key' : 'dot'
  if (field.role === 'identifier') return 'key'
  if (field.role === 'control') return 'control'
  if (field.role === 'party') return 'party'
  return 'dot'
}

function edgeTitle(edge: { kind: CycleEdgeKind; label: string }): string {
  return edge.label ? `${KIND_LABEL[edge.kind]} — ${edge.label}` : KIND_LABEL[edge.kind]
}
</script>

<template>
  <div class="cycle-strip" data-testid="cycle-strip">
    <div
      class="cycle-strip__canvas"
      :style="{ width: `${layout.width}px`, height: `${layout.height}px` }"
    >
      <svg
        class="cycle-strip__edges"
        :width="layout.width"
        :height="layout.height"
        :viewBox="`0 0 ${layout.width} ${layout.height}`"
      >
        <defs>
          <marker
            v-for="kind in KINDS"
            :id="`cycle-arrow-${kind}`"
            :key="kind"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" :class="`cycle-edge__head cycle-edge__head--${kind}`" />
          </marker>
        </defs>
        <path
          v-for="(edge, index) in layout.edges"
          :key="`${edge.ruleId}-${index}`"
          :d="path(edge.points)"
          :class="['cycle-edge', `cycle-edge--${edge.kind}`]"
          :marker-end="`url(#cycle-arrow-${edge.kind})`"
          fill="none"
          stroke-linejoin="round"
        >
          <title>{{ edgeTitle(edge) }}</title>
        </path>
      </svg>

      <template v-for="column in layout.columns" :key="column.step">
        <div class="cycle-step" :style="{ left: `${column.x}px`, width: `${column.width}px` }">
          <span class="cycle-step__number">{{ column.index + 1 }}</span>
          <span class="cycle-step__name" :title="column.step">{{ column.step }}</span>
        </div>
      </template>
      <span
        v-for="(mark, index) in layout.flowMarks"
        :key="`flow-${index}`"
        class="cycle-flow"
        :style="{ left: `${mark.x}px`, top: `${mark.y}px` }"
        aria-hidden="true"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M5 12h14" /><path d="m13 6 6 6-6 6" />
        </svg>
      </span>

      <template v-for="column in layout.columns" :key="`nodes-${column.step}`">
        <template v-for="node in column.nodes" :key="`${node.step}-${node.id}`">
          <p
            v-if="node.placeholder"
            class="cycle-placeholder"
            :style="box(node)"
            :data-testid="`cycle-node-${node.id}`"
          >
            No population of its own: recorded on
            <code>{{ node.title }}</code>
            as {{ node.columns.join(', ') }}.
          </p>
          <article
            v-else
            :class="[
              'cycle-node',
              `cycle-node--${node.kind}`,
              { 'cycle-node--unbound': node.bound === false },
            ]"
            :style="box(node)"
            :data-testid="`cycle-node-${node.id}`"
          >
            <header class="cycle-node__head" :title="node.subtitle">
              <svg v-if="node.kind === 'document'" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" />
              </svg>
              <svg v-else width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 10h18M9 10v10" />
              </svg>
              <span class="cycle-node__title">{{ node.title }}</span>
              <em v-if="node.anchor" class="cycle-pill cycle-pill--teal">population</em>
            </header>
            <p class="cycle-node__count">{{ node.countLabel }}</p>
            <!-- The field rows come next and nothing is drawn above them: an
                 arrow enters the centre of a row at a y the layout computed
                 from that row's index, so a note between the header and the
                 list would move every endpoint off its field. -->
            <ul
              v-if="node.fields.length"
              class="cycle-node__fields"
              :style="{ top: `${FIELDS_TOP}px` }"
            >
              <li
                v-for="field in node.fields"
                :key="field.name"
                :class="['cycle-field', `cycle-field--${glyph(node, field)}`]"
                :style="{ height: `${METRICS.fieldHeight}px` }"
              >
                <svg v-if="glyph(node, field) === 'key'" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M21 2l-2 2m-7.6 7.6a5 5 0 1 1-7.1 7.1 5 5 0 0 1 7.1-7.1zm0 0L19 3.4M15 8l2 2" />
                </svg>
                <svg v-else-if="glyph(node, field) === 'control'" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" /><path d="m9 12 2 2 4-4" />
                </svg>
                <svg v-else-if="glyph(node, field) === 'party'" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" />
                </svg>
                <svg v-else width="11" height="11" viewBox="0 0 24 24" aria-hidden="true">
                  <circle cx="12" cy="12" r="3" fill="currentColor" />
                </svg>
                <span class="cycle-field__name" :title="field.name">{{ field.name }}</span>
                <em v-if="field.anchor" class="cycle-pill cycle-pill--teal">anchor</em>
                <em v-if="field.stated" class="cycle-pill cycle-pill--accent">stated</em>
              </li>
            </ul>
            <div class="cycle-node__trailing" :style="{ top: `${FIELDS_TOP + node.fields.length * METRICS.fieldHeight}px` }">
              <p v-if="!node.fields.length && node.kind === 'document'" class="cycle-node__line">
                {{ node.hasSchema ? 'No field of this type is in a rule' : 'No schema induced yet' }}
              </p>
              <p v-if="node.hiddenFieldCount" class="cycle-node__line">
                +{{ node.hiddenFieldCount }} {{ node.kind === 'population' ? 'other columns' : 'not in a rule' }}
              </p>
              <p v-if="node.note && node.bound === false" class="cycle-node__line cycle-node__line--danger">
                {{ node.note }}
              </p>
              <p v-else-if="node.note" class="cycle-node__line cycle-node__line--wrap">
                {{ node.note }}
              </p>
            </div>
          </article>
        </template>
      </template>

      <template v-for="(edge, index) in layout.edges" :key="`label-${index}`">
        <span
          v-if="edge.labelAt"
          class="cycle-edge-label"
          :style="{ left: `${edge.labelAt.x}px`, top: `${edge.labelAt.y}px` }"
        >{{ edge.labelAt.text }}</span>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* Every colour is an `--aw-*` token, so the strip follows the app into dark
   mode. It did not once: the PrimeVue surface tokens do not flip, so the nodes
   drew white with light text on them. */
.cycle-strip {
  --cycle-join: var(--aw-teal);
  --cycle-assert: var(--aw-accent);
  --cycle-anchor: var(--aw-ink-strong);
  --cycle-table-join: color-mix(in srgb, var(--aw-muted) 70%, var(--aw-panel));
  --cycle-band: var(--aw-border-strong);
  position: relative;
  min-width: 100%;
  width: max-content;
}
.cycle-strip__canvas { position: relative; }
.cycle-strip__edges { position: absolute; inset: 0; pointer-events: none; overflow: visible; }
.cycle-edge { pointer-events: stroke; }
.cycle-edge--join { stroke: var(--cycle-join); stroke-width: 2; }
.cycle-edge--assert { stroke: var(--cycle-assert); stroke-width: 1.75; stroke-dasharray: 5 4; }
.cycle-edge--anchor { stroke: var(--cycle-anchor); stroke-width: 2.5; }
.cycle-edge--table_join { stroke: var(--cycle-table-join); stroke-width: 1.75; }
.cycle-edge__head--join { fill: var(--cycle-join); }
.cycle-edge__head--assert { fill: var(--cycle-assert); }
.cycle-edge__head--anchor { fill: var(--cycle-anchor); }
.cycle-edge__head--table_join { fill: var(--cycle-table-join); }

.cycle-edge-label {
  position: absolute;
  transform: translate(-50%, -50%);
  padding: 0 8px;
  height: 16px;
  line-height: 16px;
  border: 1px solid var(--cycle-anchor);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-panel);
  color: var(--cycle-anchor);
  font-family: var(--aw-font-mono);
  font-size: 10px;
  font-weight: 600;
  white-space: nowrap;
}

.cycle-step {
  position: absolute;
  top: 12px;
  height: 30px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--cycle-band);
}
.cycle-step__number {
  flex: none;
  display: inline-grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-ink-strong);
  color: var(--aw-panel);
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.cycle-step__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--aw-ink-strong);
  font-size: 12.5px;
  font-weight: 600;
}
.cycle-flow { position: absolute; color: var(--cycle-band); line-height: 0; }

.cycle-node {
  position: absolute;
  box-sizing: border-box;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: var(--aw-ink);
  box-shadow: var(--aw-shadow-sm);
  overflow: hidden;
}
.cycle-node--unbound { border-style: dashed; }
.cycle-node__head {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 10px;
  background: var(--aw-teal-soft);
  color: var(--aw-teal-strong);
}
.cycle-node--population .cycle-node__head {
  background: var(--aw-raised);
  color: var(--aw-ink-strong);
}
.cycle-node__title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--aw-font-mono);
  font-size: 12px;
  font-weight: 600;
}
.cycle-node__count {
  margin: 0;
  padding: 0 10px;
  height: 14px;
  line-height: 13px;
  color: var(--aw-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.cycle-node__fields {
  position: absolute;
  left: 0;
  right: 0;
  list-style: none;
  margin: 0;
  padding: 0;
}
.cycle-field {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  color: var(--aw-ink);
}
.cycle-field > svg { flex: none; color: var(--aw-muted-strong); }
.cycle-field--key > svg { color: var(--cycle-join); }
.cycle-node--population .cycle-field--key > svg { color: var(--cycle-anchor); }
.cycle-field--control > svg { color: var(--cycle-assert); }
.cycle-field--party > svg { color: var(--aw-ink-soft); }
.cycle-field__name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--aw-font-mono);
  font-size: 11.5px;
}
.cycle-pill {
  flex: none;
  padding: 1px 6px;
  border: 1px solid;
  border-radius: var(--aw-radius-pill);
  font-family: var(--aw-font-sans);
  font-style: normal;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  line-height: 1.3;
}
.cycle-pill--teal { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); color: var(--aw-teal-strong); }
.cycle-pill--accent { border-color: var(--aw-accent-line); background: var(--aw-accent-soft); color: var(--aw-accent); }

.cycle-node__trailing { position: absolute; left: 0; right: 0; }
.cycle-node__line {
  margin: 0;
  padding: 0 10px;
  height: 16px;
  line-height: 16px;
  color: var(--aw-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cycle-node__line--wrap { height: 32px; line-height: 16px; white-space: normal; }
.cycle-node__line--danger { color: var(--aw-danger-ink); font-weight: 600; }

.cycle-placeholder {
  position: absolute;
  box-sizing: border-box;
  margin: 0;
  padding: 10px 12px;
  border: 1px dashed var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  color: var(--aw-muted);
  font-size: 11.5px;
  line-height: 1.4;
  overflow: hidden;
}
.cycle-placeholder code { font-family: var(--aw-font-mono); font-size: 11px; color: var(--aw-ink); }
</style>
