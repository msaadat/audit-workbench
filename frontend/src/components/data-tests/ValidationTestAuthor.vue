<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'

import { api } from '../../api'
import type {
  CheckMeta,
  ColumnSchema,
  RuleSuggestion,
  ValidationRule,
  WorkspaceSummary,
} from '../../types'
import CheckDialog from '../validation/CheckDialog.vue'
import ValidationGrid from '../validation/ValidationGrid.vue'
import { newRuleId } from '../validation/rules'

const props = defineProps<{
  workspace: WorkspaceSummary
  table: string | null
}>()
const rules = defineModel<ValidationRule[]>({ required: true })
const emit = defineEmits<{
  valid: [boolean]
  error: [summary: string, error: unknown]
}>()

const checks = ref<CheckMeta[]>([])
const schema = ref<ColumnSchema[]>([])
const suggestions = ref<ValidationRule[]>([])
const suggesting = ref(false)
const dialogVisible = ref(false)
const dialogColumn = ref<string | null>(null)
const dialogRule = ref<ValidationRule | null>(null)
const enabledCount = computed(() => rules.value.filter((rule) => rule.enabled).length)

async function loadChecks() {
  try {
    checks.value = await api.get<CheckMeta[]>('/api/validation/checks')
  } catch (error) {
    emit('error', 'Could not load validation checks', error)
  }
}

async function loadSchema() {
  schema.value = []
  suggestions.value = []
  if (!props.table) return
  try {
    schema.value = (
      await api.get<{ columns: ColumnSchema[] }>(
        `/api/workspaces/${props.workspace.id}/tables/${props.table}/schema`,
      )
    ).columns
  } catch (error) {
    emit('error', 'Could not load the table schema', error)
  }
}

async function suggest() {
  if (!props.table) return
  suggesting.value = true
  try {
    const response = await api.get<{ suggestions: RuleSuggestion[] }>(
      `/api/workspaces/${props.workspace.id}/tables/${props.table}/suggest-rules`,
    )
    const have = new Set(rules.value.map((rule) => `${rule.column}|${rule.check}`))
    suggestions.value = response.suggestions
      .filter((proposal) => {
        const key = `${proposal.column}|${proposal.check}`
        if (have.has(key)) return false
        have.add(key)
        return true
      })
      .map((proposal) => ({
        id: newRuleId(),
        column: proposal.column,
        check: proposal.check,
        params: proposal.params,
        severity: proposal.severity,
        enabled: true,
      }))
  } catch (error) {
    emit('error', 'Could not suggest validation checks', error)
  } finally {
    suggesting.value = false
  }
}

function openAdd(column: string | null) {
  dialogColumn.value = column
  dialogRule.value = null
  dialogVisible.value = true
}

function openEdit(rule: ValidationRule) {
  dialogColumn.value = rule.column
  dialogRule.value = rule
  dialogVisible.value = true
}

function upsertRule(rule: ValidationRule) {
  const index = rules.value.findIndex((item) => item.id === rule.id)
  if (index >= 0) {
    const next = [...rules.value]
    next[index] = rule
    rules.value = next
  } else {
    rules.value = [...rules.value, rule]
  }
  dialogVisible.value = false
}

function removeRule(ruleId: string) {
  rules.value = rules.value.filter((rule) => rule.id !== ruleId)
  dialogVisible.value = false
}

function acceptSuggestion(rule: ValidationRule) {
  suggestions.value = suggestions.value.filter((item) => item.id !== rule.id)
  rules.value = [...rules.value, rule]
}

watch(() => props.table, () => void loadSchema(), { immediate: true })
watch(rules, (value) => emit('valid', value.length > 0), { immediate: true, deep: true })
void loadChecks()
</script>

<template>
  <section class="author">
    <div v-if="table" class="suggest-bar">
      <Button
        label="Suggest checks"
        icon="pi pi-lightbulb"
        severity="secondary"
        outlined
        size="small"
        :loading="suggesting"
        @click="suggest"
      />
      <template v-if="suggestions.length">
        <span>{{ suggestions.length }} suggested check{{ suggestions.length === 1 ? '' : 's' }}</span>
        <Button
          label="Accept all"
          text
          size="small"
          @click="rules = [...rules, ...suggestions]; suggestions = []"
        />
        <Button label="Dismiss all" text size="small" severity="secondary" @click="suggestions = []" />
      </template>
    </div>
    <p v-if="!table" class="muted">Choose a table to configure field-level checks.</p>
    <template v-else>
      <ValidationGrid
        :schema="schema"
        :rules="rules"
        :suggestions="suggestions"
        :checks="checks"
        @add="openAdd"
        @edit="openEdit"
        @remove="removeRule"
        @accept="acceptSuggestion"
        @dismiss="id => suggestions = suggestions.filter(item => item.id !== id)"
      />
      <p class="muted">
        {{ rules.length }} rule{{ rules.length === 1 ? '' : 's' }}
        <template v-if="rules.length"> ({{ enabledCount }} enabled)</template>
      </p>
    </template>

    <CheckDialog
      v-model:visible="dialogVisible"
      :workspace="workspace"
      :table="table"
      :schema="schema"
      :checks="checks"
      :column="dialogColumn"
      :rule="dialogRule"
      @save="upsertRule"
      @remove="removeRule"
    />
  </section>
</template>

<style scoped>
.author { display:flex; flex-direction:column; gap:.65rem }
.suggest-bar { display:flex; align-items:center; gap:.5rem; min-height:2.1rem; flex-wrap:wrap }
.suggest-bar span,.muted { color:var(--aw-muted); font-size:.78rem }
.muted { margin:.1rem 0 }
</style>
