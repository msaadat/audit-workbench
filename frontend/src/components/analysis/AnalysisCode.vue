<script setup lang="ts">
import { computed, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'

import { api, ApiError } from '../../api'
import type { FramePayload, RunPythonResult, SavedAnalysis, WorkspaceSummary } from '../../types'
import ChartView from '../ChartView.vue'
import CodeEditor from '../CodeEditor.vue'

// The hand-written creation surface: a blank Python editor against the same
// local sandbox the AI uses. The starter snippet documents the sandbox
// contract (which data is exposed, and how) so the auditor doesn't have to
// guess. Saving creates a `python` analysis with source 'code' in the rail.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ saved: [SavedAnalysis] }>()
const toast = useToast()

// Mirrors the sandbox rule: a table is also a bare variable when its name is
// a valid Python identifier.
const isIdentifier = (name: string) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(name)

function starterCode(): string {
  const names = props.workspace.tables.map((t) => t.name)
  const first = names[0]
  const lines = [
    '# Custom analysis — Python (Polars) running in the local sandbox.',
    '# Imports are blocked; `pl` (Polars) is already available.',
    '#',
    '# Exposed data — every workspace table, full raw rows, as a Polars DataFrame:',
  ]
  if (names.length) {
    for (const name of names) {
      lines.push(`#   tables[${JSON.stringify(name)}]${isIdentifier(name) ? `  or simply: ${name}` : ''}`)
    }
    lines.push(`#   df  — shortcut for the first table (${JSON.stringify(first)})`)
  } else {
    lines.push('#   (this workspace has no tables yet — upload data first)')
    lines.push("#   tables['name']  — each table by name; also a bare variable when the")
    lines.push('#                     name is a valid identifier. `df` is the first table.')
  }
  lines.push(
    '#',
    '# Execution runs on this machine; bounded results may be shown to the configured model.',
    '# Assign your output to `result` (DataFrame, Series, dict, or scalar);',
    '# `print(...)` output shows up below the editor.',
    '',
    first && isIdentifier(first) ? `result = ${first}.head(10)` : 'result = df.head(10)',
    '',
  )
  return lines.join('\n')
}

const title = ref('')
const code = ref(starterCode())
const policy = ref<'exception_rows' | 'informational'>('informational')
const frame = ref<FramePayload | null>(null)
const totalRows = ref(0)
const stdout = ref<string | null>(null)
const runError = ref<string | null>(null)
const running = ref(false)
const saving = ref(false)

// Declared at creation, because it is what turns a row count into a verdict
// once the procedure starts recording results.
const policyOptions = [
  { label: 'Exceptions', value: 'exception_rows', hint: 'Any returned row is a potential exception.' },
  { label: 'Informational', value: 'informational', hint: 'Rows are context; no pass/fail conclusion.' },
]
const policyHint = computed(
  () => policyOptions.find(option => option.value === policy.value)?.hint ?? '',
)

async function run() {
  running.value = true
  runError.value = null
  try {
    const result = await api.post<RunPythonResult>(
      `/api/workspaces/${props.workspace.id}/run-python`,
      { code: code.value },
    )
    frame.value = result.frame
    totalRows.value = result.total_rows
    stdout.value = result.stdout
  } catch (error) {
    runError.value = error instanceof ApiError ? error.message : String(error)
  } finally {
    running.value = false
  }
}

async function save() {
  const name = title.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'A title is required', life: 3000 })
    return
  }
  saving.value = true
  try {
    const created = await api.post<SavedAnalysis>(
      `/api/workspaces/${props.workspace.id}/analyses`,
      {
        kind: 'python',
        title: name,
        spec: { code: code.value },
        viz: { type: 'table' },
        source: 'code',
        outcome_policy: { mode: policy.value },
      },
    )
    emit('saved', created)
    toast.add({ severity: 'success', summary: 'Saved to analyses', detail: name, life: 2500 })
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Save failed', detail, life: 6000 })
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="analysis-editor-head">
    <InputText v-model="title" placeholder="Analysis title" class="title-input" />
    <span class="grow" />
    <Button label="Save" icon="pi pi-save" size="small" :loading="saving" @click="save" />
  </div>

  <p class="intro">
    Write the procedure, preview what it returns, then save it. Saving puts it in
    the rail, where running it records what it concludes.
  </p>

  <div class="policy-row">
    <div class="field">
      <label>Returned rows are</label>
      <SelectButton
        v-model="policy"
        :options="policyOptions"
        optionLabel="label"
        optionValue="value"
        :allowEmpty="false"
        size="small"
      />
    </div>
    <span class="muted">{{ policyHint }}</span>
  </div>

  <div v-if="runError" class="analysis-error">
    <i class="pi pi-exclamation-triangle" /> {{ runError }}
  </div>

  <div class="analysis-code-block">
    <div class="analysis-code-head">
      <span><i class="pi pi-code" /> Python — runs in the local sandbox</span>
      <Button label="Preview" icon="pi pi-eye" size="small" text :loading="running" @click="run" />
    </div>
    <CodeEditor v-model="code" />
    <pre v-if="stdout" class="analysis-stdout">{{ stdout }}</pre>
  </div>

  <div v-if="frame" class="result">
    <p class="muted rows">{{ totalRows.toLocaleString() }} row{{ totalRows === 1 ? '' : 's' }} · preview, not recorded</p>
    <ChartView :frame="frame" :viz="{ type: 'table' }" height="320px" />
  </div>

</template>

<style scoped>
.intro { margin: 0 0 var(--aw-space-3); color: var(--aw-muted); font-size: var(--aw-text-sm); }
.policy-row {
  display: flex;
  align-items: flex-end;
  gap: var(--aw-space-3);
  flex-wrap: wrap;
  margin-bottom: var(--aw-space-3);
}
.policy-row .muted { padding-bottom: 0.4rem; font-size: var(--aw-text-xs); }
.muted { color: var(--aw-muted); }
.rows { font-size: var(--aw-text-sm); margin: 0 0 0.5rem; }
</style>
