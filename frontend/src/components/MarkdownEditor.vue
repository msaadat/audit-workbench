<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Crepe, CrepeFeature } from '@milkdown/crepe'
import '@milkdown/crepe/theme/common/style.css'
import '@milkdown/crepe/theme/frame.css'

// WYSIWYG markdown editor: a thin v-model wrapper around Milkdown Crepe.
// Crepe stores content as markdown natively, so it round-trips with workspace documents.
const props = defineProps<{ modelValue: string }>()
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

/* Compact the Crepe top bar (toolbar) and halve the editor's text margins. */
.markdown-editor :deep(.milkdown-top-bar) {
  min-height: 32px;
  padding: 0 8px;
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
}

.markdown-editor :deep(.milkdown-toolbar .toolbar-item svg) {
  width: 18px;
  height: 18px;
}

.markdown-editor :deep(.milkdown-toolbar .divider) {
  height: 18px;
  margin: 7px;
}

.markdown-editor :deep(.ProseMirror) {
  padding: 30px 60px;
}
</style>
