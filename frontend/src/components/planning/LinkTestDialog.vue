<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../../api'
import type { DataTest, DocTest } from '../../types'

/**
 * Linking a test that already exists to an RCM row.
 *
 * `Add test` only ever created: two forms and a generation prompt, all of
 * which end in a new test. A test written before its row existed — or written
 * against the wrong row — had no way back to the matrix except opening the
 * test's own definition form and setting the RCM field there, which is the
 * long way round from a row that says it has no coverage.
 *
 * The link is one field on the test (`rcm_id`), so this dialog is a picker
 * over both registers and a PATCH. A test already carrying another row is
 * shown with the row it carries: linking it here moves it, it does not copy.
 */

const props = defineProps<{
  modelValue: boolean
  workspaceId: string
  rcmId: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; linked: [] }>()

const toast = useToast()

type Entry = {
  key: string
  kind: 'data' | 'document'
  id: string
  title: string
  subtitle: string
  rcmId: string | null
}

const loading = ref(false)
const linking = ref(false)
const entries = ref<Entry[]>([])
const selected = ref<Entry | null>(null)
const query = ref('')

const visible = computed({
  get: () => props.modelValue,
  set: value => emit('update:modelValue', value),
})

/** Tests already on this row are not offered; they are already the answer. */
const available = computed(() => entries.value.filter(entry => entry.rcmId !== props.rcmId))
const matches = computed(() => {
  const term = query.value.trim().toLowerCase()
  if (!term) return available.value
  return available.value.filter(entry => `${entry.id} ${entry.title} ${entry.subtitle} ${entry.rcmId ?? ''}`
    .toLowerCase().includes(term))
})
/** Unlinked tests first: an exploratory test is the likeliest thing to adopt. */
const unlinked = computed(() => matches.value.filter(entry => !entry.rcmId))
const elsewhere = computed(() => matches.value.filter(entry => entry.rcmId))
const alreadyHere = computed(() => entries.value.filter(entry => entry.rcmId === props.rcmId).length)

function fail(summary: string, error: unknown) {
  toast.add({
    severity: 'error',
    summary,
    detail: error instanceof ApiError ? error.message : String(error),
    life: 6000,
  })
}

async function load() {
  loading.value = true
  selected.value = null
  query.value = ''
  try {
    const [data, docs] = await Promise.all([
      api.get<{ items: DataTest[] }>(`/api/workspaces/${props.workspaceId}/data-tests`),
      api.get<{ items: DocTest[] }>(`/api/workspaces/${props.workspaceId}/doc-tests`),
    ])
    entries.value = [
      ...data.items.map((item): Entry => ({
        key: `data:${item.id}`,
        kind: 'data',
        id: item.id,
        title: item.title,
        subtitle: item.objective || item.table_refs.join(', '),
        rcmId: item.rcm_id,
      })),
      ...docs.items.map((item): Entry => ({
        key: `document:${item.id}`,
        kind: 'document',
        id: item.id,
        title: item.title,
        subtitle: item.objective || String(item.kind ?? ''),
        rcmId: item.rcm_id,
      })),
    ]
  } catch (error) {
    entries.value = []
    fail('Could not load the tests', error)
  } finally { loading.value = false }
}

// Immediate, so a dialog mounted already open — a test harness, or a parent
// that renders it only once it is wanted — still reads the registers.
watch(() => props.modelValue, open => { if (open) void load() }, { immediate: true })

async function link() {
  const entry = selected.value
  if (!entry) return
  linking.value = true
  try {
    const path = entry.kind === 'data' ? 'data-tests' : 'doc-tests'
    await api.patch(`/api/workspaces/${props.workspaceId}/${path}/${entry.id}`, { rcm_id: props.rcmId })
    toast.add({
      severity: 'success',
      summary: entry.rcmId ? `Moved to ${props.rcmId}` : `Linked to ${props.rcmId}`,
      life: 2200,
    })
    visible.value = false
    emit('linked')
  } catch (error) { fail('Could not link the test', error) }
  finally { linking.value = false }
}
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    :header="`Link an existing test to ${rcmId}`"
    :style="{ width: 'min(46rem, 94vw)' }"
  >
    <div class="link-body">
      <InputText v-model="query" placeholder="Search tests by title, objective or id" size="small" />

      <p v-if="loading" class="muted">Loading the registers…</p>
      <p v-else-if="!available.length" class="muted">
        There is no other test to link.
        <template v-if="alreadyHere">Every test in the engagement is already on this row.</template>
      </p>
      <p v-else-if="!matches.length" class="muted">No test matches that search.</p>

      <div v-else class="groups">
        <section v-if="unlinked.length">
          <p class="aw-label">Not linked to a row · {{ unlinked.length }}</p>
          <button
            v-for="entry in unlinked"
            :key="entry.key"
            type="button"
            class="entry"
            :aria-pressed="selected?.key === entry.key"
            @click="selected = entry"
          >
            <i :class="entry.kind === 'data' ? 'pi pi-chart-bar' : 'pi pi-file-check'" />
            <span class="entry-text">
              <strong>{{ entry.title }}</strong>
              <small>{{ entry.subtitle }}</small>
            </span>
          </button>
        </section>

        <!-- A test carries one row. Offering these is the point — a test
             written against the wrong row is the other half of this gap —
             but the move is stated, not implied. -->
        <section v-if="elsewhere.length">
          <p class="aw-label">Linked to another row · {{ elsewhere.length }}</p>
          <button
            v-for="entry in elsewhere"
            :key="entry.key"
            type="button"
            class="entry"
            :aria-pressed="selected?.key === entry.key"
            @click="selected = entry"
          >
            <i :class="entry.kind === 'data' ? 'pi pi-chart-bar' : 'pi pi-file-check'" />
            <span class="entry-text">
              <strong>{{ entry.title }}</strong>
              <small>{{ entry.subtitle }}</small>
            </span>
            <code class="moves">{{ entry.rcmId }}</code>
          </button>
        </section>
      </div>

      <p v-if="selected?.rcmId" class="warn">
        This test covers {{ selected.rcmId }}. Linking it here moves it; that row loses the coverage.
      </p>
    </div>

    <template #footer>
      <Button label="Cancel" severity="secondary" outlined @click="visible = false" />
      <Button
        :label="selected?.rcmId ? 'Move to this row' : 'Link test'"
        icon="pi pi-link"
        :disabled="!selected"
        :loading="linking"
        @click="link"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.link-body { display: grid; gap: .85rem; }
.groups { display: grid; gap: 1rem; max-height: 26rem; overflow-y: auto; }
.groups section { display: grid; gap: .35rem; }
.entry {
  display: flex; align-items: center; gap: .6rem; width: 100%; text-align: left;
  padding: .6rem .7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-canvas); cursor: pointer;
}
.entry:hover { border-color: var(--aw-accent); }
.entry[aria-pressed="true"] { border-color: var(--aw-accent); background: var(--aw-panel); }
.entry i { color: var(--aw-muted); }
.entry-text { display: grid; gap: .1rem; min-width: 0; flex: 1; }
.entry-text small { color: var(--aw-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.moves { color: var(--aw-muted); font-size: .78rem; }
.muted { color: var(--aw-muted); }
.warn { color: var(--aw-muted); font-size: .85rem; }
</style>
