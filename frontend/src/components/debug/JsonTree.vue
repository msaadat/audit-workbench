<script setup lang="ts">
defineProps<{ value: unknown; name?: string; path?: string; search?: string }>()

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
  <div v-else class="json-leaf"><span v-if="name" class="json-key">{{ name }}</span><code>{{ display(value) }}</code></div>
</template>

<style scoped>
.json-branch{margin-left:.8rem;border-left:1px solid #dce5ee;padding-left:.55rem}.json-branch>summary{cursor:pointer;display:flex;gap:.45rem;align-items:center;min-height:1.55rem}.json-key{color:#075985;font-family:'JetBrains Mono Variable',monospace;font-size:.75rem}.json-branch small{color:#8090a5}.json-leaf{display:grid;grid-template-columns:minmax(5rem,auto) 1fr;gap:.65rem;margin-left:1.35rem;min-height:1.45rem;align-items:start}.json-leaf code{white-space:pre-wrap;overflow-wrap:anywhere;color:#334155;font-size:.72rem}
</style>
