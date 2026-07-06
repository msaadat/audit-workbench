<script setup lang="ts">
import { ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'

const props = defineProps<{ visible: boolean; defaultTitle: string; saving?: boolean }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; pin: [payload: { title: string; note: string }] }>()

const title = ref('')
const note = ref('')

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      title.value = props.defaultTitle
      note.value = ''
    }
  },
)
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    header="Pin to dashboard"
    :style="{ width: '26rem' }"
    @update:visible="emit('update:visible', $event)"
  >
    <div class="field">
      <label for="pin-title">Tile title</label>
      <InputText id="pin-title" v-model="title" autofocus />
    </div>
    <div class="field" style="margin-top: 0.75rem">
      <label for="pin-note">Note (optional)</label>
      <Textarea id="pin-note" v-model="note" rows="2" placeholder="Why this matters…" />
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="emit('update:visible', false)" />
      <Button
        label="Pin"
        icon="pi pi-thumbtack"
        :disabled="!title.trim()"
        :loading="saving"
        @click="emit('pin', { title: title.trim(), note: note.trim() })"
      />
    </template>
  </Dialog>
</template>
