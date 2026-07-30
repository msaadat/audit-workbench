<script setup lang="ts">
import { computed, ref } from 'vue'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'

import type { AnalyticsTest } from '../../types'

const props = defineProps<{ tests: AnalyticsTest[]; selectedId: string | null }>()
defineEmits<{ select: [test: AnalyticsTest] }>()

const search = ref('')

const groups = computed(() => {
  const query = search.value.trim().toLowerCase()
  const matches = props.tests.filter(test => !query
    || test.label.toLowerCase().includes(query)
    || test.description.toLowerCase().includes(query)
    || (test.group ?? '').toLowerCase().includes(query))
  const result: Array<{ name: string; tests: AnalyticsTest[] }> = []
  for (const test of matches) {
    const name = test.group || 'Other'
    const group = result.find(item => item.name === name)
    if (group) group.tests.push(test)
    else result.push({ name, tests: [test] })
  }
  return result
})
</script>

<template>
  <div class="catalog">
    <IconField>
      <InputIcon class="pi pi-search" />
      <InputText v-model="search" placeholder="Search the analytics library" autofocus />
    </IconField>

    <!-- One row per analytic. A card grid with a paragraph in every card made
         fifteen options read like fifteen documents. -->
    <div class="groups">
      <section v-for="group in groups" :key="group.name">
        <p class="group-name">{{ group.name }}</p>
        <button
          v-for="test in group.tests"
          :key="test.id"
          type="button"
          class="row"
          :class="{ active: selectedId === test.id }"
          @click="$emit('select', test)"
        >
          <i :class="test.icon" />
          <span class="row-copy">
            <strong>{{ test.label }}</strong>
            <small>{{ test.description }}</small>
          </span>
          <i class="pi pi-arrow-right row-go" />
        </button>
      </section>
      <p v-if="!groups.length" class="empty">No analytic matches “{{ search }}”.</p>
    </div>
  </div>
</template>

<style scoped>
.catalog { display: flex; flex-direction: column; gap: 0.6rem; min-width: 0; }
.catalog :deep(.p-iconfield) { width: 100%; }
.catalog :deep(.p-inputtext) { width: 100%; }
.groups { display: flex; flex-direction: column; gap: 0.7rem; max-height: 24rem; overflow-y: auto; }
.group-name { margin: 0 0 0.25rem; color: var(--aw-muted); font-size: 0.68rem; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; }
.row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  width: 100%;
  min-width: 0;
  padding: 0.5rem 0.6rem;
  border: 1px solid transparent;
  border-radius: var(--aw-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.row + .row { margin-top: 0.15rem; }
.row:hover { background: var(--aw-raised); }
.row.active { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.row > i:first-child { flex: 0 0 auto; color: var(--aw-teal); }
.row-copy { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.row-copy strong { font-size: 0.84rem; }
.row-copy small { overflow: hidden; color: var(--aw-muted); font-size: 0.75rem; text-overflow: ellipsis; white-space: nowrap; }
.row-go { flex: 0 0 auto; color: var(--aw-muted); font-size: 0.7rem; opacity: 0; }
.row:hover .row-go, .row.active .row-go { opacity: 1; }
.empty { padding: 1.5rem 0; color: var(--aw-muted); font-size: 0.8rem; text-align: center; }
</style>
