<script setup lang="ts">
import { computed } from 'vue'

import type { CycleEdgeKind, CycleGraph } from '../../types'
import { METRICS, layoutCycle } from './cycleLayout'

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

const viewBox = computed(() => {
  const { width, height } = layout.value
  // The bus rides above the nodes, so the box starts above the origin.
  return `0 ${-METRICS.busTop - 40} ${width + 40} ${height + METRICS.busTop + 80}`
})

const KIND_LABEL: Record<CycleEdgeKind, string> = {
  join: 'links, identifier = identifier',
  assert: 'must agree',
  anchor: 'population row to its document',
  table_join: 'table join',
}

function path(points: Array<{ x: number; y: number }>): string {
  return points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ')
}

function stepHeaderStyle(x: number, width: number) {
  return { left: `${x}px`, width: `${width}px` }
}

function nodeStyle(node: { x: number; y: number; width: number }) {
  return { left: `${node.x}px`, top: `${node.y}px`, width: `${node.width}px` }
}
</script>

<template>
  <div class="cycle-strip" data-testid="cycle-strip">
    <div
      class="cycle-strip__canvas"
      :style="{ width: `${layout.width + 40}px`, height: `${layout.height + 60}px` }"
    >
      <svg class="cycle-strip__edges" :viewBox="viewBox" :style="{
        width: `${layout.width + 40}px`,
        height: `${layout.height + METRICS.busTop + 80}px`,
        top: `${-METRICS.busTop - 40}px`,
      }">
        <defs>
          <marker
            v-for="kind in (['join', 'assert', 'anchor', 'table_join'] as CycleEdgeKind[])"
            :id="`cycle-arrow-${kind}`"
            :key="kind"
            markerWidth="7"
            markerHeight="7"
            refX="6"
            refY="3.5"
            orient="auto"
          >
            <path d="M 0 0 L 7 3.5 L 0 7 z" :class="`cycle-edge__head cycle-edge__head--${kind}`" />
          </marker>
        </defs>
        <path
          v-for="(edge, index) in layout.edges"
          :key="`${edge.ruleId}-${index}`"
          :d="path(edge.points)"
          :class="['cycle-edge', `cycle-edge--${edge.kind}`]"
          :marker-end="`url(#cycle-arrow-${edge.kind})`"
          fill="none"
        >
          <title>{{ KIND_LABEL[edge.kind] }}{{ edge.label ? ` — ${edge.label}` : '' }}</title>
        </path>
      </svg>

      <div
        v-for="column in layout.columns"
        :key="column.step"
        class="cycle-step"
        :style="stepHeaderStyle(column.x, column.width)"
      >
        <span class="cycle-step__name">{{ column.step }}</span>
      </div>

      <template v-for="column in layout.columns" :key="`nodes-${column.step}`">
        <article
          v-for="node in column.nodes"
          :key="node.id"
          :class="[
            'cycle-node',
            `cycle-node--${node.kind}`,
            { 'cycle-node--unbound': node.bound === false },
          ]"
          :style="nodeStyle(node)"
          :data-testid="`cycle-node-${node.id}`"
        >
          <header class="cycle-node__head">
            <span class="cycle-node__title">{{ node.title }}</span>
            <span class="cycle-node__meta">
              {{ node.countLabel }}
              <em v-if="node.anchor" class="cycle-node__anchor">anchor</em>
            </span>
          </header>
          <!-- The field rows come first and nothing is drawn above them: an
               arrow enters the centre of a row at a y the layout computed from
               that row's index, so a note between the header and the list would
               move every endpoint off its field. -->
          <ul v-if="node.fields.length" class="cycle-node__fields">
            <li
              v-for="field in node.fields"
              :key="field.name"
              class="cycle-field"
              :style="{ height: `${METRICS.fieldHeight}px` }"
            >
              <span class="cycle-field__name">{{ field.name }}</span>
              <span v-if="field.stated" class="cycle-field__mark">stated</span>
            </li>
          </ul>
          <p v-else-if="node.kind === 'document'" class="cycle-node__pending">
            {{ node.hasSchema ? 'No field of this type is in a rule' : 'No schema induced yet' }}
          </p>
          <p v-if="node.hiddenFieldCount" class="cycle-node__hidden">
            +{{ node.hiddenFieldCount }} not in a rule
          </p>
          <p v-if="node.note" class="cycle-node__note">{{ node.note }}</p>
        </article>
      </template>
    </div>

    <p class="cycle-strip__legend">
      <span class="cycle-legend cycle-legend--join">link</span>
      <span class="cycle-legend cycle-legend--assert">must agree</span>
      <span class="cycle-legend cycle-legend--anchor">population row</span>
      <span class="cycle-legend cycle-legend--table_join">table join</span>
      <span class="cycle-strip__note">
        Only fields that take part in a relationship are shown.
      </span>
    </p>
  </div>
</template>

<style scoped>
/* Every colour is an `--aw-*` token, so the strip follows the app into dark
   mode. It did not once: the PrimeVue surface tokens do not flip, so the nodes
   drew white with light text on them. */
.cycle-strip { display: flex; flex-direction: column; gap: var(--aw-space-3); }
.cycle-strip__canvas {
  position: relative;
  min-height: 20rem;
  padding-top: 0.5rem;
  overflow: visible;
}
.cycle-strip__edges { position: absolute; left: 0; pointer-events: none; overflow: visible; }
.cycle-edge { stroke-width: 1.5; }
.cycle-edge--join { stroke: var(--aw-teal-600); }
.cycle-edge--assert { stroke: var(--aw-accent); stroke-dasharray: 4 3; }
.cycle-edge--anchor { stroke: var(--aw-info); }
.cycle-edge--table_join { stroke: var(--aw-border-strong); }
.cycle-edge__head--join { fill: var(--aw-teal-600); }
.cycle-edge__head--assert { fill: var(--aw-accent); }
.cycle-edge__head--anchor { fill: var(--aw-info); }
.cycle-edge__head--table_join { fill: var(--aw-border-strong); }

.cycle-step {
  position: absolute;
  top: 0;
  height: 1.75rem;
  display: flex;
  align-items: center;
  border-bottom: 2px solid var(--aw-border-strong);
}
.cycle-step__name {
  font-size: var(--aw-text-sm);
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--aw-ink-strong);
}

.cycle-node {
  position: absolute;
  box-sizing: border-box;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: var(--aw-ink);
  padding: 0 0 0.5rem;
  overflow: hidden;
}
.cycle-node--population { background: var(--aw-raised); }
.cycle-node--unbound { border-style: dashed; border-color: var(--aw-danger-line); }
.cycle-node__head {
  height: 2.875rem;
  padding: 0.375rem 0.625rem;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 0.125rem;
  border-bottom: 1px solid var(--aw-border);
}
.cycle-node__title {
  font-size: var(--aw-text-sm);
  font-weight: 650;
  color: var(--aw-ink-strong);
}
.cycle-node__meta {
  font-size: var(--aw-text-2xs);
  color: var(--aw-muted);
  display: flex;
  gap: 0.375rem;
}
.cycle-node__anchor { font-style: normal; font-weight: 650; color: var(--aw-teal); }
.cycle-node__note,
.cycle-node__pending,
.cycle-node__hidden {
  margin: 0.375rem 0.625rem 0;
  font-size: var(--aw-text-2xs);
  color: var(--aw-muted);
}
.cycle-node--unbound .cycle-node__note { color: var(--aw-danger); }
.cycle-node__fields { list-style: none; margin: 0.5rem 0 0; padding: 0; }
.cycle-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.375rem;
  padding: 0 0.625rem;
  font-size: var(--aw-text-xs);
  font-family: var(--aw-font-mono);
  color: var(--aw-ink-soft);
}
.cycle-field__mark {
  font-family: var(--aw-font-sans);
  font-size: var(--aw-text-2xs);
  color: var(--aw-accent);
}

.cycle-strip__legend {
  display: flex;
  align-items: center;
  gap: 0.875rem;
  margin: 0;
  font-size: var(--aw-text-xs);
  color: var(--aw-muted);
}
.cycle-legend { display: inline-flex; align-items: center; gap: 0.375rem; }
.cycle-legend::before {
  content: '';
  width: 1rem;
  height: 0;
  border-top-width: 2px;
  border-top-style: solid;
}
.cycle-legend--join::before { border-top-color: var(--aw-teal-600); }
.cycle-legend--assert::before {
  border-top-color: var(--aw-accent);
  border-top-style: dashed;
}
.cycle-legend--anchor::before { border-top-color: var(--aw-info); }
.cycle-legend--table_join::before { border-top-color: var(--aw-border-strong); }
.cycle-strip__note { margin-left: auto; font-style: italic; }
</style>
