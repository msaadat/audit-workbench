<script setup lang="ts">
import { computed } from 'vue'

import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { ContextDocument, ContextRead } from '../../types'
import { plural } from '../../format'

/**
 * What a step read, as files rather than as a clause.
 *
 * The transcript already says it in prose, and a sentence can list four
 * filenames. What it cannot do is show that four were taken and five
 * deliberately left — and that scope decision is the most audit-literate thing
 * the run does. Two rows of cards say it at a glance; the supporting material
 * stays in a quiet line underneath, because nobody needs a card for a template.
 */

const props = defineProps<{ context: ContextRead }>()
const nav = useWorkspaceNav()

const CATEGORY_LABELS: Record<string, string> = {
  background: 'Background',
  policy: 'Policy',
  regulation: 'Regulation',
  contract: 'Contract',
  minutes: 'Minutes',
  voucher: 'Voucher',
  evidence: 'Evidence',
  prior_report: 'Prior report',
  correspondence: 'Letter',
  other: 'Document',
}

/** A file reads as a file before it is read at all. */
function badge(name: string) {
  if (/\.pdf$/i.test(name)) return 'PDF'
  if (/\.docx?$/i.test(name)) return 'DOC'
  if (/\.(png|jpe?g|webp|bmp|tiff?)$/i.test(name)) return 'IMG'
  if (/\.(xlsx?|csv|tsv)$/i.test(name)) return 'XLS'
  return 'FILE'
}

function categoryLabel(value: string) {
  return CATEGORY_LABELS[value] || CATEGORY_LABELS.other
}

/** "5 vouchers", or a plain count where the kinds are mixed. */
const withheldSummary = computed(() => {
  const items = props.context.withheld
  const categories = new Set(items.map(item => item.category))
  if (categories.size === 1) {
    const label = categoryLabel([...categories][0]).toLowerCase()
    return `${items.length} ${items.length === 1 ? label : `${label}s`}`
  }
  return plural(items.length, 'document')
})

const footer = computed(() => {
  const parts: string[] = []
  if (props.context.supporting.length) {
    parts.push(`Also ${props.context.supporting.join(', ')}.`)
  }
  if (props.context.unavailable.length) {
    const list = props.context.unavailable.join(', ')
    parts.push(`${list.charAt(0).toUpperCase()}${list.slice(1)} was not available.`)
  }
  return parts.join(' ')
})

/**
 * The label a screen reader hears for the whole block.
 *
 * A single read carries its own prose sentence. A merged one — several units
 * of a fan-out stage read as one card — has no single sentence to borrow, so
 * the card describes itself from what it is showing.
 */
const label = computed(() => {
  if (props.context.sentence) return props.context.sentence
  const read = `Reading ${plural(props.context.documents.length, 'document')}`
  const stage = props.context.stage_title ? ` for ${props.context.stage_title}` : ''
  const held = props.context.withheld.length
    ? `. Holding back ${withheldSummary.value}, outside this step's scope`
    : ''
  return `${read}${stage}${held}.`
})

function open(item: ContextDocument) {
  if (item.document_id) void nav.push('documents', { doc: item.document_id })
}
</script>

<template>
  <!-- Labelled rather than aria-hidden with a sentence beside it: the cards
       are buttons, and hiding a focusable control from assistive technology
       makes it reachable by keyboard but silent when it gets focus. -->
  <section class="context-read" :aria-label="label">
    <header>
      <strong>Reading</strong>
      <span>
        {{ plural(context.documents.length, 'document') }}
        <template v-if="context.stage_title"> · {{ context.stage_title }}</template>
      </span>
    </header>

    <div v-if="context.documents.length" class="cards">
      <button
        v-for="item in context.documents"
        :key="item.document_id"
        type="button"
        class="doc"
        :title="`Open ${item.name}`"
        :aria-label="`Open ${item.name}, ${categoryLabel(item.category)}`"
        @click="open(item)"
      >
        <span class="badge">{{ badge(item.name) }}</span>
        <span class="identity">
          <b>{{ item.name }}</b>
          <small>
            <span class="tag">{{ categoryLabel(item.category) }}</span>
            <span v-if="item.pages">{{ plural(item.pages, 'page') }}</span>
          </small>
        </span>
      </button>
    </div>

    <template v-if="context.withheld.length">
      <header class="held">
        <strong>Held back</strong>
        <span>{{ withheldSummary }} — outside this step's scope</span>
      </header>
      <div class="cards">
        <button
          v-for="item in context.withheld"
          :key="item.document_id"
          type="button"
          class="doc held-doc"
          :title="`Open ${item.name}`"
        :aria-label="`Open ${item.name}, ${categoryLabel(item.category)}`"
          @click="open(item)"
        >
          <span class="badge">{{ badge(item.name) }}</span>
          <span class="identity">
            <b>{{ item.name }}</b>
            <small><span class="tag">{{ categoryLabel(item.category) }}</span></small>
          </span>
        </button>
      </div>
    </template>

    <p v-if="footer" class="footer">{{ footer }}</p>
  </section>
</template>

<style scoped>
.context-read{display:grid;gap:.4rem;align-self:flex-start;width:min(92%,42rem);padding:.7rem .8rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-surface);background:var(--aw-canvas)}
.context-read>header{display:flex;align-items:baseline;gap:.45rem}
.context-read>header>strong{font-size:var(--aw-text-sm);color:var(--aw-ink)}
.context-read>header>span{color:var(--aw-muted);font-family:var(--aw-font-mono);font-size:var(--aw-text-2xs);letter-spacing:.04em;text-transform:uppercase}
.context-read>header.held{margin-top:.35rem;padding-top:.5rem;border-top:1px dashed var(--aw-border)}
.context-read>header.held>strong{color:var(--aw-muted)}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.4rem}
.doc{display:grid;grid-template-columns:2.1rem minmax(0,1fr);gap:0 .5rem;align-items:center;padding:.45rem .5rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);background:var(--aw-panel);text-align:left;cursor:pointer;color:inherit}
.doc:hover{border-color:var(--aw-teal);background:var(--aw-teal-soft)}
.doc:focus-visible{outline:2px solid var(--aw-teal);outline-offset:1px}
.badge{display:grid;place-items:center;width:2.1rem;height:1.6rem;border-radius:var(--aw-radius-control);background:var(--aw-teal-soft);color:var(--aw-teal);font-family:var(--aw-font-mono);font-size:.55rem;font-weight:700;letter-spacing:.03em}
.identity{display:grid;gap:.1rem;min-width:0}
.identity b{font-size:var(--aw-text-xs);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.identity small{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem;color:var(--aw-muted);font-size:var(--aw-text-2xs)}
.tag{padding:.05rem .3rem;border:1px solid var(--aw-teal-line);border-radius:var(--aw-radius-pill);background:var(--aw-teal-soft);color:var(--aw-teal);font-weight:700;letter-spacing:.04em;text-transform:uppercase}

/* Held back is a decision, not a warning: quieter, never alarming. */
.doc.held-doc{border-style:dashed;background:transparent}
.doc.held-doc .badge{background:var(--aw-raised);color:var(--aw-muted)}
.doc.held-doc .identity b{color:var(--aw-muted);font-weight:500}
.doc.held-doc .tag{border-color:var(--aw-border-strong);background:var(--aw-raised);color:var(--aw-muted)}
.doc.held-doc:hover{border-color:var(--aw-border-strong);background:var(--aw-raised)}

.footer{margin:.3rem 0 0;padding-top:.45rem;border-top:1px dashed var(--aw-border);color:var(--aw-muted);font-size:var(--aw-text-2xs);line-height:1.5}
</style>
