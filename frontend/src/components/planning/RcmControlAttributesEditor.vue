<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type {
  CycleFieldSelector,
  CycleOperator,
  CyclePackDescriptor,
  CycleVouchMetadata,
  RcmControlAttribute,
  RcmCycleRequiredComparison,
} from '../../types'

const props = defineProps<{
  modelValue: RcmControlAttribute[]
  metadata: CycleVouchMetadata | null
}>()
const emit = defineEmits<{ 'update:modelValue': [RcmControlAttribute[]] }>()

const assertions = ['Existence', 'Completeness', 'Accuracy', 'Authorization', 'Valuation', 'Cut-off', 'Compliance', 'Operational']
const evidenceKinds = computed(() => props.metadata?.registry.evidence_kinds ?? [])
const minCycleRecordKinds = computed(() => props.metadata?.limits.min_cycle_record_kinds ?? 2)
const comparisonOperators: CycleOperator[] = ['equal_exact', 'equal_normalized', 'numeric_within', 'date_on_or_before', 'date_within', 'present']

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
      required_comparisons: [],
    })
  } else {
    const { registry: _registry, required_record_kinds: _kinds, required_comparisons: _comparisons, ...rest } = current as RcmControlAttribute & { registry?: unknown; required_record_kinds?: unknown; required_comparisons?: unknown }
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
    required_comparisons: [],
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
function setRequiredRecordKinds(index: number, value: string[]) {
  const attribute = props.modelValue[index]
  if (attribute.evidence_kind !== 'transaction_cycle') return
  const retained = attribute.required_comparisons.filter(comparison =>
    value.includes(comparison.left.record_kind)
    && (!comparison.right || value.includes(comparison.right.record_kind)),
  )
  replace(index, { required_record_kinds: value, required_comparisons: retained })
}
function fieldOptions(attribute: RcmControlAttribute, recordKindId: string) {
  if (attribute.evidence_kind !== 'transaction_cycle') return []
  const pack = packFor(attribute)
  const recordKind = pack?.record_kinds.find(item => item.id === recordKindId)
  const allowed = new Set(recordKind?.available_field_kinds ?? [])
  return (pack?.field_kinds ?? []).filter(field => allowed.has(field.id)).flatMap(field =>
    field.attributes.map(item => ({
      label: `${field.label} · ${item.id} (${item.semantic_type})`,
      value: `${field.group}|${field.kind}|${item.id}`,
      selector: { group: field.group, kind: field.kind, attribute: item.id } as CycleFieldSelector,
    })),
  )
}
function selectorValue(selector?: CycleFieldSelector): string {
  return selector ? `${selector.group}|${selector.kind}|${selector.attribute}` : ''
}
function comparisonValues(attribute: RcmControlAttribute): RcmCycleRequiredComparison[] {
  return attribute.evidence_kind === 'transaction_cycle' ? attribute.required_comparisons : []
}
function replaceComparison(attributeIndex: number, comparisonIndex: number, changes: Partial<RcmCycleRequiredComparison>) {
  const attribute = props.modelValue[attributeIndex]
  if (attribute.evidence_kind !== 'transaction_cycle') return
  const comparisons = attribute.required_comparisons.map((item, index) =>
    index === comparisonIndex ? { ...item, ...changes } : item,
  )
  replace(attributeIndex, { required_comparisons: comparisons })
}
function setComparisonField(attributeIndex: number, comparisonIndex: number, side: 'left' | 'right', value: string) {
  const attribute = props.modelValue[attributeIndex]
  if (attribute.evidence_kind !== 'transaction_cycle') return
  const comparison = attribute.required_comparisons[comparisonIndex]
  const operand = side === 'left' ? comparison.left : comparison.right
  if (!operand) return
  const option = fieldOptions(attribute, operand.record_kind).find(item => item.value === value)
  if (option) replaceComparison(attributeIndex, comparisonIndex, { [side]: { ...operand, field: option.selector } })
}
function setComparisonRecordKind(attributeIndex: number, comparisonIndex: number, side: 'left' | 'right', recordKind: string) {
  const attribute = props.modelValue[attributeIndex]
  if (attribute.evidence_kind !== 'transaction_cycle') return
  const selector = fieldOptions(attribute, recordKind)[0]?.selector ?? { group: '', kind: '', attribute: '' }
  replaceComparison(attributeIndex, comparisonIndex, { [side]: { record_kind: recordKind, field: selector } })
}
function setComparisonOperator(attributeIndex: number, comparisonIndex: number, operator: CycleOperator) {
  const attribute = props.modelValue[attributeIndex]
  if (attribute.evidence_kind !== 'transaction_cycle') return
  const comparison = attribute.required_comparisons[comparisonIndex]
  const changes: Partial<RcmCycleRequiredComparison> = { operator }
  if (operator === 'present') changes.right = undefined
  else if (!comparison.right) {
    const recordKind = attribute.required_record_kinds[1] ?? attribute.required_record_kinds[0] ?? ''
    changes.right = { record_kind: recordKind, field: fieldOptions(attribute, recordKind)[0]?.selector ?? { group: '', kind: '', attribute: '' } }
  }
  changes.tolerance = operator === 'numeric_within'
    ? { absolute: 0, percent: 0 }
    : operator === 'date_within' ? 0 : undefined
  replaceComparison(attributeIndex, comparisonIndex, changes)
}
function addComparison(attributeIndex: number) {
  const attribute = props.modelValue[attributeIndex]
  if (attribute.evidence_kind !== 'transaction_cycle' || attribute.required_record_kinds.length < 2) return
  const leftKind = attribute.required_record_kinds[0]
  const rightKind = attribute.required_record_kinds[1]
  const suffix = attribute.required_comparisons.length + 1
  replace(attributeIndex, {
    required_comparisons: [
      ...attribute.required_comparisons,
      {
        key: `comparison_${suffix}`,
        label: '',
        left: { record_kind: leftKind, field: fieldOptions(attribute, leftKind)[0]?.selector ?? { group: '', kind: '', attribute: '' } },
        right: { record_kind: rightKind, field: fieldOptions(attribute, rightKind)[0]?.selector ?? { group: '', kind: '', attribute: '' } },
        operator: 'equal_exact',
      },
    ],
  })
}
function removeComparison(attributeIndex: number, comparisonIndex: number) {
  const attribute = props.modelValue[attributeIndex]
  if (attribute.evidence_kind !== 'transaction_cycle') return
  replace(attributeIndex, { required_comparisons: attribute.required_comparisons.filter((_, index) => index !== comparisonIndex) })
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
    <div class="heading"><div><strong>Control attributes</strong><small>Cycle requirements declare their minimum registry-backed evidence comparisons; linked tests choose the population and bind roles.</small></div><Button label="Add attribute" icon="pi pi-plus" size="small" outlined @click="add" /></div>
    <article v-for="(attribute, index) in modelValue" :key="index" class="attribute">
      <label>Key<InputText :modelValue="attribute.key" @update:modelValue="replace(index, { key: $event })" /></label>
      <label>Assertion<Select :modelValue="attribute.assertion" :options="assertions" @update:modelValue="replace(index, { assertion: $event })" /></label>
      <label>Evidence strategy<Select :modelValue="attribute.evidence_kind" :options="evidenceKinds" optionLabel="label" optionValue="id" @update:modelValue="setEvidenceKind(index, String($event))" /></label>
      <label v-if="attribute.evidence_kind === 'transaction_cycle'">Cycle pack<Select :modelValue="transactionPackId(attribute)" :options="packOptions(index)" optionLabel="label" optionValue="id" placeholder="No pack installed" @update:modelValue="setPack(index, String($event))" /></label>
      <label class="wide">Requirement<Textarea :modelValue="attribute.requirement" rows="2" autoResize @update:modelValue="replace(index, { requirement: $event })" /></label>
      <label v-if="attribute.evidence_kind === 'transaction_cycle'" class="wide">Required record kinds<MultiSelect :modelValue="attribute.required_record_kinds ?? []" :options="(packFor(attribute)?.record_kinds ?? []).filter(item => item.bindable)" optionLabel="label" optionValue="id" display="chip" filter @update:modelValue="setRequiredRecordKinds(index, $event as string[])" /></label>
      <small v-if="attribute.evidence_kind === 'transaction_cycle' && (attribute.required_record_kinds?.length ?? 0) < minCycleRecordKinds" class="wide warn">A transaction cycle links at least {{ minCycleRecordKinds }} record kinds. For a requirement answered by one record, use Document content; for one the imported tables can answer across the population, use Tabular population.</small>
      <section v-if="attribute.evidence_kind === 'transaction_cycle'" class="comparison-list wide">
        <div class="comparison-heading"><div><strong>Required comparisons</strong><small>Every generated Cycle vouch test must cover these exact selectors.</small></div><Button label="Add comparison" icon="pi pi-plus" size="small" outlined :disabled="attribute.required_record_kinds.length < minCycleRecordKinds" @click="addComparison(index)" /></div>
        <article v-for="(comparison, comparisonIndex) in comparisonValues(attribute)" :key="comparisonIndex" class="comparison">
          <label>Key<InputText :modelValue="comparison.key" @update:modelValue="replaceComparison(index, comparisonIndex, { key: String($event) })" /></label>
          <label>Label<InputText :modelValue="comparison.label" @update:modelValue="replaceComparison(index, comparisonIndex, { label: String($event) })" /></label>
          <label>Operator<Select :modelValue="comparison.operator" :options="comparisonOperators" @update:modelValue="setComparisonOperator(index, comparisonIndex, $event as CycleOperator)" /></label>
          <label>Left record<Select :modelValue="comparison.left.record_kind" :options="attribute.required_record_kinds" @update:modelValue="setComparisonRecordKind(index, comparisonIndex, 'left', String($event))" /></label>
          <label>Left field<Select :modelValue="selectorValue(comparison.left.field)" :options="fieldOptions(attribute, comparison.left.record_kind)" optionLabel="label" optionValue="value" filter @update:modelValue="setComparisonField(index, comparisonIndex, 'left', String($event))" /></label>
          <template v-if="comparison.operator !== 'present' && comparison.right">
            <label>Right record<Select :modelValue="comparison.right.record_kind" :options="attribute.required_record_kinds" @update:modelValue="setComparisonRecordKind(index, comparisonIndex, 'right', String($event))" /></label>
            <label>Right field<Select :modelValue="selectorValue(comparison.right.field)" :options="fieldOptions(attribute, comparison.right.record_kind)" optionLabel="label" optionValue="value" filter @update:modelValue="setComparisonField(index, comparisonIndex, 'right', String($event))" /></label>
          </template>
          <template v-if="comparison.operator === 'numeric_within' && typeof comparison.tolerance === 'object'">
            <label>Absolute tolerance<InputNumber :modelValue="comparison.tolerance.absolute" :min="0" @update:modelValue="replaceComparison(index, comparisonIndex, { tolerance: { ...comparison.tolerance, absolute: Number($event ?? 0) } })" /></label>
            <label>Percent tolerance<InputNumber :modelValue="comparison.tolerance.percent" :min="0" @update:modelValue="replaceComparison(index, comparisonIndex, { tolerance: { ...comparison.tolerance, percent: Number($event ?? 0) } })" /></label>
          </template>
          <label v-if="comparison.operator === 'date_within'">Day tolerance<InputNumber :modelValue="typeof comparison.tolerance === 'number' ? comparison.tolerance : 0" :min="0" :useGrouping="false" @update:modelValue="replaceComparison(index, comparisonIndex, { tolerance: Number($event ?? 0) })" /></label>
          <Button icon="pi pi-trash" label="Remove comparison" text severity="danger" size="small" @click="removeComparison(index, comparisonIndex)" />
        </article>
        <small v-if="!attribute.required_comparisons.length" class="warn">At least one direct comparison is required. If no registered comparison can answer the requirement, choose Manual inspection, Inquiry, or Mixed.</small>
      </section>
      <Button icon="pi pi-trash" label="Remove" text severity="danger" size="small" :disabled="modelValue.length <= 1" @click="remove(index)" />
    </article>
  </section>
</template>

<style scoped>
.attributes { display:flex; flex-direction:column; gap:.65rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface) }
.heading { display:flex; align-items:center; justify-content:space-between; gap:1rem }.heading>div { display:flex; flex-direction:column; gap:.2rem }.heading small { color:var(--aw-muted); font-weight:400 }
.attribute { display:grid; grid-template-columns:1fr 1fr 1fr; gap:.65rem; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-canvas) }
.comparison-list { display:flex; flex-direction:column; gap:.55rem; padding:.65rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control) }
.comparison-heading { display:flex; justify-content:space-between; align-items:center; gap:.75rem }.comparison-heading>div { display:flex; flex-direction:column; gap:.15rem }.comparison-heading small { color:var(--aw-muted); font-weight:400 }
.comparison { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.55rem; padding:.6rem; background:var(--aw-surface); border-radius:var(--aw-radius-control) }.comparison>.p-button { justify-self:start }
label { display:flex; flex-direction:column; gap:.3rem; min-width:0; color:var(--aw-ink-soft); font-size:var(--aw-text-sm); font-weight:600 }.wide { grid-column:1/-1 }.attribute>.p-button { justify-self:start }
.warn { color:var(--aw-warn); font-size:var(--aw-text-sm) }
@media(max-width:800px){.attribute,.comparison{grid-template-columns:1fr}.wide{grid-column:auto}}
</style>
