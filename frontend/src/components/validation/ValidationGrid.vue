<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'

import type { CheckMeta, ColumnSchema, ValidationRule } from '../../types'
import { ruleLabel } from './rules'

// The authoring centerpiece: one row per column of the bound table, with the
// attached checks as clickable chips — a field with no chips is visibly
// unvalidated. Table-level checks (unique key, row count) get a pinned row at
// the bottom; rules pointing at columns the table no longer has are kept in
// an "unknown columns" section so a schema change never silently drops rules.
const props = defineProps<{
  schema: ColumnSchema[]
  rules: ValidationRule[]
  checks: CheckMeta[]
  // Profile-derived proposals rendered as dashed "ghost" chips until the
  // auditor explicitly accepts (✓) or dismisses (✕) them.
  suggestions?: ValidationRule[]
}>()
const emit = defineEmits<{
  add: [string | null]
  edit: [ValidationRule]
  remove: [string]
  accept: [ValidationRule]
  dismiss: [string]
}>()

const columnRules = computed(() => {
  const byColumn: Record<string, ValidationRule[]> = {}
  for (const rule of props.rules) {
    if (rule.column) (byColumn[rule.column] ??= []).push(rule)
  }
  return byColumn
})

const columnSuggestions = computed(() => {
  const byColumn: Record<string, ValidationRule[]> = {}
  for (const rule of props.suggestions ?? []) {
    if (rule.column) (byColumn[rule.column] ??= []).push(rule)
  }
  return byColumn
})

const tableRules = computed(() => props.rules.filter((r) => !r.column))

const unknownRules = computed(() => {
  const known = new Set(props.schema.map((c) => c.name))
  return props.rules.filter((r) => r.column && !known.has(r.column))
})

function label(rule: ValidationRule): string {
  return ruleLabel(rule, props.checks)
}
</script>

<template>
  <div class="grid">
    <div class="grid-head">
      <span>Field</span>
      <span>Type</span>
      <span>Checks</span>
      <span />
    </div>

    <div v-for="column in schema" :key="column.name" class="grid-row">
      <span class="col-name">{{ column.name }}</span>
      <span class="col-kind" :data-kind="column.kind">{{ column.kind }}</span>
      <span class="chips">
        <button
          v-for="rule in columnRules[column.name] ?? []"
          :key="rule.id"
          class="chip"
          :class="{ warn: rule.severity === 'warn', off: !rule.enabled }"
          @click="emit('edit', rule)"
          v-tooltip.top="rule.enabled ? 'Edit check' : 'Disabled — click to edit'"
        >
          <i v-if="rule.severity === 'warn'" class="pi pi-exclamation-triangle" />
          {{ label(rule) }}
          <i class="pi pi-times x" @click.stop="emit('remove', rule.id)" />
        </button>
        <span
          v-for="ghost in columnSuggestions[column.name] ?? []"
          :key="ghost.id"
          class="chip ghost"
          v-tooltip.top="'Suggested from the current data'"
        >
          {{ label(ghost) }}
          <i class="pi pi-check x accept" @click="emit('accept', ghost)" v-tooltip.top="'Accept'" />
          <i class="pi pi-times x" @click="emit('dismiss', ghost.id)" v-tooltip.top="'Dismiss'" />
        </span>
        <span
          v-if="!(columnRules[column.name] ?? []).length && !(columnSuggestions[column.name] ?? []).length"
          class="muted none"
          >— no checks —</span
        >
      </span>
      <span class="row-add">
        <Button
          icon="pi pi-plus"
          label="Add"
          text
          size="small"
          @click="emit('add', column.name)"
        />
      </span>
    </div>

    <div class="grid-row table-row">
      <span class="col-name">Table-level</span>
      <span class="col-kind" data-kind="table">table</span>
      <span class="chips">
        <button
          v-for="rule in tableRules"
          :key="rule.id"
          class="chip"
          :class="{ warn: rule.severity === 'warn', off: !rule.enabled }"
          @click="emit('edit', rule)"
        >
          <i v-if="rule.severity === 'warn'" class="pi pi-exclamation-triangle" />
          {{ label(rule) }}
          <i class="pi pi-times x" @click.stop="emit('remove', rule.id)" />
        </button>
        <span v-if="!tableRules.length" class="muted none">— no checks —</span>
      </span>
      <span class="row-add">
        <Button icon="pi pi-plus" label="Add" text size="small" @click="emit('add', null)" />
      </span>
    </div>

    <div v-if="unknownRules.length" class="grid-row unknown-row">
      <span class="col-name">
        <i class="pi pi-exclamation-circle" /> Unknown columns
      </span>
      <span class="col-kind" data-kind="missing">missing</span>
      <span class="chips">
        <button
          v-for="rule in unknownRules"
          :key="rule.id"
          class="chip missing"
          @click="emit('edit', rule)"
          v-tooltip.top="`Column '${rule.column}' is not in this table — the rule will error on run`"
        >
          {{ rule.column }}: {{ label(rule) }}
          <i class="pi pi-times x" @click.stop="emit('remove', rule.id)" />
        </button>
      </span>
      <span class="row-add" />
    </div>
  </div>
</template>

<style scoped>
.grid {
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  overflow: hidden;
}

.grid-head,
.grid-row {
  display: grid;
  grid-template-columns: minmax(9rem, 14rem) 5.5rem 1fr auto;
  gap: 0.75rem;
  align-items: center;
  padding: 0.45rem 0.9rem;
}

.grid-head {
  font-size: var(--aw-text-xs);
  font-weight: 600;
  color: var(--aw-muted);
  border-bottom: 1px solid var(--aw-border);
  background: var(--aw-canvas);
}

.grid-row { border-bottom: 1px solid var(--aw-raised); }
.grid-row:last-child { border-bottom: none; }
.grid-row:hover { background: var(--aw-canvas); }

.col-name {
  font-weight: 500;
  font-size: var(--aw-text-base);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.col-kind {
  font-size: var(--aw-text-xs);
  border-radius: var(--aw-radius-pill);
  padding: 0.1rem 0.55rem;
  text-align: center;
  width: fit-content;
  background: var(--aw-raised);
  color: var(--aw-ink-soft);
}
.col-kind[data-kind='numeric'] { background: var(--aw-info-soft); color: var(--aw-info); }
.col-kind[data-kind='date'] { background: var(--aw-accent-soft); color: var(--aw-accent); }
.col-kind[data-kind='boolean'] { background: var(--aw-teal-soft); color: var(--aw-teal); }
.col-kind[data-kind='missing'] { background: var(--aw-danger-soft); color: var(--aw-danger); }

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  align-items: center;
  min-width: 0;
}
.none { font-size: var(--aw-text-sm); }

.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font: inherit;
  font-size: var(--aw-text-sm);
  padding: 0.15rem 0.55rem;
  border-radius: var(--aw-radius-pill);
  border: 1px solid var(--aw-teal-line);
  background: var(--aw-teal-soft);
  color: var(--aw-teal-strong);
  cursor: pointer;
  transition: border-color 0.15s;
  max-width: 100%;
}
.chip:hover { border-color: var(--aw-teal-600); }
.chip.warn {
  border-color: var(--aw-warn-line);
  background: var(--aw-warn-soft);
  color: var(--aw-warn-ink);
}
.chip.warn > .pi-exclamation-triangle { font-size: var(--aw-text-2xs); }
.chip.off {
  border-color: var(--aw-border);
  background: var(--aw-raised);
  color: var(--aw-muted);
  text-decoration: line-through;
}
.chip.missing {
  border-color: var(--aw-danger-line);
  background: var(--aw-danger-soft);
  color: var(--aw-danger);
}
.chip.ghost {
  border-style: dashed;
  border-color: var(--aw-teal-line);
  background: transparent;
  color: var(--aw-teal);
  cursor: default;
}
.chip .accept:hover { color: var(--aw-ok); }
.chip .x {
  font-size: var(--aw-text-2xs);
  padding: 0.12rem;
  border-radius: 50%;
}
.chip .x:hover { background: rgba(0, 0, 0, 0.12); }

.table-row {
  border-top: 2px solid var(--aw-border);
  background: var(--aw-canvas);
}
.unknown-row .col-name { color: var(--aw-danger); }
.unknown-row .col-name i { font-size: var(--aw-text-sm); margin-right: 0.2rem; }

.row-add { justify-self: end; }
</style>
