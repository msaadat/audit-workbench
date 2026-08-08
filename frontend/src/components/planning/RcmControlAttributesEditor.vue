<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type { CyclePackDescriptor, CycleVouchMetadata, RcmControlAttribute } from '../../types'

const props = defineProps<{
  modelValue: RcmControlAttribute[]
  metadata: CycleVouchMetadata | null
}>()
const emit = defineEmits<{ 'update:modelValue': [RcmControlAttribute[]] }>()

const assertions = ['Existence', 'Completeness', 'Accuracy', 'Authorization', 'Valuation', 'Cut-off', 'Compliance', 'Operational']
const evidenceKinds = computed(() => props.metadata?.registry.evidence_kinds ?? [])
const minCycleRecordKinds = computed(() => props.metadata?.limits.min_cycle_record_kinds ?? 2)

// A transaction-cycle attribute is only usable once a pack is resolved. Until
// metadata loads — or where no pack is installed — the registry is absent, so
// every read goes through here rather than assuming the discriminator implies
// the payload.
function transactionPackId(attribute?: RcmControlAttribute): string | undefined {
  return attribute?.evidence_kind === 'transaction_cycle'
    ? attribute.registry?.pack_id
    : undefined
}

function replace(index: number, changes: Record<string, unknown>) {
  const values = props.modelValue.map(item => ({ ...item })) as Array<Record<string, unknown>>
  values[index] = { ...values[index], ...changes }
  emit('update:modelValue', values as unknown as RcmControlAttribute[])
}
function setEvidenceKind(index: number, evidenceKind: string) {
  const current = props.modelValue[index]
  if (evidenceKind === 'transaction_cycle') {
    const existingPackId = transactionPackId(props.modelValue.find((item, itemIndex) => itemIndex !== index && item.evidence_kind === 'transaction_cycle'))
    const pack = props.metadata?.registry.packs.find(item => item.id === existingPackId) ?? props.metadata?.registry.packs[0]
    // A cycle attribute is meaningless without an exact pack reference, and a
    // half-formed one would fail the save. Until metadata resolves, the change
    // simply does not apply.
    if (!pack) return
    replace(index, {
      evidence_kind: evidenceKind,
      registry: { pack_id: pack.id, pack_version: pack.version, definition_hash: pack.definition_hash },
      required_record_kinds: [],
    })
  } else {
    const { registry: _registry, required_record_kinds: _kinds, ...rest } = current as RcmControlAttribute & { registry?: unknown; required_record_kinds?: unknown }
    const values = props.modelValue.map(item => ({ ...item }))
    values[index] = { ...rest, evidence_kind: evidenceKind } as RcmControlAttribute
    emit('update:modelValue', values)
  }
}
function setPack(index: number, packId: string) {
  const pack = props.metadata?.registry.packs.find(item => item.id === packId)
  if (!pack) return
  replace(index, {
    registry: { pack_id: pack.id, pack_version: pack.version, definition_hash: pack.definition_hash },
    required_record_kinds: [],
  })
}
function packFor(attribute: RcmControlAttribute): CyclePackDescriptor | undefined {
  const packId = transactionPackId(attribute)
  return packId ? props.metadata?.registry.packs.find(pack => pack.id === packId) : undefined
}
function packOptions(index: number): CyclePackDescriptor[] {
  const existingPackId = transactionPackId(props.modelValue.find((item, itemIndex) => itemIndex !== index && item.evidence_kind === 'transaction_cycle'))
  return existingPackId
    ? (props.metadata?.registry.packs ?? []).filter(pack => pack.id === existingPackId)
    : props.metadata?.registry.packs ?? []
}
// Keys must be unique within the row, and the backend rejects the whole save if
// they are not. Counting entries would repeat a key as soon as one is removed
// from the middle, so the suffix walks past whatever is already taken.
function nextKey(): string {
  const taken = new Set(props.modelValue.map(item => item.key))
  let suffix = props.modelValue.length + 1
  while (taken.has(`attribute_${suffix}`)) suffix += 1
  return `attribute_${suffix}`
}
function add() {
  emit('update:modelValue', [
    ...props.modelValue,
    {
      key: nextKey(),
      assertion: 'Operational',
      requirement: '',
      evidence_kind: 'manual_inspection',
    },
  ])
}
function remove(index: number) {
  if (props.modelValue.length <= 1) return
  emit('update:modelValue', props.modelValue.filter((_, itemIndex) => itemIndex !== index))
}
</script>

<template>
  <section class="attributes">
    <div class="heading"><div><strong>Control attributes</strong><small>Requirements only; executable tables, fields, and comparisons belong to linked tests.</small></div><Button label="Add attribute" icon="pi pi-plus" size="small" outlined @click="add" /></div>
    <article v-for="(attribute, index) in modelValue" :key="index" class="attribute">
      <label>Key<InputText :modelValue="attribute.key" @update:modelValue="replace(index, { key: $event })" /></label>
      <label>Assertion<Select :modelValue="attribute.assertion" :options="assertions" @update:modelValue="replace(index, { assertion: $event })" /></label>
      <label>Evidence strategy<Select :modelValue="attribute.evidence_kind" :options="evidenceKinds" optionLabel="label" optionValue="id" @update:modelValue="setEvidenceKind(index, String($event))" /></label>
      <label v-if="attribute.evidence_kind === 'transaction_cycle'">Cycle pack<Select :modelValue="transactionPackId(attribute)" :options="packOptions(index)" optionLabel="label" optionValue="id" placeholder="No pack installed" @update:modelValue="setPack(index, String($event))" /></label>
      <label class="wide">Requirement<Textarea :modelValue="attribute.requirement" rows="2" autoResize @update:modelValue="replace(index, { requirement: $event })" /></label>
      <label v-if="attribute.evidence_kind === 'transaction_cycle'" class="wide">Required record kinds<MultiSelect :modelValue="attribute.required_record_kinds ?? []" :options="(packFor(attribute)?.record_kinds ?? []).filter(item => item.bindable)" optionLabel="label" optionValue="id" display="chip" filter @update:modelValue="replace(index, { required_record_kinds: $event })" /></label>
      <small v-if="attribute.evidence_kind === 'transaction_cycle' && (attribute.required_record_kinds?.length ?? 0) < minCycleRecordKinds" class="wide warn">A transaction cycle links at least {{ minCycleRecordKinds }} record kinds. For a requirement answered by one record, use Document content; for one the imported tables can answer across the population, use Tabular population.</small>
      <Button icon="pi pi-trash" label="Remove" text severity="danger" size="small" :disabled="modelValue.length <= 1" @click="remove(index)" />
    </article>
  </section>
</template>

<style scoped>
.attributes { display:flex; flex-direction:column; gap:.65rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface) }
.heading { display:flex; align-items:center; justify-content:space-between; gap:1rem }.heading>div { display:flex; flex-direction:column; gap:.2rem }.heading small { color:var(--aw-muted); font-weight:400 }
.attribute { display:grid; grid-template-columns:1fr 1fr 1fr; gap:.65rem; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-canvas) }
label { display:flex; flex-direction:column; gap:.3rem; min-width:0; color:var(--aw-ink-soft); font-size:var(--aw-text-sm); font-weight:600 }.wide { grid-column:1/-1 }.attribute>.p-button { justify-self:start }
.warn { color:var(--aw-warn); font-size:var(--aw-text-sm) }
@media(max-width:800px){.attribute{grid-template-columns:1fr}.wide{grid-column:auto}}
</style>
