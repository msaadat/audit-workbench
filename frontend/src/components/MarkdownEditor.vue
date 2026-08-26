<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Crepe, CrepeFeature } from '@milkdown/crepe'
import '@milkdown/crepe/theme/common/style.css'
import '@milkdown/crepe/theme/frame.css'

// WYSIWYG markdown editor: a thin v-model wrapper around Milkdown Crepe.
// Crepe stores content as markdown natively, so it round-trips with workspace documents.
const props = withDefaults(defineProps<{ modelValue: string; placeholder?: string }>(), {
  // Crepe ships "Please enter…", which is neither this product's voice nor
  // this product's language. Callers override it with something about the
  // document they are editing.
  placeholder: 'Start writing, or generate a draft.',
})
const emit = defineEmits<{ 'update:modelValue': [string] }>()

const host = ref<HTMLElement>()
let crepe: Crepe | null = null
// Last value that originated inside the editor — lets the watcher below tell an
// external change (agent regenerated the APM) apart from our own keystrokes.
let lastEmitted = ''

async function mount(value: string) {
  crepe = new Crepe({
    root: host.value,
    defaultValue: value,
    features: { [CrepeFeature.TopBar]: true },
    featureConfigs: { [CrepeFeature.Placeholder]: { text: props.placeholder } },
  })
  crepe.on((listener) => {
    listener.markdownUpdated((_ctx, markdown, prev) => {
      if (markdown !== prev) {
        lastEmitted = markdown
        emit('update:modelValue', markdown)
      }
    })
  })
  await crepe.create()
}

async function unmount() {
  await crepe?.destroy()
  crepe = null
}

onMounted(() => {
  lastEmitted = props.modelValue
  void mount(props.modelValue)
})

// Crepe's defaultValue only seeds the initial content, so an external change
// (e.g. reload() swapping in an agent draft) requires rebuilding the editor.
// Skip when the value came from our own edit to avoid clobbering the cursor.
watch(
  () => props.modelValue,
  async (value) => {
    if (value === lastEmitted) return
    lastEmitted = value
    await unmount()
    await mount(value)
  },
)

onBeforeUnmount(() => void unmount())
</script>

<template>
  <div ref="host" class="markdown-editor" />
</template>

<style scoped>
.markdown-editor {
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-surface);
  overflow: auto;
  background: var(--aw-panel);
}

/*
 * Crepe intentionally ships a self-contained document theme. Map that theme
 * to the workbench tokens here so the editor is a native surface rather than
 * a second visual language inside the app.
 */
.markdown-editor :deep(.milkdown) {
  --crepe-color-background: var(--aw-panel);
  --crepe-color-on-background: var(--aw-ink);
  --crepe-color-surface: var(--aw-raised);
  --crepe-color-surface-low: var(--aw-canvas);
  --crepe-color-on-surface: var(--aw-ink);
  --crepe-color-on-surface-variant: var(--aw-ink-soft);
  --crepe-color-outline: var(--aw-border-strong);
  --crepe-color-primary: var(--aw-teal);
  --crepe-color-secondary: var(--aw-teal-soft);
  --crepe-color-on-secondary: var(--aw-teal);
  --crepe-color-inverse: var(--aw-navy-900);
  --crepe-color-on-inverse: var(--aw-on-navy);
  --crepe-color-inline-code: var(--aw-ink);
  --crepe-color-error: var(--aw-danger);
  --crepe-color-hover: var(--aw-teal-soft);
  --crepe-color-selected: var(--aw-teal-line);
  --crepe-color-inline-area: var(--aw-raised);
  --crepe-font-title: var(--aw-font-sans);
  --crepe-font-default: var(--aw-font-sans);
  --crepe-font-code: var(--aw-font-mono);
  --crepe-shadow-1: var(--aw-shadow-sm);
  --crepe-shadow-2: var(--aw-shadow-md);
}

/*
 * A memorandum is read, not filled in.
 *
 * The editor drew the APM and the report as full-width interface text at body
 * size — the visual language of a form field, over a document a partner is
 * meant to read end to end. A measure, a reading size and page-like padding
 * are the difference between "a textarea with Markdown in it" and a planning
 * memorandum, and they cost no component logic.
 *
 * The measure is set as padding on the content box, not as `max-width` plus
 * `margin-inline: auto` on each block. Centring blocks individually gives each
 * one its own left edge — a heading, a paragraph and a list all start at
 * different x, because their boxes are different widths — and Crepe's drag
 * handles, which position against the block, drift with them. Padding gives
 * every child one shared edge and lets a wide table use the room it needs.
 */
.markdown-editor :deep(.milkdown .ProseMirror) {
  padding-block: 2rem;
  padding-inline: max(1.75rem, calc((100% - 68ch) / 2));
  font-size: var(--aw-text-md);
  line-height: 1.65;
}
.markdown-editor :deep(.milkdown .ProseMirror > :is(h1, h2, h3)) {
  margin-top: 1.6em;
  line-height: 1.25;
  letter-spacing: -0.01em;
  text-wrap: balance;
}
.markdown-editor :deep(.milkdown .ProseMirror > h1) { font-size: var(--aw-text-2xl); }
.markdown-editor :deep(.milkdown .ProseMirror > h2) { font-size: var(--aw-text-xl); }
.markdown-editor :deep(.milkdown .ProseMirror > h3) { font-size: var(--aw-text-lg); }

.markdown-editor:focus-within {
  border-color: var(--aw-teal-600);
  box-shadow: 0 0 0 1px var(--aw-teal-600);
}

/* Compact the Crepe top bar (toolbar) and align its controls with PrimeVue. */
.markdown-editor :deep(.milkdown-top-bar) {
  min-height: 32px;
  padding: 0 8px;
  border-bottom-color: var(--aw-border);
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-divider) {
  height: 18px;
  margin: 7px;
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-heading-selector) {
  padding: 4px;
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-heading-button) {
  height: 24px;
  padding: 2px 2px 2px 6px;
  border-radius: var(--aw-radius-control);
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-heading-label) {
  font-size: 12px;
  line-height: 16px;
  min-width: 60px;
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-chevron) {
  width: 18px;
  height: 18px;
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-chevron svg) {
  width: 12px;
  height: 12px;
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-item) {
  width: 24px;
  height: 24px;
  margin: 4px;
  padding: 2px;
  border-radius: var(--aw-radius-control);
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-item svg) {
  width: 18px;
  height: 18px;
}

.markdown-editor :deep(.milkdown-toolbar .toolbar-item) {
  width: 24px;
  height: 24px;
  margin: 4px;
  padding: 2px;
  border-radius: var(--aw-radius-control);
}

.markdown-editor :deep(.milkdown-toolbar .toolbar-item svg) {
  width: 18px;
  height: 18px;
}

.markdown-editor :deep(.milkdown-toolbar .divider) {
  height: 18px;
  margin: 7px;
}

.markdown-editor :deep(.milkdown-top-bar .top-bar-heading-dropdown),
.markdown-editor :deep(.milkdown-toolbar) {
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
}

.markdown-editor :deep(.milkdown button:focus-visible),
.markdown-editor :deep(.milkdown input:focus-visible) {
  outline: 3px solid rgb(13 148 136 / 30%);
  outline-offset: 1px;
}

.markdown-editor :deep(.ProseMirror) {
  padding: var(--aw-space-4) var(--aw-space-5);
  color: var(--aw-ink);
  font-family: var(--aw-font-sans);
  font-size: var(--aw-text-base);
  line-height: 1.55;
}

/* Match the compact markdown renderer used in previews, chat, and reports. */
.markdown-editor :deep(.ProseMirror h1),
.markdown-editor :deep(.ProseMirror h2),
.markdown-editor :deep(.ProseMirror h3),
.markdown-editor :deep(.ProseMirror h4),
.markdown-editor :deep(.ProseMirror h5),
.markdown-editor :deep(.ProseMirror h6) {
  color: var(--aw-ink);
  font-family: var(--aw-font-sans);
  font-weight: 700;
  letter-spacing: -0.02em;
}

.markdown-editor :deep(.ProseMirror h1) { font-size: var(--aw-text-xl); line-height: 1.25; margin: 0 0 0.8rem; }
.markdown-editor :deep(.ProseMirror h2) { font-size: var(--aw-text-lg); line-height: 1.3; margin: 1rem 0 0.4rem; }
.markdown-editor :deep(.ProseMirror h3),
.markdown-editor :deep(.ProseMirror h4) { font-size: var(--aw-text-md); line-height: 1.35; margin: 0.8rem 0 0.3rem; }
.markdown-editor :deep(.ProseMirror h5),
.markdown-editor :deep(.ProseMirror h6) { font-size: var(--aw-text-base); line-height: 1.4; margin: 0.7rem 0 0.25rem; }

.markdown-editor :deep(.ProseMirror p) {
  padding: 0;
  margin: 0.35rem 0;
  font-size: var(--aw-text-base);
  line-height: 1.55;
}

.markdown-editor :deep(.ProseMirror ul),
.markdown-editor :deep(.ProseMirror ol) {
  margin: 0.35rem 0;
  padding-left: 1.25rem;
}

.markdown-editor :deep(.ProseMirror li) { line-height: 1.55; }

.markdown-editor :deep(.ProseMirror a) {
  color: var(--aw-teal);
  text-underline-offset: 2px;
}

.markdown-editor :deep(.ProseMirror code) {
  display: inline;
  padding: 0 0.25rem;
  border-radius: var(--aw-radius-control);
  background: var(--aw-raised);
  color: var(--aw-ink);
  font-family: var(--aw-font-mono);
}

.markdown-editor :deep(.ProseMirror pre) {
  margin: 0.75rem 0;
  padding: var(--aw-space-3);
  border-radius: var(--aw-radius-control);
  background: var(--aw-raised);
}

.markdown-editor :deep(.ProseMirror pre code) { padding: 0; }

.markdown-editor :deep(.ProseMirror blockquote) {
  margin: 0.75rem 0;
  padding-left: var(--aw-space-3);
  color: var(--aw-ink-soft);
}

.markdown-editor :deep(.ProseMirror blockquote::before) { background: var(--aw-teal-line); }

.markdown-editor :deep(.milkdown-table-block) { margin: 0.75rem 0; }

.markdown-editor :deep(.milkdown-table-block table) {
  width: 100%;
  font-size: var(--aw-text-sm);
}

.markdown-editor :deep(.milkdown-table-block th),
.markdown-editor :deep(.milkdown-table-block td) {
  padding: 0.45rem 0.55rem;
  border-color: var(--aw-border);
  text-align: left;
  vertical-align: top;
}

.markdown-editor :deep(.milkdown-table-block th) { background: var(--aw-raised); }

@media (max-width: 700px) {
  .markdown-editor :deep(.ProseMirror) { padding: var(--aw-space-3) var(--aw-space-4); }
}
</style>
