<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api, ApiError } from '../../api'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { ArtifactProvenance, AuditDocument, ProvenanceSize } from '../../types'
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
const documents = ref<AuditDocument[]>([])
const loading = ref(false)
const error = ref('')

const attributed = computed(() =>
  payload.value?.state === 'attributed' ? payload.value : null)
const context = computed(() => attributed.value?.context ?? null)
const selections = computed(() => context.value?.selections ?? [])
const truncations = computed(() => context.value?.truncations ?? [])

/**
 * Not supplying a source is three different facts, and reporting them as one
 * list read as four failures where three of them were decisions. A selector
 * that declined a candidate chose a scope; a limit that bit is a capacity
 * fact; an absent source is neither. Reasons are authored sentences from the
 * context resolver rather than stable codes, so they are matched loosely and
 * anything unrecognised falls through to the mildest reading.
 */
type OmissionKind = 'scope' | 'limit' | 'unavailable'

function omissionKind(reason: string): OmissionKind {
  const text = reason.toLowerCase()
  if (text.includes('did not match') || text.includes('selector item limit')) return 'scope'
  if (text.includes('limit')) return 'limit'
  return 'unavailable'
}

const omissions = computed(() => {
  const order: OmissionKind[] = ['scope', 'limit', 'unavailable']
  return (context.value?.omissions ?? [])
    .map(item => ({ ...item, kind: omissionKind(item.reason ?? '') }))
    .sort((left, right) => order.indexOf(left.kind) - order.indexOf(right.kind))
})
const countOf = (kind: OmissionKind) =>
  computed(() => omissions.value.filter(item => item.kind === kind).length)
const scoped = countOf('scope')
const overLimit = countOf('limit')
const unavailable = countOf('unavailable')
const truncated = computed(() => truncations.value.length)
/** Nothing the step selected was lost — scope and absence are not losses. */
const intact = computed(() => !truncated.value && !overLimit.value)

/** Documents are the sources a reviewer can actually open and check. */
const documentIdFor = (sourceRef: string) => {
  if (!sourceRef.startsWith('document:')) return ''
  // Page-scoped refs read `document:<id>:page:<n>` and name the same file.
  return sourceRef.split(':')[1] ?? ''
}

/**
 * What the auditor calls this source. A document is named by the file as it
 * arrived — "Procurement SOP Extracts.docx" — because `document:30393b95dc`
 * identifies it to the system and to nobody else. Every other ref already
 * reads as itself and is left alone.
 */
function sourceLabel(sourceRef: string) {
  const id = documentIdFor(sourceRef)
  if (!id) return sourceRef
  const found = documents.value.find(item => item.id === id)
  return found?.source || found?.title || sourceRef
}

/**
 * Sources, in the order a reviewer looks for them.
 *
 * A flat manifest list put four named documents twentieth out of twenty-one
 * rows, behind twelve table representations nobody scans. The files an
 * auditor recognises and can open lead; the tables collapse behind a count,
 * because how many were profiled is the fact and which columns went into each
 * profile is not; templates and prior drafts come last, because they say what
 * the step was rather than what it rested on.
 */
type GroupKey = 'documents' | 'tables' | 'other'

const GROUP_LABEL: Record<GroupKey, string> = {
  documents: 'Documents',
  tables: 'Tables',
  other: 'Other context',
}

function groupKeyFor(sourceType: string, sourceRef: string): GroupKey {
  if (sourceType === 'documents' || sourceRef.startsWith('document:')) return 'documents'
  if (sourceType === 'tables' || sourceRef.startsWith('table:')) return 'tables'
  return 'other'
}

/** A short type tag, so a file reads as a file before it is read at all. */
function badgeFor(group: GroupKey, label: string) {
  if (group === 'tables') return 'TBL'
  if (group !== 'documents') return 'CTX'
  if (/\.pdf$/i.test(label)) return 'PDF'
  if (/\.docx?$/i.test(label)) return 'DOC'
  if (/\.(png|jpe?g|webp|bmp|tiff?)$/i.test(label)) return 'IMG'
  if (/\.(xlsx?|csv|tsv)$/i.test(label)) return 'XLS'
  return 'FILE'
}

/**
 * The name to lead a row with, by what the group makes it.
 *
 * A table keeps its workspace name verbatim — `po_data` is what the Data tab
 * calls it, and prettifying it would break the match. Supporting context has
 * no name worth showing: `template:apm` says less than "Artifact template",
 * which is what the representation already records, so the kind leads and the
 * ref moves to the line underneath.
 */
function rowLabel(group: GroupKey, ref: string, kind: string) {
  if (group === 'documents') return sourceLabel(ref)
  const tail = ref.includes(':') ? ref.slice(ref.indexOf(':') + 1) : ref
  if (group === 'tables') return tail
  const readable = kind.replaceAll('_', ' ').trim()
  return readable ? readable[0].toUpperCase() + readable.slice(1) : tail
}

function addSize(total: ProvenanceSize | undefined, next: ProvenanceSize | undefined) {
  if (!next) return total
  if (!total) return { ...next }
  return {
    characters: (total.characters ?? 0) + (next.characters ?? 0),
    estimated_tokens: (total.estimated_tokens ?? 0) + (next.estimated_tokens ?? 0),
    items: (total.items ?? 0) + (next.items ?? 0),
    media_items: (total.media_items ?? 0) + (next.media_items ?? 0),
  } as ProvenanceSize
}

interface GroupedSource {
  ref: string
  label: string
  badge: string
  kinds: string[]
  size?: ProvenanceSize
  documentId: string
}

interface SourceGroup {
  key: GroupKey
  label: string
  rows: GroupedSource[]
  collapsible: boolean
}

const sourceGroups = computed<SourceGroup[]>(() => {
  const buckets = new Map<GroupKey, Map<string, GroupedSource>>()
  for (const item of selections.value) {
    const key = groupKeyFor(item.source_type, item.source_ref)
    // One row per source, not per representation: a table selected once for
    // its metadata and once for its profile is one table.
    const identity = documentIdFor(item.source_ref) || item.source_ref
    const rows = buckets.get(key) ?? new Map<string, GroupedSource>()
    buckets.set(key, rows)
    const existing = rows.get(identity)
    const kind = item.representation.kind.replaceAll('_', ' ')
    if (existing) {
      if (!existing.kinds.includes(kind)) existing.kinds.push(kind)
      existing.size = addSize(existing.size, item.supplied_size)
      continue
    }
    const label = rowLabel(key, item.source_ref, item.representation.kind)
    rows.set(identity, {
      ref: item.source_ref,
      label,
      badge: badgeFor(key, label),
      kinds: [kind],
      size: item.supplied_size ? { ...item.supplied_size } : undefined,
      documentId: documentIdFor(item.source_ref),
    })
  }
  const order: GroupKey[] = ['documents', 'tables', 'other']
  return order
    .filter(key => buckets.get(key)?.size)
    .map(key => ({
      key,
      label: GROUP_LABEL[key],
      rows: [...(buckets.get(key) as Map<string, GroupedSource>).values()],
      // Documents stay open — they are the reason anyone opened this panel.
      collapsible: key !== 'documents',
    }))
})

/** What a collapsed group says about itself: the count is the fact. */
function groupSummary(group: { key: GroupKey; rows: unknown[] }) {
  const total = group.rows.length
  if (group.key === 'tables') return plural(total, 'table')
  if (group.key === 'documents') return plural(total, 'document')
  return `${total} other ${total === 1 ? 'source' : 'sources'}`
}

/**
 * The same three groups, for what the step did not read.
 *
 * Withholding carries a second dimension the supplied side does not — why —
 * and the two correlate without matching: documents are declined by scope,
 * tables fall to size limits, an optional pack is simply absent. So the rows
 * group by kind of source, as above, and each group states its reason once
 * when every row shares it rather than repeating the phrase down the column.
 */
type WithheldKind = 'truncated' | 'limit' | 'unavailable' | 'scope'

const WITHHELD_PHRASE: Record<WithheldKind, string> = {
  truncated: 'Cut short',
  limit: 'Past the size limit',
  unavailable: 'Not available',
  scope: "Outside this step's scope",
}
// Losses first, decisions last: what went missing outranks what was declined.
const WITHHELD_ORDER: WithheldKind[] = ['truncated', 'limit', 'unavailable', 'scope']

interface WithheldRow {
  ref: string
  label: string
  badge: string
  kind: WithheldKind
  note: string
  documentId: string
}

interface WithheldGroup {
  key: GroupKey
  label: string
  rows: WithheldRow[]
  collapsible: boolean
  /** The one reason shared by every row, when there is one. */
  reason: string
}

function withheldLabel(group: GroupKey, ref: string, sourceId: string) {
  if (group === 'documents') return sourceLabel(ref)
  if (group === 'tables') return ref.includes(':') ? ref.slice(ref.indexOf(':') + 1) : ref
  // A source-level omission has no ref at all — the id is the only name it has.
  const name = (ref ? ref.slice(ref.indexOf(':') + 1) : sourceId).replaceAll('_', ' ').trim()
  return name ? name[0].toUpperCase() + name.slice(1) : sourceId
}

const withheldGroups = computed<WithheldGroup[]>(() => {
  const raw: { ref: string; sourceId: string; kind: WithheldKind; note: string }[] = [
    ...truncations.value.map(item => ({
      ref: item.source_ref,
      sourceId: item.source_id,
      kind: 'truncated' as WithheldKind,
      note: `${size(item.original_size)} → ${size(item.supplied_size)}`,
    })),
    ...(context.value?.omissions ?? []).map(item => ({
      ref: item.source_ref ?? '',
      sourceId: item.source_id,
      kind: omissionKind(item.reason ?? ''),
      note: '',
    })),
  ]

  const buckets = new Map<GroupKey, Map<string, WithheldRow>>()
  for (const item of raw) {
    const key = groupKeyFor('', item.ref)
    const identity = `${documentIdFor(item.ref) || item.ref || item.sourceId}|${item.kind}`
    const rows = buckets.get(key) ?? new Map<string, WithheldRow>()
    buckets.set(key, rows)
    if (rows.has(identity)) continue
    const label = withheldLabel(key, item.ref, item.sourceId)
    rows.set(identity, {
      ref: item.ref || item.sourceId,
      label,
      badge: badgeFor(key, label),
      kind: item.kind,
      note: item.note,
      documentId: documentIdFor(item.ref),
    })
  }

  const order: GroupKey[] = ['documents', 'tables', 'other']
  return order
    .filter(key => buckets.get(key)?.size)
    .map(key => {
      const rows = [...(buckets.get(key) as Map<string, WithheldRow>).values()]
        .sort((left, right) =>
          WITHHELD_ORDER.indexOf(left.kind) - WITHHELD_ORDER.indexOf(right.kind))
      const kinds = new Set(rows.map(row => row.kind))
      return {
        key,
        label: GROUP_LABEL[key],
        rows,
        collapsible: key !== 'documents',
        reason: kinds.size === 1 ? WITHHELD_PHRASE[rows[0].kind] : '',
      }
    })
})

const withheldExpanded = ref<GroupKey[]>([])
const isWithheldOpen = (group: WithheldGroup) =>
  !group.collapsible || withheldExpanded.value.includes(group.key)
function toggleWithheld(group: WithheldGroup) {
  if (!group.collapsible) return
  withheldExpanded.value = withheldExpanded.value.includes(group.key)
    ? withheldExpanded.value.filter(key => key !== group.key)
    : [...withheldExpanded.value, group.key]
}

const expanded = ref<GroupKey[]>([])
const isOpen = (group: SourceGroup) =>
  !group.collapsible || expanded.value.includes(group.key)
function toggleGroup(group: SourceGroup) {
  if (!group.collapsible) return
  expanded.value = expanded.value.includes(group.key)
    ? expanded.value.filter(key => key !== group.key)
    : [...expanded.value, group.key]
}

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
    // The document list only supplies display names. A failure to load it
    // leaves refs showing as ids, which is the old behaviour, so it must not
    // fail the panel itself.
    const [provenance, catalogue] = await Promise.all([
      api.get<ArtifactProvenance>(
        `/api/workspaces/${props.workspaceId}/provenance?artifact=${encodeURIComponent(props.artifactRef)}`,
      ),
      api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspaceId}/documents`)
        .then(result => result.items)
        .catch(() => [] as AuditDocument[]),
    ])
    payload.value = provenance
    documents.value = catalogue
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
            <section v-for="group in sourceGroups" :key="group.key" class="group">
              <button
                v-if="group.collapsible"
                class="group-head toggle"
                :aria-expanded="isOpen(group)"
                @click="toggleGroup(group)"
              >
                <i :class="isOpen(group) ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" />
                <span>{{ groupSummary(group) }}</span>
              </button>
              <div v-else class="group-head">
                <span>{{ group.label }}</span>
                <b>{{ group.rows.length }}</b>
              </div>
              <div v-if="isOpen(group)" class="group-body">
                <div v-for="row in group.rows" :key="row.ref" class="source">
                  <span class="ic badge">{{ row.badge }}</span>
                  <span class="detail">
                    <b :title="row.ref">{{ row.label }}</b>
                    <!-- Supporting context already leads with its kind, so the
                         ref is the line that adds something underneath it. -->
                    <small>
                      {{ [size(row.size), ...(group.key === 'other' ? [row.ref] : row.kinds)].filter(Boolean).join(' · ') }}
                    </small>
                    <button
                      v-if="row.documentId"
                      class="jump"
                      @click="nav.push('documents', { doc: row.documentId })"
                    >Open the document</button>
                  </span>
                </div>
              </div>
            </section>
            <p v-if="!selections.length" class="muted">Nothing was supplied to the model for this step.</p>
            <p v-if="context.supplied_size" class="total">Total supplied: {{ size(context.supplied_size) }}</p>
          </template>
        </div>
      </div>

      <!-- What it did not read. This card is the point of the rail. -->
      <div v-if="context?.state === 'available'" class="card">
        <h6>Not supplied <span>{{ omissions.length + truncations.length }}</span></h6>
        <div class="body">
          <section v-for="group in withheldGroups" :key="group.key" class="group">
            <button
              v-if="group.collapsible"
              class="group-head toggle"
              :aria-expanded="isWithheldOpen(group)"
              @click="toggleWithheld(group)"
            >
              <i :class="isWithheldOpen(group) ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" />
              <span>{{ groupSummary(group) }}</span>
              <em v-if="group.reason">{{ group.reason }}</em>
            </button>
            <div v-else class="group-head">
              <span>{{ group.label }}</span>
              <em v-if="group.reason">{{ group.reason }}</em>
              <b v-else>{{ group.rows.length }}</b>
            </div>
            <div v-if="isWithheldOpen(group)" class="group-body">
              <div v-for="row in group.rows" :key="row.ref + row.kind" class="source">
                <span
                  class="ic badge"
                  :class="row.kind === 'truncated' ? 'warn' : row.kind === 'scope' ? 'scope-ic' : 'muted-ic'"
                >{{ row.badge }}</span>
                <span class="detail">
                  <b :title="row.ref">{{ row.label }}</b>
                  <!-- The reason repeats per row only where the group is mixed. -->
                  <small v-if="!group.reason">{{ WITHHELD_PHRASE[row.kind] }}</small>
                  <small v-if="row.note" class="muted">{{ row.note }}</small>
                  <button
                    v-if="row.documentId"
                    class="jump"
                    @click="nav.push('documents', { doc: row.documentId })"
                  >Open the document</button>
                </span>
              </div>
            </div>
          </section>
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
          <!-- Four different facts, reported as four. A source the selector
               declined is a scope decision and an absent optional source is a
               fact about the engagement; neither casts doubt on the work
               product, and one undifferentiated warning over all of them read
               as a disclaimer on it. "Unsupported" is reserved for material
               that was genuinely cut short. -->
          <template v-if="context?.state === 'available'">
            <p v-if="intact" class="verdict ok">
              <i class="pi pi-check-circle" />
              Everything this step selected was supplied in full.
            </p>
            <p v-if="truncated" class="verdict warn">
              <i class="pi pi-exclamation-triangle" />
              {{ plural(truncated, 'source') }} {{ verb(truncated, 'was', 'were') }} cut short.
              Anything resting on the missing part is unsupported.
            </p>
            <p v-if="overLimit" class="verdict warn">
              <i class="pi pi-exclamation-triangle" />
              {{ plural(overLimit, 'source') }} did not fit within the size limit.
            </p>
            <p v-if="unavailable" class="verdict muted-verdict">
              <i class="pi pi-minus-circle" />
              {{ plural(unavailable, 'source') }} {{ verb(unavailable, 'was', 'were') }} not available.
            </p>
            <p v-if="scoped" class="verdict muted-verdict">
              <i class="pi pi-filter" />
              {{ plural(scoped, 'source') }} {{ verb(scoped, 'was', 'were') }} outside this step's scope.
            </p>
          </template>
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

.group { display: grid; gap: 0.35rem; }
.group + .group { padding-top: 0.4rem; border-top: 1px dashed var(--aw-border); }
.group-head {
  display: flex; align-items: center; gap: 0.35rem;
  width: 100%; padding: 0;
  border: 0; background: none; text-align: left;
  color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs);
  font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
}
.group-head b { margin-left: auto; font-variant-numeric: tabular-nums; }
/* The reason a whole group was withheld, stated once beside its count. */
.group-head em { margin-left: auto; font-style: normal; font-weight: 400; text-transform: none; letter-spacing: 0; }
.group-head.toggle { cursor: pointer; }
.group-head.toggle:hover { color: var(--aw-ink); }
.group-head.toggle:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; border-radius: 3px; }
.group-head > i { font-size: 0.6rem; }
.group-body { display: grid; gap: 0.4rem; }

.source { display: flex; gap: 0.4rem; align-items: flex-start; }
.ic {
  flex: 0 0 1rem; height: 1rem;
  display: grid; place-items: center;
  border-radius: var(--aw-radius-control);
  background: var(--aw-teal-soft); color: var(--aw-teal);
  font-size: var(--aw-text-2xs); font-weight: 700;
}
/* A file-type tag needs room for three letters; a status glyph does not. */
.ic.badge {
  flex-basis: 2.1rem; height: 1.05rem;
  font-family: var(--aw-font-mono);
  font-size: 0.55rem; letter-spacing: 0.03em;
}
.ic.warn { background: var(--aw-warn-soft); color: var(--aw-warn); }
.ic.muted-ic { background: var(--aw-raised); color: var(--aw-muted); }
/* A scope decision is neither a warning nor a gap, so it reads as neither. */
.ic.scope-ic { background: var(--aw-raised); color: var(--aw-ink-soft); }
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
.verdict.muted-verdict { color: var(--aw-muted); }
.verdict i { padding-top: 0.1rem; }

.note { display: flex; align-items: center; gap: 0.35rem; margin: 0; padding: 0.5rem 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.note.bad { color: var(--aw-danger); }
.muted { margin: 0; color: var(--aw-muted); line-height: 1.45; }
.broken { color: var(--aw-warn); }
</style>
