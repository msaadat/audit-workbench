<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import Button from 'primevue/button'
import Menu from 'primevue/menu'
import Textarea from 'primevue/textarea'

import type { AssistantCapabilities, AssistantSlashCommand, AuditDocument } from '../../types'
import type { AgentMode } from '../../composables/useAgentRun'

const props = defineProps<{
  busy: boolean
  capabilities: AssistantCapabilities | null
  mode: AgentMode
  documents: AuditDocument[]
  selectedIds: string[]
  runActive?: boolean
}>()
const emit = defineEmits<{
  send: [string]
  'update:mode': [AgentMode]
  documents: []
  removeDocument: [string]
  stop: []
}>()
const draft = ref('')
const textareaRef = ref()
const modeMenu = ref()
const modeOptions = [
  { label: 'Auto', value: 'auto', hint: 'Apply workspace changes automatically' },
  { label: 'Ask', value: 'permission', hint: 'Ask for approval before changes' },
]
const modeLabel = computed(() => modeOptions.find(option => option.value === props.mode)?.label ?? 'Auto')
const modeMenuItems = computed(() => modeOptions.map(option => ({
  label: option.label,
  icon: option.value === props.mode ? 'pi pi-check' : undefined,
  command: () => emit('update:mode', option.value as AgentMode),
})))
const canSend = computed(() => {
  if (!draft.value.trim() || props.busy) return false
  // `/status` is a local deterministic command and remains available even
  // when neither model profile has been configured.
  if (/^\/status$/i.test(draft.value.trim())) return true
  return Boolean(props.capabilities?.ask || props.capabilities?.act)
})
const placeholder = computed(() => {
  // Capabilities are unknown while the chat is still loading; only a loaded
  // "nothing configured" state should show the configuration hint.
  if (props.capabilities && !props.capabilities.ask && !props.capabilities.act) return 'Configure an assistant or agent model first'
  // Steering a live run has always worked; nothing ever said so.
  if (props.runActive) return 'Send a message to steer the run…'
  return 'Ask a question, or type / for commands…'
})

// Slash-command autocomplete. A command is the whole message, never an
// argument tail, so the menu stays open for as long as the draft is a bare
// "/query" and closes the moment it stops looking like one being typed.
function normalize(text: string) { return text.trim().toLowerCase().replace(/[\s_-]+/g, ' ') }
const commandQuery = computed(() => {
  if (!draft.value.startsWith('/') || draft.value.includes('\n')) return null
  return normalize(draft.value.slice(1))
})
const commandMatches = computed<AssistantSlashCommand[]>(() => {
  const query = commandQuery.value
  if (query === null) return []
  const all = props.capabilities?.commands ?? []
  if (!query) return all
  return all.filter(item => {
    const slash = normalize(item.slash)
    return slash.startsWith(query) || slash.includes(query)
  })
})
const menuOpen = ref(false)
const activeIndex = ref(0)
let suppressReopen = false
watch(commandMatches, matches => {
  activeIndex.value = 0
  if (suppressReopen) { suppressReopen = false; return }
  menuOpen.value = matches.length > 0
})
watch(commandQuery, query => { if (query === null) menuOpen.value = false })

async function selectCommand(item: AssistantSlashCommand) {
  suppressReopen = true
  draft.value = `/${item.slash}`
  menuOpen.value = false
  await nextTick()
  const el = textareaRef.value?.$el ?? textareaRef.value
  el?.focus?.()
  el?.setSelectionRange?.(draft.value.length, draft.value.length)
}

function submit() { const value = draft.value.trim(); if (!value || !canSend.value) return; draft.value = ''; menuOpen.value = false; emit('send', value) }
function openModeMenu(event: Event) { modeMenu.value?.toggle(event) }
function keydown(event: KeyboardEvent) {
  if (menuOpen.value && commandMatches.value.length) {
    if (event.key === 'ArrowDown') { event.preventDefault(); activeIndex.value = (activeIndex.value + 1) % commandMatches.value.length; return }
    if (event.key === 'ArrowUp') { event.preventDefault(); activeIndex.value = (activeIndex.value - 1 + commandMatches.value.length) % commandMatches.value.length; return }
    if (event.key === 'Escape') { event.preventDefault(); menuOpen.value = false; return }
    if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey && !event.isComposing) {
      event.preventDefault()
      void selectCommand(commandMatches.value[activeIndex.value])
      return
    }
  }
  // isComposing: Enter that commits an IME composition must not send.
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit() }
}
function label(id: string) { return props.documents.find(doc => doc.id === id)?.title ?? id }
</script>

<template>
  <div class="composer">
    <div class="composer-surface">
      <ul v-if="menuOpen && commandMatches.length" class="command-menu" role="listbox" aria-label="Slash commands">
        <li
          v-for="(item, index) in commandMatches" :key="item.id" role="option" :aria-selected="index === activeIndex"
          :class="{ active: index === activeIndex }"
          @mousedown.prevent="selectCommand(item)" @mouseenter="activeIndex = index"
        >
          <span class="command-slash">/{{ item.slash }}</span>
          <span class="command-label">{{ item.label }}</span>
          <span v-if="item.description" class="command-description">{{ item.description }}</span>
        </li>
      </ul>
      <div v-if="selectedIds.length" class="chips">
        <span v-for="id in selectedIds" :key="id"><i class="pi pi-file" />{{ label(id) }}<button :aria-label="`Remove ${label(id)}`" @click="emit('removeDocument', id)"><i class="pi pi-times" /></button></span>
      </div>
      <!-- Stays enabled while busy so the next message can be drafted during a long reply. -->
      <Textarea ref="textareaRef" v-model="draft" rows="1" autoResize :placeholder="placeholder" @keydown="keydown" />
      <div class="toolbar">
        <Button class="control" icon="pi pi-paperclip" label="Context" outlined size="small" severity="secondary" @click="emit('documents')" />
        <Button class="control mode" :label="modeLabel" icon="pi pi-angle-down" iconPos="right" outlined size="small" severity="secondary" aria-haspopup="true" aria-controls="composer-mode-menu" @click="openModeMenu" />
        <Menu id="composer-mode-menu" ref="modeMenu" :model="modeMenuItems" popup />
        <span class="grow" />
        <!-- Stopping a run is a first-class action, not something to hunt for
             inside an expanded card. -->
        <Button v-if="runActive" icon="pi pi-stop-circle" size="small" severity="danger" outlined aria-label="Stop the run" @click="emit('stop')" />
        <Button class="send" label="Send" icon="pi pi-send" iconPos="right" size="small" aria-label="Send" :loading="busy" :disabled="!canSend" @click="submit" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.composer{padding:.55rem .7rem .65rem;border-top:1px solid var(--aw-border);background:var(--aw-canvas)}
.composer-surface{position:relative;display:grid;gap:.35rem;padding:.38rem .55rem .45rem;border:1px solid var(--aw-border-strong);border-radius:6px;background:var(--p-surface-0);box-shadow:var(--aw-shadow-sm);transition:border-color .15s,box-shadow .15s}
.composer-surface:focus-within{border-color:var(--aw-teal-600);box-shadow:0 0 0 2px rgb(13 148 136 / 10%)}
.command-menu{position:absolute;left:0;right:0;bottom:100%;margin:0 0 .4rem;padding:.3rem;list-style:none;border:1px solid var(--aw-border-strong);border-radius:8px;background:var(--p-surface-0);box-shadow:var(--aw-shadow-md);max-height:14rem;overflow-y:auto;z-index:20}
.command-menu li{display:flex;align-items:baseline;gap:.45rem;padding:.4rem .5rem;border-radius:6px;cursor:pointer}
.command-menu li.active{background:var(--aw-teal-soft)}
.command-slash{flex:0 0 auto;font-family:var(--aw-mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:.72rem;font-weight:600;color:var(--aw-teal)}
.command-label{flex:0 0 auto;font-size:.75rem;font-weight:600}
.command-description{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.68rem;color:var(--aw-muted)}
.composer :deep(textarea){width:100%;min-height:1.6rem;padding:.18rem 0;border:0;border-radius:0;background:transparent;box-shadow:none;font-size:.8rem;line-height:1.35;max-height:9rem;resize:none}
.composer :deep(textarea:enabled:focus){border-color:transparent;outline:0;box-shadow:none}
.toolbar{display:flex;align-items:center;gap:.35rem}.grow{flex:1}.control{font-size:.68rem}.control :deep(.p-button-label){font-weight:600}.control :deep(.p-button-icon){font-size:.68rem}.mode{min-width:3.15rem}.send{font-size:.68rem}.send :deep(.p-button-label){font-weight:700}.send :deep(.p-button-icon){font-size:.65rem}.chips{display:flex;gap:.3rem;overflow:auto}.chips span{display:flex;align-items:center;gap:.25rem;flex:0 0 auto;max-width:11rem;padding:.25rem .4rem;border-radius:999px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:.65rem}.chips span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chips button{border:0;background:transparent;color:inherit;cursor:pointer;padding:0}
@media(max-width:520px){.composer{padding:.4rem}.control{padding-inline:.4rem}.control :deep(.p-button-label){display:none}.mode :deep(.p-button-label){display:inline}.send{padding-inline:.5rem}}
</style>
