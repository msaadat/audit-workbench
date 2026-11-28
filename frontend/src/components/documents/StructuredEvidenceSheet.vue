<script setup lang="ts">
import { computed } from 'vue'

import { sentenceCase } from '../../format'
import type { AuditDocument, DocumentAnalysisCitation, DocumentSchemaRef } from '../../types'

/**
 * Structured fields extracted from a document by the model, rendered as clean,
 * scannable field cards with inline page citations and cross-document links.
 *
 * Used in two places: compact inside the Reading pane beside the original PDF,
 * and full-width under the viewer's `Fields` toggle when reading multi-record
 * extractions.
 */

const props = defineProps<{
  records: Array<Record<string, unknown>>
  schema?: DocumentSchemaRef | null
  citations?: DocumentAnalysisCitation[]
  /** Whether the extraction validated against its schema. */
  validated?: boolean
  /** Compact mode for embedding directly inside the Reading pane. */
  compact?: boolean
  /** Workspace documents, used to turn matching reference values into jump links. */
  documents?: AuditDocument[]
  /** Current document id, excluded from cross-document links. */
  currentDocumentId?: string
  /** Currently hovered or focused highlight key (`recIdx:fieldName:occ`). */
  activeKey?: string | null
}>()

const emit = defineEmits<{
  cite: [citation: DocumentAnalysisCitation]
  'open-document': [documentId: string]
  hover: [fieldKey: string | null]
  'focus-field': [payload: { key: string; citation: DocumentAnalysisCitation | null }]
}>()

interface Field {
  fieldKey: string
  rawKey: string
  key: string
  label: string
  value: string
  citation: DocumentAnalysisCitation | null
  page: number | null
  linkedDoc: { id: string; title: string } | null
  prominent: boolean
}
interface Group { name: string; label: string; fields: Field[] }

const ACRONYMS: Record<string, string> = {
  id: 'ID',
  po: 'PO',
  grn: 'GRN',
  pr: 'PR',
  req: 'Req',
  ntn: 'NTN',
  gst: 'GST',
  vat: 'VAT',
  iban: 'IBAN',
  fx: 'FX',
  gl: 'GL',
  pkr: 'PKR',
  usd: 'USD',
  eur: 'EUR',
  gbp: 'GBP',
  gl_code: 'GL code',
}

function humanize(raw: string): string {
  const words = raw.replace(/^local\./, '').split(/[_\s]+/).filter(Boolean)
  if (!words.length) return raw
  return words.map((word, index) => {
    const lower = word.toLowerCase()
    if (ACRONYMS[lower]) return ACRONYMS[lower]
    return index === 0 ? lower[0].toUpperCase() + lower.slice(1) : lower
  }).join(' ')
}

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(display).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const citationById = computed(() => new Map(
  (props.citations ?? []).map(citation => [String(citation.id), citation]),
))

function normalizeToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '')
}

/** Match an extracted identifier value (e.g. `PO2024004`, `GRN2024004`) to another workspace document. */
function findLinkedDocument(rawKey: string, value: string): { id: string; title: string } | null {
  const docs = props.documents
  if (!docs?.length) return null
  const keyLower = rawKey.toLowerCase()
  const looksLikeRef = /(reference|ref|number|order|grn|requisition|voucher|invoice|po_)/.test(keyLower)
  if (!looksLikeRef) return null
  const token = normalizeToken(value)
  if (token.length < 5 || !/\d/.test(token) || !/[a-z]/.test(token)) return null
  for (const doc of docs) {
    if (doc.id === props.currentDocumentId) continue
    const hay = normalizeToken(`${doc.source} ${doc.title}`)
    if (hay.includes(token)) {
      return { id: doc.id, title: doc.source || doc.title }
    }
  }
  return null
}

function isProminent(rawKey: string): boolean {
  return /(amount|total|net_payable|value|sum|balance)/i.test(rawKey)
}

/**
 * One record's fields, whichever shape the extraction wrote them in.
 *
 * A schema-guided extraction states `{fields: [{name, value, citation}]}`; an
 * older profile-specific one states a plain object.
 */
function fieldsOf(record: Record<string, unknown>, recIndex: number): Field[] {
  const seen = new Map<string, number>()
  const nextKey = (rawKey: string) => {
    const occ = seen.get(rawKey) ?? 0
    seen.set(rawKey, occ + 1)
    return `${recIndex}:${rawKey}:${occ}`
  }
  const listed = record.fields
  if (Array.isArray(listed)) {
    return listed.map(item => {
      const entry = item as Record<string, unknown>
      const rawKey = String(entry.name ?? '')
      const value = display(entry.value)
      const citation = citationById.value.get(String(entry.citation ?? '')) ?? null
      return {
        fieldKey: nextKey(rawKey),
        rawKey,
        key: rawKey,
        label: humanize(rawKey),
        value,
        citation,
        page: citation?.page ?? null,
        linkedDoc: findLinkedDocument(rawKey, value),
        prominent: isProminent(rawKey),
      }
    })
  }
  const out: Field[] = []
  for (const [key, value] of Object.entries(record)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      for (const [inner, innerValue] of Object.entries(value as Record<string, unknown>)) {
        const rawKey = `${key}_${inner}`
        const text = display(innerValue)
        out.push({
          fieldKey: nextKey(rawKey),
          rawKey,
          key: rawKey,
          label: humanize(rawKey),
          value: text,
          citation: null,
          page: null,
          linkedDoc: findLinkedDocument(rawKey, text),
          prominent: isProminent(rawKey),
        })
      }
    } else {
      const text = display(value)
      out.push({
        fieldKey: nextKey(key),
        rawKey: key,
        key,
        label: humanize(key),
        value: text,
        citation: null,
        page: null,
        linkedDoc: findLinkedDocument(key, text),
        prominent: isProminent(key),
      })
    }
  }
  return out
}

/**
 * Group fields by shared prefix (`vendor_*`, `invoice_*`, `po_*`) so related
 * facts sit together instead of as one flat list.
 */
function group(fields: Field[], fallback: string): Group[] {
  const counts = new Map<string, number>()
  for (const field of fields) {
    const prefix = field.key.split('_')[0]
    counts.set(prefix, (counts.get(prefix) ?? 0) + 1)
  }
  const groups: Group[] = []
  const loose: Field[] = []
  for (const field of fields) {
    const prefix = field.key.split('_')[0]
    if ((counts.get(prefix) ?? 0) < 2) { loose.push(field); continue }
    const existing = groups.find(item => item.name === prefix)
    const trimmedKey = field.key.slice(prefix.length + 1) || field.key
    const short: Field = { ...field, key: trimmedKey, label: humanize(trimmedKey) }
    if (existing) existing.fields.push(short)
    else groups.push({ name: prefix, label: humanize(prefix), fields: [short] })
  }
  if (loose.length) {
    groups.push({ name: fallback, label: humanize(fallback), fields: loose })
  }
  return groups
}

/**
 * Whether a page reference tells the reader anything. On a one-page invoice
 * every field said `p.1` — seventeen chips stating one fact — so where every
 * cited field sits on the same page, the reading pane says so once above the
 * list and the rows carry none. The full-width sheet keeps them.
 */
const fieldPages = computed(() => new Set(
  props.records.flatMap((record, index) => fieldsOf(record, index))
    .map(field => field.page)
    .filter((page): page is number => page != null),
))
const showPages = computed(() => !props.compact || fieldPages.value.size > 1)

const sheets = computed(() => props.records.map((record, index) => {
  const allFields = fieldsOf(record, index)
  return {
    index,
    fieldCount: allFields.length,
    groups: group(allFields, props.schema?.document_type ?? 'details'),
  }
}))
</script>

<template>
  <div class="sheets" :class="{ compact }">
    <article v-for="sheet in sheets" :key="sheet.index" class="sheet">
      <header v-if="!compact || sheets.length > 1">
        <span class="eyebrow">
          Record {{ sheet.index + 1 }} of {{ records.length }}<template v-if="schema?.document_type"> · {{ sentenceCase(schema.document_type) }}</template>
        </span>
        <!-- In the reading pane the heading above the list already says who
             read these and against what. -->
        <template v-if="!compact">
          <span v-if="validated" class="pill">Validated</span>
          <span class="by-model"><i class="aw-icon aw-icon-sparkles" aria-hidden="true" />AI extracted</span>
        </template>
      </header>

      <div class="body">
        <section v-for="grp in sheet.groups" :key="grp.name" class="field-group">
          <h5 class="group-title">{{ grp.label }}</h5>
          <!-- The reading pane lists fields as label and value, one per line:
               boxed cards in a 24rem pane fitted eight fields to a screen. -->
          <dl v-if="compact" class="field-list">
            <div
              v-for="field in grp.fields"
              :key="field.fieldKey"
              :data-field-key="field.fieldKey"
              class="field-card field-row"
              :class="{ prominent: field.prominent, highlighted: activeKey === field.fieldKey }"
              @mouseenter="emit('hover', field.fieldKey)"
              @mouseleave="emit('hover', null)"
              @click="emit('focus-field', { key: field.fieldKey, citation: field.citation })"
            >
              <dt class="field-label">{{ field.label }}</dt>
              <dd class="field-cell">
                <button
                  v-if="field.linkedDoc"
                  type="button"
                  class="field-link"
                  :title="`Open ${field.linkedDoc.title}`"
                  @click.stop="emit('open-document', field.linkedDoc.id)"
                >
                  <span>{{ field.value }}</span>
                  <i class="aw-icon aw-icon-arrow-up-right" aria-hidden="true" />
                </button>
                <span v-else class="field-value">{{ field.value }}</span>
                <button
                  v-if="showPages && field.page && field.citation"
                  type="button"
                  class="cite"
                  :title="field.citation.excerpt ? `Page ${field.page}: ${field.citation.excerpt}` : `Jump to page ${field.page}`"
                  @click.stop="emit('focus-field', { key: field.fieldKey, citation: field.citation }); emit('cite', field.citation)"
                >p.{{ field.page }}</button>
              </dd>
            </div>
          </dl>
          <div v-else class="field-grid">
            <div
              v-for="field in grp.fields"
              :key="field.fieldKey"
              :data-field-key="field.fieldKey"
              class="field-card"
              :class="{ prominent: field.prominent, highlighted: activeKey === field.fieldKey }"
              @mouseenter="emit('hover', field.fieldKey)"
              @mouseleave="emit('hover', null)"
              @click="emit('focus-field', { key: field.fieldKey, citation: field.citation })"
            >
              <div class="field-main">
                <span class="field-label">{{ field.label }}</span>
                <button
                  v-if="field.linkedDoc"
                  type="button"
                  class="field-link"
                  :title="`Open ${field.linkedDoc.title}`"
                  @click.stop="emit('open-document', field.linkedDoc.id)"
                >
                  <span>{{ field.value }}</span>
                  <i class="aw-icon aw-icon-arrow-up-right" aria-hidden="true" />
                </button>
                <span v-else class="field-value">{{ field.value }}</span>
              </div>
              <button
                v-if="field.page && field.citation"
                type="button"
                class="cite"
                :title="field.citation.excerpt ? `Page ${field.page}: ${field.citation.excerpt}` : `Jump to page ${field.page}`"
                @click.stop="emit('focus-field', { key: field.fieldKey, citation: field.citation }); emit('cite', field.citation)"
              >p.{{ field.page }}</button>
              <span v-else-if="field.page" class="cite static">p.{{ field.page }}</span>
            </div>
          </div>
        </section>
      </div>
    </article>
  </div>
</template>

<style scoped>
.sheets { display: flex; flex-direction: column; gap: .75rem; }
.sheet {
  width: 100%; max-width: 52rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  overflow: hidden;
}
.sheets.compact .sheet { max-width: none; border: 0; border-radius: 0; background: transparent; }

.sheet header {
  display: flex; align-items: center; gap: .5rem;
  padding: .5rem .875rem;
  border-bottom: 1px solid var(--aw-border); background: var(--aw-canvas);
}
.sheets.compact .sheet header {
  padding: .35rem 0 .5rem;
  background: transparent;
}
.eyebrow {
  flex: 1; min-width: 0;
  color: var(--aw-muted); font-size: var(--aw-text-xs);
  font-weight: 600;
}
.pill {
  padding: .05rem .45rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-ok-soft); color: var(--aw-ok);
  font-size: var(--aw-text-2xs); font-weight: 700;
}
.by-model {
  display: inline-flex; align-items: center; gap: .25rem;
  padding: .05rem .45rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-accent-soft); color: var(--aw-accent);
  font-size: var(--aw-text-2xs); font-weight: 600;
}

.body { display: flex; flex-direction: column; gap: .875rem; padding: 1rem 1.125rem; }
.sheets.compact .body { gap: .625rem; padding: 0; }

.field-group { display: flex; flex-direction: column; gap: .375rem; }
.group-title {
  margin: 0;
  color: var(--aw-muted);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(12.5rem, 1fr));
  gap: .45rem;
}
.sheets.compact .field-grid {
  grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr));
  gap: .375rem;
}
.field-card {
  display: flex; align-items: flex-start; justify-content: space-between; gap: .4rem;
  min-width: 0;
  padding: .45rem .55rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
  cursor: pointer;
  transition: border-color 140ms ease, background-color 140ms ease, box-shadow 140ms ease;
}
.field-card:hover {
  border-color: var(--aw-accent-line);
}
.field-card.highlighted {
  border-color: var(--aw-accent);
  background: var(--aw-accent-soft);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--aw-accent) 22%, transparent);
}
.field-card.prominent {
  border-color: var(--aw-teal-line);
  background: var(--aw-teal-soft);
}
.field-main { display: flex; flex-direction: column; gap: .125rem; min-width: 0; flex: 1; }
.field-label {
  color: var(--aw-muted);
  font-size: var(--aw-text-2xs);
  font-weight: 600;
  line-height: 1.25;
}
.field-value {
  color: var(--aw-ink-strong);
  font-size: var(--aw-text-xs);
  font-weight: 500;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.field-card.prominent .field-value {
  color: var(--aw-teal-strong);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.field-link {
  display: inline-flex; align-items: center; gap: .2rem;
  max-width: 100%; padding: 0; border: 0; background: none;
  color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600;
  text-align: left; text-decoration: underline; text-underline-offset: 2px;
  cursor: pointer;
}
.field-link:hover { color: var(--aw-teal-strong); }
.field-link span { overflow-wrap: anywhere; }
.field-link .aw-icon { font-size: var(--aw-text-2xs); flex: none; }

.cite {
  flex: none;
  padding: .05rem .35rem;
  border: 1px solid var(--aw-accent-line);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-accent-soft);
  color: var(--aw-accent);
  font-family: var(--aw-font-mono);
  font-size: var(--aw-text-2xs);
  font-weight: 600;
  cursor: pointer;
}
.cite:hover {
  border-color: var(--aw-accent);
}
.cite.static { cursor: default; }

/* The reading pane's list: label and value on one ruled line. */
.field-list { display: flex; flex-direction: column; margin: 0; }
.field-row {
  display: grid; grid-template-columns: minmax(6.5rem, 38%) minmax(0, 1fr); gap: .75rem;
  align-items: baseline;
  padding: .4rem .5rem; margin: 0 -.5rem;
  border: 0; border-top: 1px solid var(--aw-border); border-radius: 0;
  background: transparent;
}
.field-row:first-child { border-top: 0; }
.field-row:hover { background: color-mix(in srgb, var(--aw-raised) 70%, transparent); border-color: var(--aw-border); }
.field-row.highlighted {
  border-color: var(--aw-border);
  border-radius: 4px;
  background: var(--aw-accent-soft);
  box-shadow: inset 2px 0 0 var(--aw-accent);
}
.field-row.prominent { border-color: var(--aw-border); background: transparent; }
.field-row.prominent.highlighted { background: var(--aw-accent-soft); }
.field-row .field-label { font-size: var(--aw-text-xs); font-weight: 500; }
.field-cell { display: flex; align-items: baseline; gap: .4rem; margin: 0; min-width: 0; }
.field-row .field-value { flex: 1; font-size: var(--aw-text-sm); }
.field-row.prominent .field-value { color: var(--aw-ink-strong); font-weight: 700; }
.field-row .field-link { font-size: var(--aw-text-sm); }
.sheets.compact .field-group { gap: .125rem; }
</style>
