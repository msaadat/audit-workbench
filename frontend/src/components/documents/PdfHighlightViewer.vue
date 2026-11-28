<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'

import { plural } from '../../format'
import type { DocumentHighlightItem, DocumentNoteMarker } from '../../types'

/**
 * Renders a PDF page as a crisp image with two interactive AI overlay layers:
 *  - Purple bounding boxes for AI-extracted structured fields (two-way synced
 *    with the Reading pane's field cards).
 *  - Amber numbered callout pins for AI audit notes (clicking a pin opens the
 *    full observation as an anchored callout with Prev/Next stepping).
 */

const props = defineProps<{
  workspaceId: string
  documentId: string
  page: number
  pageCount: number
  items: DocumentHighlightItem[]
  notes?: DocumentNoteMarker[]
  activeKey?: string | null
  activeNoteKey?: string | null
  cacheBust?: string
}>()

const emit = defineEmits<{
  'update:page': [page: number]
  hover: [key: string | null]
  select: [item: DocumentHighlightItem]
  'select-note': [note: DocumentNoteMarker | null]
  'edit-notes': []
}>()

const showFields = ref(true)
const showNotes = ref(true)
const hoveredNoteKey = ref<string | null>(null)
const imageLoading = ref(true)
const imageError = ref(false)
const stageRef = ref<HTMLElement | null>(null)

const ACRONYMS: Record<string, string> = {
  id: 'ID', po: 'PO', grn: 'GRN', pr: 'PR', req: 'Req',
  ntn: 'NTN', gst: 'GST', vat: 'VAT', iban: 'IBAN', fx: 'FX',
  gl: 'GL', pkr: 'PKR', usd: 'USD', eur: 'EUR', gbp: 'GBP',
}

function fieldLabel(raw: string): string {
  const words = raw.replace(/^local\./, '').split(/[_\s]+/).filter(Boolean)
  if (!words.length) return raw
  return words.map((word, idx) => {
    const lower = word.toLowerCase()
    if (ACRONYMS[lower]) return ACRONYMS[lower]
    return idx === 0 ? lower[0].toUpperCase() + lower.slice(1) : lower
  }).join(' ')
}

const pageImageUrl = computed(() => {
  const q = props.cacheBust ? `?v=${encodeURIComponent(props.cacheBust)}` : ''
  return `/api/workspaces/${props.workspaceId}/documents/${props.documentId}/pages/${props.page}/image${q}`
})

const fileUrl = computed(() => `/api/workspaces/${props.workspaceId}/documents/${props.documentId}/file`)
const allNotes = computed(() => props.notes ?? [])
const pageItems = computed(() => props.items.filter(item => item.page === props.page))
const pageNotes = computed(() => allNotes.value.filter(note => note.page === props.page))
const openNote = computed(() =>
  allNotes.value.find(note => note.key === props.activeNoteKey) ?? null,
)
const openNoteOnPage = computed(() =>
  openNote.value && openNote.value.page === props.page ? openNote.value : null,
)
const openNoteIndex = computed(() =>
  openNote.value ? allNotes.value.findIndex(n => n.key === openNote.value!.key) : -1,
)

const totalPages = computed(() => Math.max(1, props.pageCount || 1))

watch(pageImageUrl, () => {
  imageLoading.value = true
  imageError.value = false
})

watch(() => props.activeKey, async key => {
  if (!key) return
  const match = props.items.find(item => item.key === key)
  if (!match) return
  if (match.page !== props.page) emit('update:page', match.page)
  await nextTick()
  const el = stageRef.value?.querySelector<HTMLElement>(`[data-hl-key="${CSS.escape(key)}"]`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
})

watch(() => props.activeNoteKey, async key => {
  if (!key) return
  const match = allNotes.value.find(note => note.key === key)
  if (!match) return
  if (match.page !== props.page) emit('update:page', match.page)
  await nextTick()
  const el = stageRef.value?.querySelector<HTMLElement>(`[data-note-key="${CSS.escape(key)}"]`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
})

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && props.activeNoteKey) {
    emit('select-note', null)
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

function boxStyle(box: { x: number; y: number; width: number; height: number }) {
  return {
    left: `${(box.x * 100).toFixed(3)}%`,
    top: `${(box.y * 100).toFixed(3)}%`,
    width: `${(box.width * 100).toFixed(3)}%`,
    height: `${(box.height * 100).toFixed(3)}%`,
  }
}

function pinStyle(note: DocumentNoteMarker) {
  return {
    left: `${(note.x * 100).toFixed(3)}%`,
    top: `${(note.y * 100).toFixed(3)}%`,
  }
}

function calloutClasses(note: DocumentNoteMarker) {
  return {
    'flip-left': note.x > 0.52,
    'flip-up': note.y > 0.62,
  }
}

function toggleNote(note: DocumentNoteMarker) {
  emit('select-note', props.activeNoteKey === note.key ? null : note)
}

function stepNote(delta: number) {
  if (!allNotes.value.length || openNoteIndex.value < 0) return
  const nextIdx = (openNoteIndex.value + delta + allNotes.value.length) % allNotes.value.length
  const target = allNotes.value[nextIdx]
  if (target.page !== props.page) emit('update:page', target.page)
  emit('select-note', target)
}
</script>

<template>
  <div class="pdf-hl">
    <!-- A page with nothing read from it yet is drawn on the same sheet, with
         no bar: there is nothing to count and no layer to toggle. -->
    <div v-if="items.length || allNotes.length || totalPages > 1" class="pdf-hl-bar">
      <span v-if="items.length || allNotes.length" class="pdf-hl-stat">
        <i class="aw-icon aw-icon-sparkles" aria-hidden="true" />
        <strong v-if="pageItems.length">{{ plural(pageItems.length, 'field') }}</strong>
        <template v-if="pageItems.length && pageNotes.length"><span aria-hidden="true">·</span></template>
        <strong v-if="pageNotes.length" class="stat-notes">{{ plural(pageNotes.length, 'audit note') }}</strong>
        <span class="aw-type-meta">on page {{ page }}</span>
      </span>
      <span class="grow" />
      <label v-if="items.length" class="layer-toggle">
        <input v-model="showFields" type="checkbox" />
        <span>Fields</span>
      </label>
      <label v-if="allNotes.length" class="layer-toggle warn">
        <input v-model="showNotes" type="checkbox" />
        <span>Notes</span>
      </label>
      <div v-if="totalPages > 1" class="pdf-hl-pager">
        <Button
          icon="aw-icon aw-icon-chevron-left"
          text
          size="small"
          severity="secondary"
          :disabled="page <= 1"
          aria-label="Previous page"
          @click="emit('update:page', page - 1)"
        />
        <span class="aw-figure">{{ page }} / {{ totalPages }}</span>
        <Button
          icon="aw-icon aw-icon-chevron-right"
          text
          size="small"
          severity="secondary"
          :disabled="page >= totalPages"
          aria-label="Next page"
          @click="emit('update:page', page + 1)"
        />
      </div>
    </div>

    <div ref="stageRef" class="pdf-hl-stage" @click="activeNoteKey && emit('select-note', null)">
      <div v-if="imageError" class="pdf-hl-fallback">
        <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" />
        <span>Could not render this PDF page. <a :href="fileUrl" target="_blank" rel="noopener noreferrer">Open the original file</a> to view it.</span>
      </div>
      <div v-else class="pdf-sheet" :class="{ loading: imageLoading }">
        <img
          class="pdf-page-img"
          :src="pageImageUrl"
          :alt="`Page ${page}`"
          @load="imageLoading = false"
          @error="imageLoading = false; imageError = true"
        />
        <div v-if="!imageLoading" class="pdf-overlay" aria-label="AI field and audit note highlights">
          <!-- Layer 1: Purple AI-extracted field boxes -->
          <template v-for="item in pageItems" :key="item.key">
            <button
              v-for="(box, boxIdx) in item.boxes"
              :key="`${item.key}:${boxIdx}`"
              type="button"
              class="hl-box"
              :data-hl-key="boxIdx === 0 ? item.key : undefined"
              :class="{
                visible: showFields || activeKey === item.key,
                active: activeKey === item.key,
                dimmed: Boolean(activeKey && activeKey !== item.key),
              }"
              :style="boxStyle(box)"
              :aria-label="`${fieldLabel(item.field_name)}: ${item.value}`"
              @mouseenter="emit('hover', item.key)"
              @mouseleave="emit('hover', null)"
              @focus="emit('hover', item.key)"
              @blur="emit('hover', null)"
              @click.stop="emit('select', item)"
            >
              <span v-if="boxIdx === 0" class="hl-tag">{{ fieldLabel(item.field_name) }}</span>
            </button>
          </template>

          <!-- Layer 2: Amber anchor highlight when a note pin is hovered or open -->
          <template v-for="note in pageNotes" :key="`anchor:${note.key}`">
            <span
              v-if="note.box && (hoveredNoteKey === note.key || activeNoteKey === note.key)"
              class="note-anchor-box"
              :style="boxStyle(note.box)"
              aria-hidden="true"
            />
          </template>

          <!-- Layer 3: Amber numbered audit-note pins -->
          <template v-if="showNotes || activeNoteKey">
            <div
              v-for="note in pageNotes"
              :key="note.key"
              class="note-pin-wrap"
              :class="{ open: activeNoteKey === note.key }"
              :style="pinStyle(note)"
            >
              <button
                type="button"
                class="note-pin"
                :data-note-key="note.key"
                :class="{ active: activeNoteKey === note.key, unanchored: !note.anchored }"
                :aria-expanded="activeNoteKey === note.key"
                :aria-label="`Audit note ${note.number}: ${note.text}`"
                @mouseenter="hoveredNoteKey = note.key"
                @mouseleave="hoveredNoteKey = null"
                @click.stop="toggleNote(note)"
              >{{ note.number }}</button>

              <!-- Floating callout popover for the active pin -->
              <article
                v-if="openNoteOnPage && openNoteOnPage.key === note.key"
                class="note-callout"
                :class="calloutClasses(note)"
                role="dialog"
                :aria-label="`Audit note ${note.number}`"
                @click.stop
              >
                <header class="callout-head">
                  <span class="callout-badge">Audit note #{{ note.number }}</span>
                  <span class="callout-scope">{{ note.anchored ? 'Pinned to text' : 'Page-level observation' }}</span>
                  <button type="button" class="callout-close" aria-label="Close callout" @click="emit('select-note', null)">
                    <i class="aw-icon aw-icon-x" aria-hidden="true" />
                  </button>
                </header>
                <p class="callout-body">{{ note.text }}</p>
                <footer class="callout-foot">
                  <div v-if="allNotes.length > 1" class="callout-nav">
                    <button type="button" class="callout-step" aria-label="Previous note" @click="stepNote(-1)">
                      <i class="aw-icon aw-icon-chevron-left" aria-hidden="true" /> Prev
                    </button>
                    <span class="aw-figure">{{ openNoteIndex + 1 }} / {{ allNotes.length }}</span>
                    <button type="button" class="callout-step" aria-label="Next note" @click="stepNote(1)">
                      Next <i class="aw-icon aw-icon-chevron-right" aria-hidden="true" />
                    </button>
                  </div>
                  <span class="grow" />
                  <button type="button" class="callout-edit" @click="emit('edit-notes')">Edit in notes</button>
                </footer>
              </article>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pdf-hl {
  display: flex;
  flex-direction: column;
  gap: .625rem;
  flex: 1 1 auto;
  min-height: 0;
}
.pdf-hl-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: .5rem .75rem;
  padding: .4rem .65rem;
  border: 1px solid var(--aw-accent-line);
  border-radius: var(--aw-radius-control);
  background: var(--aw-accent-soft);
  font-size: var(--aw-text-xs);
}
.pdf-hl-stat {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  color: var(--aw-ink-strong);
}
.pdf-hl-stat .aw-icon { color: var(--aw-accent); }
.stat-notes { color: var(--aw-warn-ink); }
.grow { flex: 1; }
.layer-toggle {
  display: inline-flex;
  align-items: center;
  gap: .3rem;
  color: var(--aw-ink-soft);
  font-weight: 600;
  cursor: pointer;
  user-select: none;
}
.layer-toggle input { accent-color: var(--aw-accent); cursor: pointer; }
.layer-toggle.warn input { accent-color: var(--aw-warn); }
.pdf-hl-pager {
  display: inline-flex;
  align-items: center;
  gap: .15rem;
  color: var(--aw-ink-soft);
}

.pdf-hl-stage {
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: .25rem 0 1.25rem;
}
.pdf-hl-fallback {
  display: flex;
  align-items: center;
  gap: .5rem;
  padding: .75rem 1rem;
  border: 1px solid var(--aw-warn-line);
  border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft);
  color: var(--aw-warn-ink);
  font-size: var(--aw-text-sm);
}

.pdf-sheet {
  position: relative;
  width: min(100%, 46rem);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-sheet);
  box-shadow: var(--aw-sheet-shadow);
  overflow: visible;
}
.pdf-sheet.loading { min-height: 28rem; opacity: .6; }
.pdf-page-img {
  display: block;
  width: 100%;
  height: auto;
  border-radius: var(--aw-radius-control);
  user-select: none;
}
.pdf-overlay {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

/* Purple extracted field boxes */
.hl-box {
  position: absolute;
  padding: 0;
  border: 1.5px solid transparent;
  border-radius: 4px;
  background: transparent;
  cursor: pointer;
  pointer-events: auto;
  opacity: 0;
  transition: opacity 140ms ease, background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
}
.hl-box.visible {
  opacity: 1;
  border-color: color-mix(in srgb, var(--aw-accent) 55%, transparent);
  background: color-mix(in srgb, var(--aw-accent) 12%, transparent);
}
.hl-box.dimmed { opacity: .32; }
.hl-box.active,
.hl-box:hover {
  z-index: 4;
  opacity: 1;
  border-color: var(--aw-accent);
  background: color-mix(in srgb, var(--aw-accent) 24%, transparent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--aw-accent) 22%, transparent);
}
.hl-tag {
  position: absolute;
  left: 0;
  bottom: calc(100% + 4px);
  padding: .1rem .45rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-accent);
  color: var(--aw-on-accent);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
  line-height: 1.3;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  transform: translateY(2px);
  transition: opacity 120ms ease, transform 120ms ease;
  box-shadow: var(--aw-shadow-sm);
}
.hl-box.active .hl-tag,
.hl-box:hover .hl-tag {
  opacity: 1;
  transform: translateY(0);
}

/* Amber audit note anchor & numbered pins */
.note-anchor-box {
  position: absolute;
  z-index: 5;
  border: 2px solid var(--aw-warn);
  border-radius: 4px;
  background: color-mix(in srgb, var(--aw-warn) 18%, transparent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--aw-warn) 22%, transparent);
  pointer-events: none;
}
.note-pin-wrap {
  position: absolute;
  z-index: 6;
  transform: translate(-50%, -50%);
  pointer-events: auto;
}
.note-pin-wrap.open { z-index: 12; }
.note-pin {
  display: grid;
  place-items: center;
  width: 1.4rem;
  height: 1.4rem;
  padding: 0;
  border: 2px solid var(--aw-panel);
  border-radius: 50%;
  background: var(--aw-warn);
  color: var(--aw-on-dark);
  font-size: var(--aw-text-2xs);
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  cursor: pointer;
  box-shadow: var(--aw-shadow-md);
  transition: transform 130ms ease, box-shadow 130ms ease;
}
.note-pin.unanchored { border-style: dashed; }
.note-pin:hover,
.note-pin.active {
  transform: scale(1.18);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--aw-warn) 32%, transparent), var(--aw-shadow-md);
}

/* Anchored callout card */
.note-callout {
  position: absolute;
  top: calc(100% + 8px);
  left: -0.5rem;
  width: min(21rem, 72vw);
  display: flex;
  flex-direction: column;
  gap: .5rem;
  padding: .7rem .8rem;
  border: 1px solid var(--aw-warn-line);
  border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  color: var(--aw-ink);
  box-shadow: var(--aw-shadow-lg);
  text-align: left;
}
.note-callout.flip-left { left: auto; right: -0.5rem; }
.note-callout.flip-up { top: auto; bottom: calc(100% + 8px); }
.callout-head {
  display: flex;
  align-items: center;
  gap: .4rem;
}
.callout-badge {
  padding: .08rem .45rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-warn-soft);
  color: var(--aw-warn-ink);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
}
.callout-scope {
  color: var(--aw-muted);
  font-size: var(--aw-text-2xs);
  flex: 1;
  min-width: 0;
}
.callout-close {
  display: grid;
  place-items: center;
  width: 1.4rem;
  height: 1.4rem;
  padding: 0;
  border: 0;
  border-radius: var(--aw-radius-control);
  background: transparent;
  color: var(--aw-muted);
  cursor: pointer;
}
.callout-close:hover { background: var(--aw-raised); color: var(--aw-ink-strong); }
.callout-body {
  margin: 0;
  color: var(--aw-ink);
  font-size: var(--aw-text-xs);
  line-height: 1.5;
}
.callout-foot {
  display: flex;
  align-items: center;
  gap: .5rem;
  padding-top: .45rem;
  border-top: 1px solid var(--aw-border);
  font-size: var(--aw-text-2xs);
}
.callout-nav {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  color: var(--aw-muted);
}
.callout-step {
  display: inline-flex;
  align-items: center;
  gap: .15rem;
  padding: .15rem .4rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
  color: var(--aw-ink-soft);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.callout-step:hover { border-color: var(--aw-teal); color: var(--aw-teal); }
.callout-edit {
  padding: 0;
  border: 0;
  background: none;
  color: var(--aw-teal);
  font: inherit;
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
</style>
