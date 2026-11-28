<script lang="ts">
/**
 * A file's format, drawn as a small page with its extension on a band.
 *
 * Policies arrive as Word files and evidence as PDFs, and the list gave no
 * sign of either. The band carries the format's own colour, but the word on
 * it is what says the format, so the badge never depends on colour alone.
 */
export type FileKind = 'pdf' | 'word' | 'sheet' | 'image' | 'text' | 'other'

const KINDS: Array<{ kind: FileKind; ext: string; name: string; match: RegExp }> = [
  { kind: 'pdf', ext: 'PDF', name: 'PDF', match: /^pdf$/ },
  { kind: 'word', ext: 'DOC', name: 'Word', match: /^docx?$/ },
  { kind: 'sheet', ext: 'XLS', name: 'Excel', match: /^xls[xm]?$/ },
  { kind: 'sheet', ext: 'CSV', name: 'CSV', match: /^(csv|tsv)$/ },
  { kind: 'image', ext: 'IMG', name: 'Image', match: /^(png|jpe?g|webp|bmp|gif|tiff?)$/ },
  { kind: 'text', ext: 'TXT', name: 'Text', match: /^(txt|md|markdown)$/ },
]

export function fileFormat(file: string): { kind: FileKind; ext: string; name: string } {
  const suffix = (/\.([a-z0-9]{1,5})$/i.exec(file)?.[1] ?? '').toLowerCase()
  const found = KINDS.find(item => item.match.test(suffix))
  if (found) return { kind: found.kind, ext: found.ext, name: found.name }
  return { kind: 'other', ext: suffix.slice(0, 3).toUpperCase() || 'FILE', name: suffix.toUpperCase() || 'File' }
}
</script>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ file: string }>()
const format = computed(() => fileFormat(props.file))
</script>

<template>
  <span class="file-badge" :data-kind="format.kind" role="img" :aria-label="`${format.name} file`" :title="`${format.name} file`">
    <svg viewBox="0 0 20 24" width="20" height="24" aria-hidden="true">
      <path class="sheet" d="M2.5 1.5h10.5l4.5 4.5v16.5h-15z" />
      <path class="fold" d="M13 1.5V6h4.5" />
    </svg>
    <span class="ext">{{ format.ext }}</span>
  </span>
</template>

<style scoped>
.file-badge { position: relative; display: inline-block; flex: none; width: 20px; height: 24px; }
.file-badge svg { display: block; }
.sheet { fill: var(--aw-panel); stroke: var(--aw-border-strong); }
.fold { fill: none; stroke: var(--aw-border-strong); }
.ext {
  position: absolute; left: -3px; bottom: 3px;
  padding: 1px 2px; border-radius: 2px;
  background: var(--aw-muted); color: #fff;
  font: 700 6.5px/1 var(--aw-font-sans); letter-spacing: .02em;
}
/* The colours each format is known by, kept dark enough for the white label. */
.file-badge[data-kind='pdf'] .ext { background: #b42318; }
.file-badge[data-kind='word'] .ext { background: #2b5797; }
.file-badge[data-kind='sheet'] .ext { background: #1d6f42; }
/* Images and text keep the neutral band: purple is the AI's colour here. */
</style>
