<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'

import type { AssistantChatSummary } from '../../types'
import UiOverflowMenu from '../ui/UiOverflowMenu.vue'
import { plural } from '../../format'

/**
 * The conversations, as rows in the fieldwork form.
 *
 * `ChatHistoryPanel` said a title, a timestamp and a message count, which is
 * the same three facts about every chat — so choosing between them meant
 * opening them. What actually distinguishes one from another is what happened
 * in it: whether a run is live, and whether any of its runs failed.
 */

const props = defineProps<{
  chats: AssistantChatSummary[]
  activeId: string | null
  /** Show the search field; the docked popover is too short to need it. */
  searchable?: boolean
}>()
const emit = defineEmits<{ select: [string]; create: []; rename: []; remove: [] }>()

const search = ref('')
const visible = computed(() => {
  const needle = search.value.trim().toLowerCase()
  if (!needle) return props.chats
  return props.chats.filter(chat => chat.title.toLowerCase().includes(needle))
})

// Rename and delete belong to a chat, not to the panel. Beside `New chat` they
// read as three peers, which put "create" and "delete for ever" a few pixels
// apart and made both apply to whichever chat happened to be selected.
const rowActions = [
  { label: 'Rename chat', icon: 'pi pi-pencil', command: () => emit('rename') },
  { label: 'Delete chat', icon: 'pi pi-trash', command: () => emit('remove') },
]

const RUNNING = new Set(['running', 'awaiting_approval', 'awaiting_input', 'queued', 'starting'])
function tone(chat: AssistantChatSummary): 'info' | 'warn' | 'ok' | 'neutral' {
  if (chat.last_run_status && RUNNING.has(chat.last_run_status)) return 'info'
  if (chat.failed_run_count) return 'warn'
  return chat.run_count ? 'ok' : 'neutral'
}

function when(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

function meta(chat: AssistantChatSummary): string {
  const parts: string[] = []
  if (chat.last_run_status && RUNNING.has(chat.last_run_status)) parts.push('running')
  parts.push(when(chat.updated_at))
  parts.push(plural(chat.message_count, 'message'))
  if (chat.failed_run_count) parts.push(`${plural(chat.failed_run_count, 'run')} with failures`)
  return parts.filter(Boolean).join(' · ')
}
</script>

<template>
  <div class="chat-list">
    <header>
      <h3 class="aw-label">Chats</h3>
      <Button icon="pi pi-plus" text size="small" aria-label="New chat" v-tooltip.bottom="'New chat'" @click="emit('create')" />
    </header>
    <div v-if="searchable" class="search">
      <IconField>
        <InputIcon class="pi pi-search" />
        <InputText v-model="search" size="small" placeholder="Search chats" />
      </IconField>
    </div>
    <div class="rows">
      <div v-for="chat in visible" :key="chat.id" class="row" :class="{ active: chat.id === activeId }">
        <button type="button" class="open" @click="emit('select', chat.id)">
          <span class="dot" :data-tone="tone(chat)" aria-hidden="true" />
          <span class="copy">
            <span class="title">{{ chat.title }}</span>
            <span class="meta aw-figure" :data-tone="tone(chat)">{{ meta(chat) }}</span>
          </span>
        </button>
        <UiOverflowMenu v-if="chat.id === activeId" :items="rowActions" :tooltip="`Actions for ${chat.title}`" />
      </div>
      <p v-if="!visible.length" class="empty">{{ chats.length ? 'No chat matches.' : 'No chats yet.' }}</p>
    </div>
  </div>
</template>

<style scoped>
.chat-list { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: .5rem; min-height: 2.75rem; padding: 0 .5rem 0 .75rem; }
header h3 { margin: 0; }
.search { padding: 0 .625rem .5rem; }
.search :deep(.p-iconfield), .search :deep(.p-inputtext) { width: 100%; }
.rows { flex: 1; min-height: 0; overflow-y: auto; }

.row { display: flex; align-items: center; gap: .1rem; min-width: 0; border-left: 3px solid transparent; }
.row:hover:not(.active) { background: var(--aw-raised); }
.row.active { border-left-color: var(--aw-teal); background: var(--aw-teal-soft); }
.open {
  display: flex; align-items: center; gap: .5rem;
  flex: 1; min-width: 0;
  padding: .5rem .5rem .5rem .625rem;
  border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='info'] { background: var(--aw-info); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.title { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-sm); text-overflow: ellipsis; white-space: nowrap; }
.row.active .title { color: var(--aw-ink-strong); font-weight: 600; }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-2xs); text-overflow: ellipsis; white-space: nowrap; }
.meta[data-tone='warn'] { color: var(--aw-warn-ink); }
.empty { padding: 1rem .75rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
