<script setup lang="ts">
import { ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'

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
const frame = ref<FramePayload | null>(null)
const totalRows = ref(0)
const stdout = ref<string | null>(null)
const runError = ref<string | null>(null)
const running = ref(false)
const saving = ref(false)

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
  <div class="detail-head">
    <InputText v-model="title" placeholder="Analysis title" class="title-input" />
    <span class="grow" />
    <Button label="Save" icon="pi pi-save" size="small" :loading="saving" @click="save" />
  </div>

  <div v-if="runError" class="err">
    <i class="pi pi-exclamation-triangle" /> {{ runError }}
  </div>

  <div class="code-block">
    <div class="code-head">
      <span><i class="pi pi-code" /> Python — runs in the local sandbox</span>
      <Button label="Run" icon="pi pi-play" size="small" text :loading="running" @click="run" />
    </div>
    <CodeEditor v-model="code" />
    <pre v-if="stdout" class="stdout">{{ stdout }}</pre>
  </div>

  <div v-if="frame" class="result">
    <p class="muted rows">{{ totalRows.toLocaleString() }} row{{ totalRows === 1 ? '' : 's' }}</p>
    <ChartView :frame="frame" :viz="{ type: 'table' }" height="320px" />
  </div>

</template>

<style scoped>
.detail-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
  position: sticky;
  top: -2px;
  z-index: 5;
  background: var(--aw-panel);
  padding: 0.4rem 0;
}
.detail-head .grow { flex: 1; }
.title-input { min-width: 16rem; font-weight: 600; }

.err {
  color: var(--aw-danger);
  background: var(--aw-danger-soft);
  border: 1px solid var(--aw-danger-line);
  border-radius: var(--aw-radius-control);
  padding: 0.6rem 0.85rem;
  margin-bottom: 0.85rem;
}

.code-block { margin-bottom: 1rem; }
.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--aw-text-sm);
  color: var(--aw-muted);
  margin-bottom: 0.25rem;
}
.stdout {
  background: var(--aw-ink-strong);
  color: var(--aw-panel);
  border-radius: var(--aw-radius-control);
  padding: 0.5rem 0.75rem;
  font-size: var(--aw-text-sm);
  overflow-x: auto;
  margin: 0.4rem 0 0;
}
.rows { font-size: var(--aw-text-sm); margin: 0 0 0.5rem; }
</style>
