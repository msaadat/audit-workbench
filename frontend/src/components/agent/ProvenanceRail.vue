<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError } from '../../api'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { ArtifactProvenance, ProvenanceSize } from '../../types'
import { plural, verb } from '../../format'

/**
 * Where a generated work product came from.
 *
 * The first question a reviewer asks about anything an agent wrote is what it
 * actually read — and the second is what it *didn't*. Both are answered from
 * the context manifest the run already persisted, so this rail reports rather
 * than reconstructs. When a sidecar is missing or fails its integrity check it
 * says so; it never shows a partial trail as if it were complete.
 *
 * Attribution is per artifact, which is the granularity the sidecars record.
 * There is no per-paragraph provenance because no per-paragraph record exists.
 */

const props = defineProps<{
  workspaceId: string
  /** e.g. `planning:apm`, `rcm:RCM-9FB041`, `datatest:DAT-1234`. */
  artifactRef: string
}>()

const nav = useWorkspaceNav()
const payload = ref<ArtifactProvenance | null>(null)
const loading = ref(false)
const error = ref('')

const attributed = computed(() =>
  payload.value?.state === 'attributed' ? payload.value : null)
const context = computed(() => attributed.value?.context ?? null)
const selections = computed(() => context.value?.selections ?? [])
const omissions = computed(() => context.value?.omissions ?? [])
const truncations = computed(() => context.value?.truncations ?? [])

/** Documents are the sources a reviewer can actually open and check. */
const documentIdFor = (sourceRef: string) =>
  sourceRef.startsWith('document:') ? sourceRef.slice('document:'.length) : ''

function size(value?: ProvenanceSize) {
  if (!value) return ''
  if (value.media_items) return plural(value.media_items, 'image')
  if (!value.characters) return plural(value.items, 'item')
  return `${value.characters.toLocaleString()} chars · ~${value.estimated_tokens.toLocaleString()} tokens`
}

function shortHash(value?: string | null) {
  if (!value) return ''
  const digest = value.startsWith('sha256:') ? value.slice(7) : value
  return digest.slice(0, 12)
}

function duration(from?: string | null, to?: string | null) {
  if (!from || !to) return ''
  const ms = Date.parse(to) - Date.parse(from)
  if (!Number.isFinite(ms) || ms <= 0) return ''
  const seconds = Math.round(ms / 1000)
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

async function load() {
  if (!props.artifactRef) return
  loading.value = true
  error.value = ''
  try {
    payload.value = await api.get<ArtifactProvenance>(
      `/api/workspaces/${props.workspaceId}/provenance?artifact=${encodeURIComponent(props.artifactRef)}`,
    )
  } catch (failure) {
    error.value = failure instanceof ApiError ? failure.message : String(failure)
    payload.value = null
  } finally {
    loading.value = false
  }
}

watch(() => [props.workspaceId, props.artifactRef].join('|'), load, { immediate: true })
defineExpose({ reload: load })
</script>

<template>
  <aside class="provenance">
    <header>
      <i class="pi pi-shield" />
      <strong>Where this came from</strong>
      <button class="refresh" title="Reload provenance" @click="load"><i class="pi pi-refresh" /></button>
    </header>

    <p v-if="loading" class="note"><i class="pi pi-spin pi-spinner" /> Reading the run record…</p>
    <p v-else-if="error" class="note bad"><i class="pi pi-exclamation-triangle" /> {{ error }}</p>

    <div v-else-if="payload?.state === 'unattributed'" class="card">
      <h6>Not agent-written</h6>
      <div class="body"><p class="muted">{{ payload.reason }}</p></div>
    </div>

    <template v-else-if="attributed">
      <!-- What it read. -->
      <div class="card">
        <h6>Sources in context <span v-if="context?.state === 'available'">{{ selections.length }}</span></h6>
        <div class="body">
          <p v-if="context?.state !== 'available'" class="muted broken">
            <i class="pi pi-exclamation-triangle" />
            Provenance unavailable — {{ context?.reason }}
          </p>
          <template v-else>
            <div v-for="item in selections" :key="item.source_id + item.source_ref" class="source">
              <span class="ic">{{ item.source_type.slice(0, 1).toUpperCase() }}</span>
              <span class="detail">
                <b>{{ item.source_ref }}</b>
                <small>{{ size(item.supplied_size) }} · {{ item.representation.kind.replaceAll('_', ' ') }}</small>
                <button
                  v-if="documentIdFor(item.source_ref)"
                  class="jump"
                  @click="nav.push('documents', { doc: documentIdFor(item.source_ref) })"
                >Open the document</button>
              </span>
            </div>
            <p v-if="!selections.length" class="muted">Nothing was supplied to the model for this step.</p>
            <p v-if="context.supplied_size" class="total">Total supplied: {{ size(context.supplied_size) }}</p>
          </template>
        </div>
      </div>

      <!-- What it did not read. This card is the point of the rail. -->
      <div v-if="context?.state === 'available'" class="card">
        <h6>Not supplied <span>{{ omissions.length + truncations.length }}</span></h6>
        <div class="body">
          <div v-for="item in truncations" :key="'t' + item.source_id + item.source_ref" class="source">
            <span class="ic warn">!</span>
            <span class="detail">
              <b>{{ item.source_ref }}</b>
              <small>Truncated — {{ item.reason }}</small>
              <small class="muted">{{ size(item.original_size) }} → {{ size(item.supplied_size) }}</small>
            </span>
          </div>
          <div v-for="item in omissions" :key="'o' + item.source_id + (item.source_ref ?? '')" class="source">
            <span class="ic muted-ic">–</span>
            <span class="detail">
              <b>{{ item.source_ref || item.source_id }}</b>
              <small>{{ item.reason }}</small>
            </span>
          </div>
          <p v-if="!omissions.length && !truncations.length" class="muted">
            Nothing was omitted or truncated — the model saw every candidate source in full.
          </p>
        </div>
      </div>

      <!-- How it was generated. -->
      <div class="card">
        <h6>Generation</h6>
        <div class="body kv">
          <span>Step</span><b>{{ attributed.unit.stage_title }}</b>
          <span>Capability</span><code>{{ attributed.unit.capability }}</code>
          <template v-if="attributed.model.model">
            <span>Model</span><b>{{ attributed.model.provider }} / {{ attributed.model.model }}</b>
          </template>
          <template v-if="attributed.model.usage?.calls">
            <span>Calls</span>
            <b>
              {{ attributed.model.usage.calls }} ·
              {{ (attributed.model.usage.prompt_tokens ?? 0).toLocaleString() }} prompt tokens
              <em v-if="attributed.model.scope === 'worker_across_run'" class="scope">for this worker across the run</em>
            </b>
          </template>
          <template v-if="duration(attributed.unit.started_at, attributed.unit.finished_at)">
            <span>Took</span><b>{{ duration(attributed.unit.started_at, attributed.unit.finished_at) }}</b>
          </template>
          <template v-if="context?.manifest_hash">
            <span>Context</span><code>{{ shortHash(context.manifest_hash) }}…</code>
          </template>
          <template v-if="attributed.receipt.state === 'available'">
            <span>Receipt</span><code>{{ shortHash(attributed.receipt.receipt_hash) }}…</code>
            <span>Committed</span><b>revision {{ attributed.receipt.workspace_revision_after }}</b>
          </template>
          <template v-else>
            <span>Receipt</span><b class="broken">{{ attributed.receipt.reason }}</b>
          </template>
        </div>
      </div>

      <!-- What this rail does and does not prove. -->
      <div class="card">
        <h6>Trust</h6>
        <div class="body">
          <p v-if="context?.state === 'available' && !omissions.length && !truncations.length" class="verdict ok">
            <i class="pi pi-check-circle" /> Every candidate source was supplied in full.
          </p>
          <p v-else-if="context?.state === 'available'" class="verdict warn">
            <i class="pi pi-exclamation-triangle" />
            {{ plural(omissions.length, 'source') }} {{ verb(omissions.length, 'was', 'were') }} not supplied and {{ truncations.length }} were cut short.
            Anything resting on them is unsupported.
          </p>
          <p v-else class="verdict warn">
            <i class="pi pi-exclamation-triangle" /> No usable context record — what this step read cannot be established.
          </p>
          <p class="muted">
            The generated text itself is not reproduced here; provenance identifies it by hash
            <code v-if="attributed.proposal.payload_hash">{{ shortHash(attributed.proposal.payload_hash) }}…</code>
          </p>
        </div>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.provenance { display: flex; flex-direction: column; gap: 0.55rem; min-width: 0; }
.provenance > header { display: flex; align-items: center; gap: 0.4rem; }
.provenance > header strong { flex: 1; font-size: var(--aw-text-sm); }
.provenance > header > i { color: var(--aw-teal); font-size: var(--aw-text-base); }
.refresh { padding: 0.15rem 0.3rem; border: 0; border-radius: var(--aw-radius-control); background: transparent; color: var(--aw-muted); cursor: pointer; }
.refresh:hover { background: var(--aw-raised); color: var(--aw-ink); }

.card { border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); overflow: hidden; }
.card h6 {
  display: flex;
  gap: 0.4rem;
  margin: 0;
  padding: 0.35rem 0.55rem;
  border-bottom: 1px solid var(--aw-border);
  background: var(--aw-raised);
  color: var(--aw-muted);
  font-family: var(--aw-font-mono);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
}
.card h6 span { margin-left: auto; font-variant-numeric: tabular-nums; }
.card .body { display: grid; gap: 0.4rem; padding: 0.5rem 0.55rem; font-size: var(--aw-text-xs); }

.source { display: flex; gap: 0.4rem; align-items: flex-start; }
.ic {
  flex: 0 0 1rem; height: 1rem;
  display: grid; place-items: center;
  border-radius: var(--aw-radius-control);
  background: var(--aw-teal-soft); color: var(--aw-teal);
  font-size: var(--aw-text-2xs); font-weight: 700;
}
.ic.warn { background: var(--aw-warn-soft); color: var(--aw-warn); }
.ic.muted-ic { background: var(--aw-raised); color: var(--aw-muted); }
.detail { display: grid; gap: 0.05rem; min-width: 0; }
.detail b { font-size: var(--aw-text-xs); font-weight: 600; overflow-wrap: anywhere; }
.detail small { color: var(--aw-muted); font-size: var(--aw-text-2xs); line-height: 1.35; }
.jump { justify-self: start; margin-top: 0.15rem; padding: 0; border: 0; background: none; color: var(--aw-teal); font-size: var(--aw-text-2xs); text-decoration: underline; cursor: pointer; }

.total { margin: 0.15rem 0 0; padding-top: 0.35rem; border-top: 1px dashed var(--aw-border); color: var(--aw-muted); font-size: var(--aw-text-2xs); }

.kv { grid-template-columns: auto minmax(0, 1fr); gap: 0.25rem 0.6rem; align-items: baseline; }
.kv > span { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); }
.kv > b { font-size: var(--aw-text-xs); font-weight: 600; overflow-wrap: anywhere; }
.kv code, .body code { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); overflow-wrap: anywhere; }
.scope { display: block; color: var(--aw-muted); font-size: var(--aw-text-2xs); font-style: normal; font-weight: 400; }

.verdict { display: flex; align-items: flex-start; gap: 0.35rem; margin: 0; font-size: var(--aw-text-xs); line-height: 1.45; }
.verdict.ok { color: var(--aw-ok); }
.verdict.warn { color: var(--aw-warn); }
.verdict i { padding-top: 0.1rem; }

.note { display: flex; align-items: center; gap: 0.35rem; margin: 0; padding: 0.5rem 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.note.bad { color: var(--aw-danger); }
.muted { margin: 0; color: var(--aw-muted); line-height: 1.45; }
.broken { color: var(--aw-warn); }
</style>
