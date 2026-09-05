<script setup lang="ts">
import { computed } from 'vue'

import type { DocumentAnalysisCitation, DocumentSchemaRef } from '../../types'

/**
 * What the model read from the page, laid out as a document rather than as a
 * blob of JSON behind a `<details>`.
 *
 * The record *is* the machine's reading of the file beside it, so it is drawn
 * in the same frame and at the same width as the file on the Preview tab: the
 * auditor's job is to compare the two, and a pre-formatted dump in a monospace
 * box makes that a transcription exercise. The syntax stays — the braces and
 * quotes are how you know this is a record and not prose — but it is drawn
 * faintly, so the words are what the eye lands on.
 */

const props = defineProps<{
  records: Array<Record<string, unknown>>
  schema?: DocumentSchemaRef | null
  citations?: DocumentAnalysisCitation[]
  /** Whether the extraction validated against its schema. */
  validated?: boolean
}>()

interface Field { key: string; value: string; page: number | null }
interface Group { name: string; fields: Field[] }

function display(value: unknown): string {
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(display).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** The page a citation id resolves to, which is what the `p.N` chip states. */
const pages = computed(() => new Map(
  (props.citations ?? []).map(citation => [String(citation.id), citation.page]),
))

/**
 * One record's fields, whichever shape the extraction wrote them in.
 *
 * A schema-guided extraction states `{fields: [{name, value, citation}]}`; an
 * older profile-specific one states a plain object. Both are the same thing to
 * a reader, so both are flattened here rather than in two renderers.
 */
function fieldsOf(record: Record<string, unknown>): Field[] {
  const listed = record.fields
  if (Array.isArray(listed)) {
    return listed.map(item => {
      const entry = item as Record<string, unknown>
      return {
        key: String(entry.name ?? ''),
        value: display(entry.value),
        page: pages.value.get(String(entry.citation ?? '')) ?? null,
      }
    })
  }
  const out: Field[] = []
  for (const [key, value] of Object.entries(record)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      for (const [inner, innerValue] of Object.entries(value as Record<string, unknown>)) {
        out.push({ key: `${key}_${inner}`, value: display(innerValue), page: null })
      }
    } else {
      out.push({ key, value: display(value), page: null })
    }
  }
  return out
}

/**
 * Groups are the schema's own nesting where the schema nests. A flat field list
 * names its groups in its keys — `supplier_name`, `supplier_address`,
 * `invoice_date` — so the prefix is the group wherever two fields share one,
 * and everything else falls into a group named for the type.
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
    // The prefix is the group's name, so it is not repeated in the field's.
    const short = { ...field, key: field.key.slice(prefix.length + 1) || field.key }
    if (existing) existing.fields.push(short)
    else groups.push({ name: prefix, fields: [short] })
  }
  if (loose.length) groups.push({ name: fallback, fields: loose })
  return groups
}

const sheets = computed(() => props.records.map((record, index) => ({
  index,
  groups: group(fieldsOf(record), props.schema?.document_type ?? 'record'),
})))
</script>

<template>
  <div class="sheets">
    <article v-for="sheet in sheets" :key="sheet.index" class="sheet">
      <header>
        <span class="eyebrow">
          Record {{ sheet.index + 1 }} of {{ records.length }}<template v-if="schema?.document_type"> · {{ schema.document_type }}</template>
        </span>
        <span v-if="validated" class="pill">validated</span>
        <span class="by-model">read by the model</span>
      </header>

      <div class="body">
        <span class="punct">{</span>
        <section v-for="group in sheet.groups" :key="group.name">
          <p class="group"><span class="punct">"</span>{{ group.name }}<span class="punct">": {</span></p>
          <p v-for="field in group.fields" :key="field.key" class="field">
            <span class="key"><span class="punct">"</span>{{ field.key }}<span class="punct">"</span><span class="punct">:</span></span>
            <span class="value"><span class="punct">"</span>{{ field.value }}<span class="punct">"</span><span class="punct">,</span></span>
            <span v-if="field.page" class="cite">p.{{ field.page }}</span>
          </p>
          <p class="punct close">},</p>
        </section>
        <span class="punct">}</span>
      </div>
    </article>
  </div>
</template>

<style scoped>
.sheets { display: flex; flex-direction: column; gap: .75rem; }
.sheet {
  width: 100%; max-width: 40rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  overflow: hidden;
}
.sheet header {
  display: flex; align-items: center; gap: .5rem;
  padding: .5rem .875rem;
  border-bottom: 1px solid var(--aw-border); background: var(--aw-canvas);
}
.eyebrow {
  flex: 1; min-width: 0;
  color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs);
  font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
}
.pill {
  padding: 0 .375rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-ok-soft); color: var(--aw-ok);
  font-size: var(--aw-text-2xs); font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
}
.by-model { color: var(--aw-accent); font-size: var(--aw-text-2xs); font-weight: 600; }

.body { padding: 1rem 1.25rem; }
.punct { color: var(--aw-border-strong); }
.group {
  margin: .75rem 0 .25rem;
  color: var(--aw-ink-strong); font-family: var(--aw-font-mono);
  font-size: var(--aw-text-xs); font-weight: 600;
}
.body > section:first-child .group { margin-top: .25rem; }
.field {
  display: grid; grid-template-columns: 12.5rem minmax(0, 1fr) auto;
  align-items: baseline; gap: .5rem;
  margin: 0; padding: .15rem 0 .15rem 1.25rem;
}
.key { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }
.value { color: var(--aw-ink); font-size: var(--aw-text-sm); line-height: 1.45; overflow-wrap: anywhere; }
.cite {
  flex: none; padding: 0 .3rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-teal-soft); color: var(--aw-teal);
  font-size: var(--aw-text-2xs); font-weight: 600;
}
.close { margin: 0 0 .25rem; padding-left: 0; }
</style>
