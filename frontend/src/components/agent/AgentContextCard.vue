<script setup lang="ts">
import { computed } from 'vue'

import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { WorkspaceDestination } from '../../composables/useWorkspaceNavigation'
import type { ContextArtifact, ContextDocument, ContextRead } from '../../types'
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

/**
 * What was held back, by kind: "5 vouchers", "3 vouchers and 2 contracts".
 *
 * The kind is the whole point — it is what makes the exclusion read as a scope
 * decision rather than a gap. The files themselves are not named here: a step
 * can decline a great many of them, and a wall of dimmed cards buries the four
 * documents the work actually rests on. The provenance rail on the artifact
 * lists them individually for anyone who wants the roster.
 */
const withheldSummary = computed(() => {
  const counts = new Map<string, number>()
  for (const item of props.context.withheld) {
    counts.set(item.category, (counts.get(item.category) ?? 0) + 1)
  }
  const parts = [...counts].map(([category, total]) => {
    const label = categoryLabel(category).toLowerCase()
    return `${total} ${total === 1 ? label : `${label}s`}`
  })
  if (!parts.length) return ''
  if (parts.length === 1) return parts[0]
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
})

/** "1 work product · 3 documents", naming only what is actually there. */
const counts = computed(() => {
  const parts: string[] = []
  if (props.context.artifacts.length) {
    parts.push(plural(props.context.artifacts.length, 'work product'))
  }
  parts.push(plural(props.context.documents.length, 'document'))
  return parts.join(' · ')
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
  const read = `Reading ${counts.value}`
  const stage = props.context.stage_title ? ` for ${props.context.stage_title}` : ''
  const held = props.context.withheld.length
    ? `. Holding back ${withheldSummary.value}, outside this step's scope`
    : ''
  return `${read}${stage}${held}.`
})

function open(item: ContextDocument) {
  if (item.document_id) void nav.push('documents', { doc: item.document_id })
}

const ARTIFACT_DESTINATIONS: Record<string, WorkspaceDestination> = {
  apm: 'apm',
  rcm: 'rcm',
  analysis: 'analysis',
  report: 'report',
}

function openArtifact(item: ContextArtifact) {
  const destination = ARTIFACT_DESTINATIONS[item.destination]
  if (destination) void nav.push(destination)
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
        {{ counts }}
        <template v-if="context.stage_title"> · {{ context.stage_title }}</template>
      </span>
    </header>

    <!-- Work products lead: for a stage like the RCM the memorandum is the
         main context, and the documents are what it was drafted from. -->
    <div v-if="context.artifacts.length" class="cards">
      <button
        v-for="item in context.artifacts"
        :key="item.ref"
        type="button"
        class="doc product"
        :title="`Open the ${item.name.toLowerCase()}`"
        :aria-label="`Open the ${item.name.toLowerCase()}`"
        @click="openArtifact(item)"
      >
        <span class="badge">{{ item.badge }}</span>
        <span class="identity">
          <b>{{ item.name }}</b>
          <small><span class="tag">Work product</span></small>
        </span>
      </button>
    </div>

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

    <p v-if="context.withheld.length" class="held">
      <i class="pi pi-filter" aria-hidden="true" />
      <span><strong>Held back</strong> {{ withheldSummary }} — outside this step's scope.</span>
    </p>

    <p v-if="footer" class="footer">{{ footer }}</p>
  </section>
</template>

<style scoped>
.context-read{display:grid;gap:.4rem;align-self:flex-start;width:min(92%,42rem);padding:.7rem .8rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-surface);background:var(--aw-canvas)}
.context-read>header{display:flex;align-items:baseline;gap:.45rem}
.context-read>header>strong{font-size:var(--aw-text-sm);color:var(--aw-ink)}
.context-read>header>span{color:var(--aw-muted);font-family:var(--aw-font-mono);font-size:var(--aw-text-2xs);letter-spacing:.04em;text-transform:uppercase}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.4rem}
.doc{display:grid;grid-template-columns:2.1rem minmax(0,1fr);gap:0 .5rem;align-items:center;padding:.45rem .5rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);background:var(--aw-panel);text-align:left;cursor:pointer;color:inherit}
.doc:hover{border-color:var(--aw-teal);background:var(--aw-teal-soft)}
.doc:focus-visible{outline:2px solid var(--aw-teal);outline-offset:1px}
.badge{display:grid;place-items:center;width:2.1rem;height:1.6rem;border-radius:var(--aw-radius-control);background:var(--aw-teal-soft);color:var(--aw-teal);font-family:var(--aw-font-mono);font-size:.55rem;font-weight:700;letter-spacing:.03em}
.identity{display:grid;gap:.1rem;min-width:0}
.identity b{font-size:var(--aw-text-xs);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.identity small{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem;color:var(--aw-muted);font-size:var(--aw-text-2xs)}
.tag{padding:.05rem .3rem;border:1px solid var(--aw-teal-line);border-radius:var(--aw-radius-pill);background:var(--aw-teal-soft);color:var(--aw-teal);font-weight:700;letter-spacing:.04em;text-transform:uppercase}

/* Held back is a decision, not a warning: stated once, never alarming. */
.held{display:flex;align-items:baseline;gap:.4rem;margin:.35rem 0 0;padding-top:.45rem;border-top:1px dashed var(--aw-border);color:var(--aw-muted);font-size:var(--aw-text-xs);line-height:1.5}
.held>i{font-size:var(--aw-text-2xs)}
.held strong{color:var(--aw-ink-soft);font-weight:600}

/* A work product is the engagement's own output, not source material. */
.product .badge{background:var(--aw-accent-soft);color:var(--aw-accent)}
.product .tag{border-color:var(--aw-border-strong);background:var(--aw-raised);color:var(--aw-muted)}

.footer{margin:.3rem 0 0;padding-top:.45rem;border-top:1px dashed var(--aw-border);color:var(--aw-muted);font-size:var(--aw-text-2xs);line-height:1.5}
</style>
