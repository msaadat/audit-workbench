<script setup lang="ts">
import Button from 'primevue/button'
import type { AssistantChatSummary } from '../../types'

/**
 * Two placements: a dropdown that overlays the thread (the drawer, and narrow
 * consoles), or a docked rail that just fills whatever box it is given (the
 * console's left pane, and the mobile "Chats" dialog).
 */
import UiOverflowMenu from '../ui/UiOverflowMenu.vue'
import { plural } from '../../format'

defineProps<{ chats: AssistantChatSummary[]; activeId: string | null; docked?: boolean }>()
const emit = defineEmits<{ select: [string]; create: []; rename: []; remove: []; close: [] }>()

// Rename and delete belong to a chat, not to the panel. In one button group
// beside "New chat" they read as three peers, which put "create" and the red
// "delete for ever" a few pixels apart and made both apply to whichever chat
// happened to be selected.
const rowActions = [
  { label: 'Rename chat', icon: 'pi pi-pencil', command: () => emit('rename') },
  { label: 'Delete chat', icon: 'pi pi-trash', command: () => emit('remove') },
]
</script>

<template>
  <div class="history-panel" :class="{ docked }">
    <header>
      <strong>Chats</strong>
      <span class="grow" />
      <Button icon="pi pi-plus" text size="small" aria-label="New chat" v-tooltip.bottom="'New chat'" @click="emit('create')" />
      <Button v-if="!docked" icon="pi pi-times" text size="small" aria-label="Close" @click="emit('close')" />
    </header>
    <div v-for="chat in chats" :key="chat.id" class="chat-row" :class="{ active: chat.id === activeId }">
      <button type="button" class="chat-open" @click="emit('select', chat.id)">
        <strong>{{ chat.title }}</strong>
        <small>{{ new Date(chat.updated_at).toLocaleString() }} · {{ plural(chat.message_count, 'message') }}</small>
      </button>
      <UiOverflowMenu v-if="chat.id === activeId" :items="rowActions" :tooltip="`Actions for ${chat.title}`" />
    </div>
    <p v-if="!chats.length">No chats yet.</p>
  </div>
</template>

<style scoped>
.history-panel{position:absolute;z-index:5;inset:3.1rem .55rem auto .55rem;max-height:70%;overflow:auto;padding:.65rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-surface);background:var(--aw-panel);}header{display:flex;align-items:center;gap:.35rem;margin-bottom:.5rem}.grow{flex:1}.chat-row{display:flex;align-items:flex-start;gap:.25rem;width:100%;padding:.35rem .4rem .35rem .55rem;margin:.3rem 0;border:1px solid transparent;border-radius:var(--aw-radius-control);background:var(--aw-canvas);color:inherit}.chat-row:hover,.chat-row.active{border-color:var(--aw-teal)}.chat-open{display:grid;gap:.15rem;flex:1;min-width:0;padding:.2rem 0;border:0;background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}.chat-row strong{display:-webkit-box;overflow:hidden;font-size:var(--aw-text-sm);-webkit-box-orient:vertical;-webkit-line-clamp:2}.chat-row small,.history-panel p{font-size:var(--aw-text-2xs);color:var(--aw-muted)}
.history-panel.docked{position:static;inset:auto;z-index:auto;height:100%;max-height:none;overflow-y:auto;padding:0;border:0;border-radius:0;background:transparent;box-shadow:none}
</style>
