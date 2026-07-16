<script setup lang="ts">
import Button from 'primevue/button'
import type { AssistantChatSummary } from '../../types'

defineProps<{ chats: AssistantChatSummary[]; activeId: string | null }>()
const emit = defineEmits<{ select: [string]; create: []; rename: []; remove: []; close: [] }>()
</script>

<template>
  <div class="history-panel">
    <header><strong>Chats</strong><Button icon="pi pi-times" text size="small" @click="emit('close')" /></header>
    <div class="actions"><Button label="New chat" icon="pi pi-plus" size="small" @click="emit('create')" /><Button label="Rename" icon="pi pi-pencil" severity="secondary" outlined size="small" :disabled="!activeId" @click="emit('rename')" /><Button icon="pi pi-trash" severity="danger" text size="small" :disabled="!activeId" @click="emit('remove')" /></div>
    <button v-for="chat in chats" :key="chat.id" class="chat-row" :class="{ active: chat.id === activeId }" @click="emit('select', chat.id)"><strong>{{ chat.title }}</strong><small>{{ new Date(chat.updated_at).toLocaleString() }} · {{ chat.message_count }} messages</small></button>
    <p v-if="!chats.length">No chats yet.</p>
  </div>
</template>

<style scoped>
.history-panel{position:absolute;z-index:5;inset:3.1rem .55rem auto .55rem;max-height:70%;overflow:auto;padding:.65rem;border:1px solid var(--aw-border);border-radius:10px;background:var(--p-surface-0);box-shadow:var(--aw-shadow-md)}header,.actions{display:flex;align-items:center;gap:.35rem}header{justify-content:space-between}.actions{margin:.45rem 0 .65rem}.chat-row{display:grid;gap:.15rem;width:100%;padding:.55rem;margin:.3rem 0;text-align:left;border:1px solid transparent;border-radius:7px;background:var(--p-surface-50);color:inherit;cursor:pointer}.chat-row:hover,.chat-row.active{border-color:var(--aw-teal)}.chat-row strong{font-size:.76rem}.chat-row small,.history-panel p{font-size:.65rem;color:var(--aw-muted)}
</style>
