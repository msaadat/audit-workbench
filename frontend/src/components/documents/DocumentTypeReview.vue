<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'

import { api } from '../../api'
import type {
  DocumentTypeCatalog,
  DocumentTypeDefinition,
  UnidentifiedDocument,
} from '../../types'

const props = defineProps<{ workspaceId: string }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{
  retyped: []
  error: [summary: string, error: unknown]
}>()

const catalog = ref<DocumentTypeCatalog | null>(null)
const bucket = ref<UnidentifiedDocument[]>([])
const reclassifiable = ref<string[]>([])
const loading = ref(false)
const saving = ref<string | null>(null)

/** Per-document draft: an id chosen from the catalog, or a name to coin. */
const chosenType = ref<Record<string, string>>({})
const coinedName = ref<Record<string, string>>({})

const base = computed(() => `/api/workspaces/${props.workspaceId}/documents`)

/** Catalog entries grouped for the picker. `other` is omitted: retyping *to*
 *  `other` is what the document already is, so offering it does nothing. */
const options = computed(() => {
  const value = catalog.value
  if (!value) return []
  const byArea = new Map<string, DocumentTypeDefinition[]>()
  for (const definition of value.types) {
    if (definition.id === value.other) continue
    const group = byArea.get(definition.area) ?? []
    group.push(definition)
    byArea.set(definition.area, group)
  }
  const groups = value.areas
    .filter(area => byArea.has(area.id))
    .map(area => ({
      label: area.label,
      items: (byArea.get(area.id) ?? []).map(definition => ({
        label: definition.label,
        value: definition.id,
        hint: definition.discriminator,
      })),
    }))
  if (value.local_types.length) {
    groups.unshift({
      label: 'Defined for this engagement',
      items: value.local_types.map(local => ({
        label: local.label || local.id,
        value: local.id,
        hint: local.discriminator,
      })),
    })
  }
  return groups
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const [types, unidentified] = await Promise.all([
      api.get<DocumentTypeCatalog>(`${base.value}/types`),
      api.get<{ items: UnidentifiedDocument[]; reclassifiable: string[] }>(
        `${base.value}/unidentified`,
      ),
    ])
    catalog.value = types
    bucket.value = unidentified.items
    reclassifiable.value = unidentified.reclassifiable
  } catch (error) {
    emit('error', 'Could not load the unidentified documents.', error)
  } finally {
    loading.value = false
  }
}

async function retype(documentId: string): Promise<void> {
  const coin = (coinedName.value[documentId] ?? '').trim()
  const typeId = chosenType.value[documentId] ?? ''
  if (!coin && !typeId) return
  saving.value = documentId
  try {
    await api.patch(`${base.value}/${documentId}/type`, coin ? { coin } : { type_id: typeId })
    delete chosenType.value[documentId]
    delete coinedName.value[documentId]
    await load()
    emit('retyped')
  } catch (error) {
    emit('error', 'Could not set that document type.', error)
  } finally {
    saving.value = null
  }
}

/** Re-examine the documents still unidentified against the grown catalog. */
async function reExamine(): Promise<void> {
  saving.value = 'reclassify'
  try {
    await api.post(`${base.value}/reclassify`, {})
    emit('retyped')
    visible.value = false
  } catch (error) {
    emit('error', 'Could not start the re-examination.', error)
  } finally {
    saving.value = null
  }
}

watch(visible, open => { if (open) void load() }, { immediate: true })
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    header="Unidentified documents"
    :style="{ width: '52rem', maxWidth: '95vw' }"
  >
    <Message v-if="!loading && !bucket.length" severity="success" :closable="false">
      Every document has been identified.
    </Message>

    <template v-else>
      <Message severity="info" :closable="false" class="mb-3">
        These documents matched nothing in the catalogue. Naming one lets it take
        part in cycle testing; a name you coin here is offered for the rest of
        this engagement.
      </Message>

      <div v-for="item in bucket" :key="item.document_id" class="type-row">
        <div class="type-row__what">
          <strong>{{ item.title || item.document_id }}</strong>
          <span v-if="item.document_type_other" class="type-row__guess">
            read as “{{ item.document_type_other }}”
          </span>
          <span v-if="item.rationale" class="type-row__why">{{ item.rationale }}</span>
        </div>

        <div class="type-row__choose">
          <Select
            v-model="chosenType[item.document_id]"
            :options="options"
            option-group-label="label"
            option-group-children="items"
            option-label="label"
            option-value="value"
            placeholder="Choose a type"
            filter
            :disabled="!!(coinedName[item.document_id] ?? '').trim()"
            class="w-full"
          />
          <span class="type-row__or">or</span>
          <InputText
            v-model="coinedName[item.document_id]"
            placeholder="Name a new type"
            :disabled="!!chosenType[item.document_id]"
            class="w-full"
          />
          <Button
            label="Set"
            size="small"
            :loading="saving === item.document_id"
            :disabled="
              !chosenType[item.document_id] && !(coinedName[item.document_id] ?? '').trim()
            "
            @click="retype(item.document_id)"
          />
        </div>
      </div>
    </template>

    <template #footer>
      <span v-if="reclassifiable.length" class="type-review__pending">
        {{ reclassifiable.length }} still unidentified can be re-examined against
        the types you have named.
      </span>
      <Button
        v-if="reclassifiable.length"
        label="Re-examine"
        severity="secondary"
        :loading="saving === 'reclassify'"
        @click="reExamine"
      />
      <Button label="Close" text @click="visible = false" />
    </template>
  </Dialog>
</template>

<style scoped>
.type-row {
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--p-content-border-color);
}
.type-row:last-child { border-bottom: none; }
.type-row__what { display: flex; flex-direction: column; gap: 0.15rem; margin-bottom: 0.5rem; }
.type-row__guess { font-size: 0.85rem; color: var(--p-text-muted-color); }
.type-row__why { font-size: 0.8rem; color: var(--p-text-muted-color); }
.type-row__choose {
  display: grid;
  grid-template-columns: 1fr auto 1fr auto;
  gap: 0.5rem;
  align-items: center;
}
.type-row__or { font-size: 0.8rem; color: var(--p-text-muted-color); }
.type-review__pending {
  margin-right: auto;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
@media (max-width: 40rem) {
  .type-row__choose { grid-template-columns: 1fr; }
  .type-row__or { display: none; }
}
</style>
