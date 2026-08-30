<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'

import { api } from '../../api'
import type {
  ClassifiedDocument,
  DocumentTypeCatalog,
  DocumentTypeDefinition,
} from '../../types'

const props = defineProps<{ workspaceId: string }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{
  retyped: []
  error: [summary: string, error: unknown]
}>()

const catalog = ref<DocumentTypeCatalog | null>(null)
const assignments = ref<ClassifiedDocument[]>([])
const reclassifiable = ref<string[]>([])
const loading = ref(false)
const saving = ref<string | null>(null)
const filter = ref('')
const expanded = ref<Record<string, boolean>>({})

/** Per-document draft: an id chosen from the catalog, or a name to coin. */
const chosenType = ref<Record<string, string>>({})
const coinedName = ref<Record<string, string>>({})

const base = computed(() => `/api/workspaces/${props.workspaceId}/documents`)

/** What a type id reads as, coined types included. */
const labelOf = computed(() => {
  const value = catalog.value
  const names = new Map<string, string>()
  for (const definition of value?.types ?? []) names.set(definition.id, definition.label)
  for (const local of value?.local_types ?? []) names.set(local.id, local.label || local.id)
  return (id: string) => names.get(id) ?? id
})

/** The documents that announced they needed attention. */
const bucket = computed(() =>
  assignments.value.filter(item => item.document_type === catalog.value?.other),
)

/** Everything already carrying a type, grouped by it. A wrong label is only
 *  findable by reading down the type it was wrongly given — a count that looks
 *  too large for what the corpus holds is the signal. */
const groups = computed(() => {
  const needle = filter.value.trim().toLowerCase()
  const byType = new Map<string, ClassifiedDocument[]>()
  for (const item of assignments.value) {
    const type = item.document_type ?? ''
    if (!type || type === catalog.value?.other) continue
    if (needle && !`${item.title} ${labelOf.value(type)}`.toLowerCase().includes(needle)) {
      continue
    }
    const group = byType.get(type) ?? []
    group.push(item)
    byType.set(type, group)
  }
  return [...byType.entries()]
    .map(([id, items]) => ({ id, label: labelOf.value(id), items }))
    .sort((a, b) => a.label.localeCompare(b.label))
})

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
    const [types, classified] = await Promise.all([
      api.get<DocumentTypeCatalog>(`${base.value}/types`),
      api.get<{ items: ClassifiedDocument[]; reclassifiable: string[] }>(
        `${base.value}/classifications`,
      ),
    ])
    catalog.value = types
    assignments.value = classified.items
    reclassifiable.value = classified.reclassifiable
  } catch (error) {
    emit('error', 'Could not load the document types.', error)
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
    header="Document types"
    :style="{ width: '52rem', maxWidth: '95vw' }"
  >
    <section>
      <h4 class="type-section">Not identified</h4>
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
    </section>

    <section class="type-identified">
      <h4 class="type-section">Identified</h4>
      <Message severity="secondary" :closable="false" class="mb-3">
        A type the model was confident about never reaches the bucket above. Read
        down a type whose count looks wrong for what this engagement holds — that
        is where a document filed as something it is not will be sitting.
      </Message>

      <InputText v-model="filter" placeholder="Filter by title or type" class="w-full mb-3" />

      <div v-for="group in groups" :key="group.id" class="type-group">
        <button type="button" class="type-group__head" @click="expanded[group.id] = !expanded[group.id]">
          <i :class="expanded[group.id] ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" />
          <span>{{ group.label }}</span>
          <span class="type-group__count">{{ group.items.length }}</span>
        </button>

        <div v-show="expanded[group.id]">
          <div v-for="item in group.items" :key="item.document_id" class="type-row">
            <div class="type-row__what">
              <strong>{{ item.title || item.document_id }}</strong>
              <span v-if="item.assigned_by === 'auditor'" class="type-row__guess">
                set by you
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
        </div>
      </div>
    </section>

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
.type-section {
  margin: 0 0 0.6rem;
  font-size: 0.8rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--p-text-muted-color);
}
.type-identified {
  margin-top: 1.5rem;
  padding-top: 1.25rem;
  border-top: 1px solid var(--p-content-border-color);
}
.type-group__head {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  width: 100%;
  padding: 0.4rem 0.25rem;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 0.9rem;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}
.type-group__head:hover { color: var(--p-primary-color); }
.type-group__head i { font-size: 0.7rem; }
.type-group__count { margin-left: auto; font-weight: 400; color: var(--p-text-muted-color); }
.type-group > div > .type-row:first-child { border-top: 1px solid var(--p-content-border-color); }
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
