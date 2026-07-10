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
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  background: var(--p-surface-0);
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
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-surface-500);
  border-bottom: 1px solid var(--p-surface-200);
  background: var(--p-surface-50);
}

.grid-row { border-bottom: 1px solid var(--p-surface-100); }
.grid-row:last-child { border-bottom: none; }
.grid-row:hover { background: var(--p-surface-50); }

.col-name {
  font-weight: 500;
  font-size: 0.9rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.col-kind {
  font-size: 0.72rem;
  border-radius: 999px;
  padding: 0.1rem 0.55rem;
  text-align: center;
  width: fit-content;
  background: var(--p-surface-100);
  color: var(--p-surface-600);
}
.col-kind[data-kind='numeric'] { background: var(--p-blue-50); color: var(--p-blue-700); }
.col-kind[data-kind='date'] { background: var(--p-purple-50); color: var(--p-purple-700); }
.col-kind[data-kind='boolean'] { background: var(--p-teal-50); color: var(--p-teal-700); }
.col-kind[data-kind='missing'] { background: var(--p-red-50); color: var(--p-red-700); }

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  align-items: center;
  min-width: 0;
}
.none { font-size: 0.78rem; }

.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font: inherit;
  font-size: 0.78rem;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  border: 1px solid var(--p-primary-200);
  background: var(--p-primary-50);
  color: var(--p-primary-700);
  cursor: pointer;
  transition: border-color 0.15s;
  max-width: 100%;
}
.chip:hover { border-color: var(--p-primary-400); }
.chip.warn {
  border-color: var(--p-amber-300);
  background: var(--p-amber-50);
  color: var(--p-amber-800);
}
.chip.warn > .pi-exclamation-triangle { font-size: 0.65rem; }
.chip.off {
  border-color: var(--p-surface-200);
  background: var(--p-surface-100);
  color: var(--p-surface-500);
  text-decoration: line-through;
}
.chip.missing {
  border-color: var(--p-red-200);
  background: var(--p-red-50);
  color: var(--p-red-700);
}
.chip.ghost {
  border-style: dashed;
  border-color: var(--p-primary-300);
  background: transparent;
  color: var(--p-primary-600);
  cursor: default;
}
.chip .accept:hover { color: var(--p-green-600); }
.chip .x {
  font-size: 0.62rem;
  padding: 0.12rem;
  border-radius: 50%;
}
.chip .x:hover { background: rgba(0, 0, 0, 0.12); }

.table-row {
  border-top: 2px solid var(--p-surface-200);
  background: var(--p-surface-50);
}
.unknown-row .col-name { color: var(--p-red-600); }
.unknown-row .col-name i { font-size: 0.8rem; margin-right: 0.2rem; }

.row-add { justify-self: end; }
</style>
