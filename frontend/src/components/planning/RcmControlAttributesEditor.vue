<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type {
  CycleVouchMetadata,
  DocumentSchemaCatalogEntry,
  RcmControlAttribute,
  RcmSchemaComparison,
} from '../../types'

const props = defineProps<{
  modelValue: RcmControlAttribute[]
  metadata: CycleVouchMetadata | null
  /** This engagement's induced schemas: the whole vocabulary a comparison may
   *  address. Empty until classification and induction have run, which is what
   *  the RCM stage waits for. */
  schemas?: DocumentSchemaCatalogEntry[]
}>()
const emit = defineEmits<{ 'update:modelValue': [RcmControlAttribute[]] }>()

const assertions = ['Existence', 'Completeness', 'Accuracy', 'Authorization', 'Valuation', 'Cut-off', 'Compliance', 'Operational']
const evidenceKinds = computed(() => props.metadata?.registry?.evidence_kinds ?? [
  { id: 'transaction_cycle', label: 'Transaction cycle' },
  { id: 'tabular_population', label: 'Tabular population' },
  { id: 'document_content', label: 'Document content' },
  { id: 'manual_inspection', label: 'Manual inspection' },
  { id: 'inquiry', label: 'Inquiry' },
  { id: 'mixed', label: 'Mixed evidence' },
])
const comparisonShapes = [
  { label: 'two fields agree', value: true },
  { label: 'a field is stated', value: false },
]

const schemaCatalog = computed(() => props.schemas ?? [])

/** Type name, how many documents carry it, and what separates it from its
 *  neighbours. The population is not decoration: a requirement is written
 *  against one, and a type carrying a single document cannot answer it. An
 *  engagement that coined a type for one anomalous document had it chosen as
 *  the deal-record side of three population-wide comparisons on its name alone,
 *  which is the only thing a bare list of type names offers a reader. */
const documentTypeOptions = computed(() =>
  schemaCatalog.value.map(entry => ({
    label: `${entry.document_type} · ${entry.documents === 1 ? '1 document' : `${entry.documents ?? 0} documents`}`,
    value: entry.document_type,
    documents: entry.documents ?? 0,
    discriminator: entry.discriminator ?? '',
  })),
)

/** The fields one type states, labelled with the role that decides what a
 *  comparison over them means. */
function schemaFieldOptions(documentType: string) {
  const entry = schemaCatalog.value.find(item => item.document_type === documentType)
  return (entry?.fields ?? []).map(field => ({
    label: `${field.label || field.name} · ${field.role} (${field.value_type})`,
    value: field.name,
  }))
}

function replace(index: number, changes: Record<string, unknown>) {
  const values = props.modelValue.map(item => ({ ...item })) as Array<Record<string, unknown>>
  values[index] = { ...values[index], ...changes }
  emit('update:modelValue', values as unknown as RcmControlAttribute[])
}

function setEvidenceKind(index: number, evidenceKind: string) {
  // Comparisons belong to the transaction-cycle strategy and mean nothing under
  // any other, so they go with it rather than being left behind to be rejected.
  //
  // Switching *to* transaction cycle with none to carry leaves the attribute
  // uncontracted rather than stating an empty contract: absent means the cycle
  // design has not decided yet, empty means it decided nothing must agree, and
  // the second is refused on save.
  const carried = comparisons(props.modelValue[index])
  replace(index, {
    evidence_kind: evidenceKind,
    required_comparisons:
      evidenceKind === 'transaction_cycle' && carried.length ? carried : undefined,
  })
}

function comparisons(attribute: RcmControlAttribute): RcmSchemaComparison[] {
  return attribute.evidence_kind === 'transaction_cycle'
    ? (attribute.required_comparisons as RcmSchemaComparison[]) ?? []
    : []
}

function replaceComparison(
  attributeIndex: number,
  comparisonIndex: number,
  changes: Partial<RcmSchemaComparison>,
) {
  const updated = comparisons(props.modelValue[attributeIndex]).map((item, index) =>
    index === comparisonIndex ? { ...item, ...changes } : item,
  )
  replace(attributeIndex, { required_comparisons: updated })
}

function setOperand(
  attributeIndex: number,
  comparisonIndex: number,
  side: 'left' | 'right',
  changes: { document_type?: string; field?: string },
) {
  const comparison = comparisons(props.modelValue[attributeIndex])[comparisonIndex]
  const operand = { ...(side === 'left' ? comparison.left : comparison.right ?? {}), ...changes }
  // Changing the type invalidates the field: a field name means nothing away
  // from the type that states it, and carrying it across would silently point
  // the requirement at something no schema has.
  if (changes.document_type) {
    operand.field = schemaFieldOptions(changes.document_type)[0]?.value ?? ''
  }
  replaceComparison(attributeIndex, comparisonIndex, {
    [side]: operand as { document_type: string; field: string },
  })
}

/** A comparison reads one field or two: that it is stated, or that they agree.
 *  How to compare them is settled against the values, not chosen here. */
function setComparesTwo(attributeIndex: number, comparisonIndex: number, comparesTwo: boolean) {
  if (!comparesTwo) {
    replaceComparison(attributeIndex, comparisonIndex, { right: null })
    return
  }
  const type = documentTypeOptions.value[1]?.value ?? documentTypeOptions.value[0]?.value ?? ''
  replaceComparison(attributeIndex, comparisonIndex, {
    right: { document_type: type, field: schemaFieldOptions(type)[0]?.value ?? '' },
  })
}

function addComparison(attributeIndex: number) {
  const existing = comparisons(props.modelValue[attributeIndex])
  const leftType = documentTypeOptions.value[0]?.value ?? ''
  const rightType = documentTypeOptions.value[1]?.value ?? leftType
  replace(attributeIndex, {
    required_comparisons: [
      ...existing,
      {
        key: `comparison_${existing.length + 1}`,
        left: { document_type: leftType, field: schemaFieldOptions(leftType)[0]?.value ?? '' },
        right: { document_type: rightType, field: schemaFieldOptions(rightType)[0]?.value ?? '' },
        rationale: '',
      },
    ],
  })
}

function removeComparison(attributeIndex: number, comparisonIndex: number) {
  // Removing the last one returns the attribute to uncontracted, not to a
  // contract requiring nothing.
  const remaining = comparisons(props.modelValue[attributeIndex]).filter(
    (_, index) => index !== comparisonIndex,
  )
  replace(attributeIndex, {
    required_comparisons: remaining.length ? remaining : undefined,
  })
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
    <div class="heading"><div><strong>Control attributes</strong><small>A transaction-cycle requirement states what must agree, written against the fields this engagement's documents carry.</small></div><Button label="Add attribute" icon="pi pi-plus" size="small" outlined @click="add" /></div>
    <article v-for="(attribute, index) in modelValue" :key="index" class="attribute">
      <label>Key<InputText :modelValue="attribute.key" @update:modelValue="replace(index, { key: $event })" /></label>
      <label>Assertion<Select :modelValue="attribute.assertion" :options="assertions" @update:modelValue="replace(index, { assertion: $event })" /></label>
      <label>Evidence strategy<Select :modelValue="attribute.evidence_kind" :options="evidenceKinds" optionLabel="label" optionValue="id" @update:modelValue="setEvidenceKind(index, String($event))" /></label>
      <label class="wide">Requirement<Textarea :modelValue="attribute.requirement" rows="2" autoResize @update:modelValue="replace(index, { requirement: $event })" /></label>

      <section v-if="attribute.evidence_kind === 'transaction_cycle'" class="comparison-list wide">
        <div class="comparison-heading">
          <div>
            <strong>What must agree</strong>
            <small>
              Every generated cycle test must assert exactly these, so a
              comparison that is merely nearby proves something else.
            </small>
          </div>
          <Button label="Add comparison" icon="pi pi-plus" size="small" outlined :disabled="!documentTypeOptions.length" @click="addComparison(index)" />
        </div>
        <small v-if="!documentTypeOptions.length" class="warn">
          No document type has an induced schema yet, so there are no fields to
          write a requirement against. Classify and induce first.
        </small>
        <article v-for="(comparison, comparisonIndex) in comparisons(attribute)" :key="comparisonIndex" class="comparison">
          <label>Key<InputText :modelValue="comparison.key" @update:modelValue="replaceComparison(index, comparisonIndex, { key: String($event) })" /></label>
          <label>Requires<Select :modelValue="Boolean(comparison.right)" :options="comparisonShapes" optionLabel="label" optionValue="value" @update:modelValue="setComparesTwo(index, comparisonIndex, Boolean($event))" /></label>
          <span />
          <label>Left document<Select :modelValue="comparison.left.document_type" :options="documentTypeOptions" optionLabel="label" optionValue="value" filter @update:modelValue="setOperand(index, comparisonIndex, 'left', { document_type: String($event) })">
            <template #option="{ option }"><span class="type-option"><span :class="{ scarce: option.documents <= 1 }">{{ option.label }}</span><small v-if="option.discriminator">{{ option.discriminator }}</small></span></template>
          </Select></label>
          <label>Left field<Select :modelValue="comparison.left.field" :options="schemaFieldOptions(comparison.left.document_type)" optionLabel="label" optionValue="value" filter @update:modelValue="setOperand(index, comparisonIndex, 'left', { field: String($event) })" /></label>
          <span />
          <template v-if="comparison.right">
            <label>Right document<Select :modelValue="comparison.right.document_type" :options="documentTypeOptions" optionLabel="label" optionValue="value" filter @update:modelValue="setOperand(index, comparisonIndex, 'right', { document_type: String($event) })">
              <template #option="{ option }"><span class="type-option"><span :class="{ scarce: option.documents <= 1 }">{{ option.label }}</span><small v-if="option.discriminator">{{ option.discriminator }}</small></span></template>
            </Select></label>
            <label>Right field<Select :modelValue="comparison.right.field" :options="schemaFieldOptions(comparison.right.document_type)" optionLabel="label" optionValue="value" filter @update:modelValue="setOperand(index, comparisonIndex, 'right', { field: String($event) })" /></label>
            <span />
          </template>
          <label class="wide">What these fields must show<InputText :modelValue="comparison.rationale ?? ''" @update:modelValue="replaceComparison(index, comparisonIndex, { rationale: String($event) })" /></label>
          <Button icon="pi pi-trash" label="Remove comparison" text severity="danger" size="small" @click="removeComparison(index, comparisonIndex)" />
        </article>
        <small v-if="!comparisons(attribute).length && documentTypeOptions.length" class="note">
          Which fields must agree is decided by the cycle design, against this
          engagement's own documents. State them here to settle it yourself; leave
          them unstated and the cycle design will, or will report that no field
          these documents carry can answer the requirement.
        </small>
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
.note { color:var(--aw-muted); font-size:var(--aw-text-sm) }
.type-option { display:flex; flex-direction:column; gap:.1rem; min-width:0; max-width:32rem }
.type-option small { color:var(--aw-muted); font-weight:400; font-size:var(--aw-text-xs); white-space:normal }
/* A type carrying one document cannot answer a population requirement. */
.type-option .scarce { color:var(--aw-warn) }
@media(max-width:800px){.attribute,.comparison{grid-template-columns:1fr}.wide{grid-column:auto}}
</style>
