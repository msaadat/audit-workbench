<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: unknown; name?: string; path?: string; search?: string }>()

function parseInlineJson(value: unknown): Record<string, unknown> | unknown[] | undefined {
  if (typeof value !== 'string') return undefined
  const text = value.trim()
  if (!text.startsWith('{') && !text.startsWith('[')) return undefined
  try {
    const parsed: unknown = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> | unknown[] : undefined
  } catch {
    return undefined
  }
}

const inlineJson = computed(() => parseInlineJson(props.value))
const isContentText = computed(() => props.name === 'content' && typeof props.value === 'string' && !inlineJson.value)

function entries(value: unknown): [string, unknown][] {
  if (Array.isArray(value)) return value.map((item, index) => [String(index), item])
  if (value && typeof value === 'object') return Object.entries(value as Record<string, unknown>)
  return []
}
function expandable(value: unknown) { return !!value && typeof value === 'object' }
function display(value: unknown) {
  if (typeof value === 'string') return JSON.stringify(value)
  return String(value)
}
function collectionLabel(value: unknown) { return Array.isArray(value) ? `[${entries(value).length}]` : `{${entries(value).length}}` }
function childPath(path: string | undefined, key: string) { return `${path || '$'}.${key}` }
function visible(path: string | undefined, key: string, child: unknown, search: string | undefined) {
  if (!search) return true
  const term = search.toLowerCase()
  return childPath(path, key).toLowerCase().includes(term) || JSON.stringify(child).toLowerCase().includes(term)
}
</script>

<template>
  <details v-if="expandable(value)" class="json-branch" open>
    <summary><span v-if="name" class="json-key">{{ name }}</span><small>{{ collectionLabel(value) }}</small></summary>
    <template v-for="([key, child]) in entries(value)" :key="key">
      <JsonTree v-if="visible(path, key, child, search)" :value="child" :name="key" :path="childPath(path, key)" :search="search" />
    </template>
  </details>
  <details v-else-if="inlineJson" class="json-branch json-inline" :open="Boolean(search)">
    <summary><span v-if="name" class="json-key">{{ name }}</span><small>inline JSON {{ collectionLabel(inlineJson) }}</small></summary>
    <JsonTree :value="inlineJson" :path="path" :search="search" />
  </details>
  <div v-else-if="isContentText" class="json-text">
    <span class="json-key">{{ name }}</span>
    <pre>{{ value }}</pre>
  </div>
  <div v-else class="json-leaf"><span v-if="name" class="json-key">{{ name }}</span><code>{{ display(value) }}</code></div>
</template>

<style scoped>
.json-branch{margin-left:.8rem;border-left:1px solid var(--aw-border);padding-left:.55rem}.json-branch>summary{cursor:pointer;display:flex;gap:.45rem;align-items:center;min-height:1.55rem}.json-key{color:var(--aw-info);font-family:'JetBrains Mono Variable',monospace;font-size:var(--aw-text-sm)}.json-branch small{color:var(--aw-muted-strong)}.json-leaf{display:grid;grid-template-columns:minmax(5rem,auto) 1fr;gap:.65rem;margin-left:1.35rem;min-height:1.45rem;align-items:start}.json-leaf code{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--aw-ink-soft);font-size:var(--aw-text-xs)}
.json-text{display:grid;gap:.35rem;margin:.35rem 0 .35rem 1.35rem}.json-text pre{margin:0;max-height:28rem;overflow:auto;padding:.7rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);background:var(--aw-canvas);color:var(--aw-ink-soft);font:11px/1.55 'JetBrains Mono Variable',monospace;white-space:pre-wrap;overflow-wrap:anywhere}
</style>
