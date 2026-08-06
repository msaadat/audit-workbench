<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorView, basicSetup } from 'codemirror'
import { python } from '@codemirror/lang-python'

// Thin CodeMirror 6 wrapper with v-model, styled to sit inside PrimeVue forms.
const props = defineProps<{ modelValue: string }>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()

const host = ref<HTMLElement>()
let view: EditorView | null = null

onMounted(() => {
  view = new EditorView({
    doc: props.modelValue,
    parent: host.value!,
    extensions: [
      basicSetup,
      python(),
      EditorView.lineWrapping,
      EditorView.updateListener.of((update) => {
        if (update.docChanged) emit('update:modelValue', update.state.doc.toString())
      }),
      EditorView.theme({
        '&': { fontSize: '0.82rem' },
        '.cm-content': {
          fontFamily: "ui-monospace, 'Cascadia Code', Consolas, monospace",
          padding: '0.5rem 0',
        },
        '.cm-gutters': {
          backgroundColor: 'var(--p-surface-50)',
          color: 'var(--p-surface-400)',
          border: 'none',
        },
        '&.cm-focused': { outline: 'none' },
      }),
    ],
  })
})

watch(
  () => props.modelValue,
  (value) => {
    if (view && value !== view.state.doc.toString()) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } })
    }
  },
)

onBeforeUnmount(() => view?.destroy())
</script>

<template>
  <div ref="host" class="code-editor" />
</template>

<style scoped>
.code-editor {
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  overflow: hidden;
  background: var(--aw-panel);
}
.code-editor:focus-within {
  border-color: var(--aw-teal-600);
  box-shadow: 0 0 0 1px var(--aw-teal-600);
}
.code-editor :deep(.cm-editor) {
  max-height: 24rem;
}
.code-editor :deep(.cm-scroller) {
  overflow: auto;
}
</style>
