<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'

import { api } from '../../api'
import type {
  ColumnSchema,
  CycleAssertion,
  CycleAssertionMutationResponse,
  CycleAssertionPlacement,
  CycleFieldSelector,
  CycleOperand,
  CycleOperator,
  CyclePackDescriptor,
  CycleVouchMetadata,
  DocTest,
  EvidenceSemanticType,
} from '../../types'

const props = defineProps<{
  workspaceId: string
  testId: string
  expectedTestSha1: string
  metadata: CycleVouchMetadata | null
}>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{
  saved: [response: CycleAssertionMutationResponse]
  error: [summary: string, error: unknown]
}>()

type OperandSource = 'row' | 'role' | 'roles'
interface OperandDraft {
  source: OperandSource
  column: string
  role: string
  roles: string[]
  field: string
  entryQuantifier: 'one' | 'any' | 'all'
}

const test = ref<DocTest | null>(null)
const schema = ref<ColumnSchema[]>([])
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const editKey = ref('')
const key = ref('')
const assertionLabel = ref('')
const operator = ref<CycleOperator>('equal_exact')
const roleQuantifier = ref<'all' | 'any'>('all')
const absoluteTolerance = ref(0)
const percentTolerance = ref(0)
const dayTolerance = ref(0)
const placementMode = ref<'end' | 'before' | 'after'>('end')
const placementKey = ref('')
const left = reactive<OperandDraft>(emptyOperand())
const right = reactive<OperandDraft>(emptyOperand())

const definition = computed(() => test.value?.definition)
const roles = computed(() => definition.value?.roles ?? [])
const assertions = computed(() => definition.value?.assertions ?? [])
const exactPack = computed<CyclePackDescriptor | null>(() => {
  const reference = test.value?.registry
  if (!reference) return null
  return props.metadata?.registry.packs.find(pack =>
    pack.id === reference.pack_id
    && pack.version === reference.pack_version
    && pack.definition_hash === reference.definition_hash,
  ) ?? null
})
const staleProjection = computed(() => Boolean(
  test.value && test.value.sha1 !== props.expectedTestSha1,
))
const isPresent = computed(() => operator.value === 'present')
const requiredType = computed<EvidenceSemanticType | null>(() => ({
  numeric_within: 'number',
  date_on_or_before: 'date',
  date_within: 'date',
} as Partial<Record<CycleOperator, EvidenceSemanticType>>)[operator.value] ?? null)
const placementOptions = computed(() => assertions.value.filter(item => item.key !== editKey.value))
const canSave = computed(() => Boolean(
  test.value
  && exactPack.value
  && assertionLabel.value.trim()
  && operandReady(left)
  && (isPresent.value || operandReady(right))
  && !staleProjection.value
  && (editKey.value || placementMode.value === 'end' || placementKey.value),
))

function emptyOperand(): OperandDraft {
  return { source: 'role', column: '', role: '', roles: [], field: '', entryQuantifier: 'one' }
}

function resetOperand(target: OperandDraft) {
  Object.assign(target, emptyOperand())
}

function resetForm() {
  editKey.value = ''
  key.value = ''
  assertionLabel.value = ''
  operator.value = 'equal_exact'
  roleQuantifier.value = 'all'
  absoluteTolerance.value = 0
  percentTolerance.value = 0
  dayTolerance.value = 0
  placementMode.value = 'end'
  placementKey.value = ''
  resetOperand(left)
  resetOperand(right)
}

function semanticType(column: ColumnSchema): EvidenceSemanticType {
  return column.kind === 'numeric' ? 'number' : column.kind
}

function rowColumns(side: OperandDraft) {
  const wanted = requiredType.value
  const other = side === left ? right : left
  const pairedType = !wanted && other.source !== 'row' && other.field
    ? fieldOptions(other).find(field => field.value === other.field)?.semanticType
    : null
  return schema.value.filter(column => !wanted || semanticType(column) === wanted)
    .filter(column => !pairedType || semanticType(column) === pairedType)
}

function selectedRoleNames(side: OperandDraft): string[] {
  return side.source === 'roles' ? side.roles : side.role ? [side.role] : []
}

function fieldOptions(side: OperandDraft) {
  const pack = exactPack.value
  const namedRoles = selectedRoleNames(side)
  if (!pack || !namedRoles.length) return []
  const roleRecords = namedRoles.map(name => {
    const role = roles.value.find(value => value.role === name)
    return pack.record_kinds.find(record => record.id === role?.record_kind)
  })
  const wanted = requiredType.value
  const requiredPresenceRole = isPresent.value
    && side === left
    && namedRoles.length === 1
    && roles.value.find(value => value.role === namedRoles[0])?.required
  return pack.field_kinds.flatMap(field => field.attributes
    .filter(attribute => (!wanted || attribute.semantic_type === wanted)
      && (!requiredPresenceRole || attribute.control_evidence === true)
      && roleRecords.every(record => record?.available_field_kinds.includes(field.id)))
    .map(attribute => ({
      value: `${field.group}|${field.kind}|${attribute.id}`,
      label: `${field.label} · ${attribute.id.replaceAll('_', ' ')}`,
      semanticType: attribute.semantic_type,
    })))
}

function operandReady(side: OperandDraft): boolean {
  if (side.source === 'row') return Boolean(side.column) && !isPresent.value
  if (side.source === 'role') return Boolean(side.role && side.field)
  return Boolean(side.roles.length && side.field)
}

function selector(value: string): CycleFieldSelector {
  const [group, kind, attribute] = value.split('|')
  return { group, kind, attribute }
}

function buildOperand(side: OperandDraft): CycleOperand {
  if (side.source === 'row') return { source: 'row', column: side.column }
  if (side.source === 'role') {
    return { source: 'role', role: side.role, field: selector(side.field) }
  }
  return {
    source: 'roles', roles: [...side.roles], field: selector(side.field),
    entry_quantifier: side.entryQuantifier,
  }
}

function loadOperand(target: OperandDraft, operand: CycleOperand | undefined) {
  resetOperand(target)
  if (!operand) return
  target.source = operand.source
  if (operand.source === 'row') target.column = operand.column
  else {
    target.field = `${operand.field.group}|${operand.field.kind}|${operand.field.attribute}`
    if (operand.source === 'role') target.role = operand.role
    else {
      target.roles = [...operand.roles]
      target.entryQuantifier = operand.entry_quantifier
    }
  }
}

function selectExisting(assertionKey: string) {
  editKey.value = assertionKey
  const assertion = assertions.value.find(value => value.key === assertionKey)
  if (!assertion) {
    resetForm()
    return
  }
  key.value = assertion.key
  assertionLabel.value = assertion.label
  operator.value = assertion.operator
  roleQuantifier.value = assertion.role_quantifier ?? 'all'
  absoluteTolerance.value = typeof assertion.tolerance === 'object' ? assertion.tolerance.absolute : 0
  percentTolerance.value = typeof assertion.tolerance === 'object' ? assertion.tolerance.percent : 0
  dayTolerance.value = typeof assertion.tolerance === 'number' ? assertion.tolerance : 0
  loadOperand(left, assertion.left)
  loadOperand(right, assertion.right)
  placementMode.value = 'end'
  placementKey.value = ''
}

async function loadAuthoringContext() {
  loading.value = true
  loadError.value = ''
  try {
    test.value = await api.get<DocTest>(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}`,
    )
    const table = test.value.definition?.population.table
    schema.value = table
      ? (await api.get<{ columns: ColumnSchema[] }>(
          `/api/workspaces/${props.workspaceId}/tables/${table}/schema`,
        )).columns
      : []
    if (!exactPack.value) throw new Error('The test registry descriptor is unavailable or stale.')
    if (staleProjection.value) throw new Error('The Cycle vouch grid changed. Close authoring and reload it before saving.')
    resetForm()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : String(error)
    emit('error', 'Could not load assertion authoring', error)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!canSave.value) return
  const assertion: Omit<CycleAssertion, 'key'> & { key?: string } = {
    ...(key.value.trim() ? { key: key.value.trim() } : {}),
    label: assertionLabel.value.trim(),
    left: buildOperand(left),
    operator: operator.value,
    ...(!isPresent.value ? { right: buildOperand(right) } : {}),
    ...((left.source === 'roles' || right.source === 'roles') && !isPresent.value
      ? { role_quantifier: roleQuantifier.value } : {}),
    ...(operator.value === 'numeric_within'
      ? { tolerance: { absolute: absoluteTolerance.value, percent: percentTolerance.value } }
      : operator.value === 'date_within' ? { tolerance: Math.trunc(dayTolerance.value) } : {}),
  }
  const placement: CycleAssertionPlacement | undefined = !editKey.value && placementMode.value !== 'end'
    ? placementMode.value === 'before'
      ? { before_key: placementKey.value }
      : { after_key: placementKey.value }
    : undefined
  saving.value = true
  try {
    const response = await api.post<CycleAssertionMutationResponse>(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}/assertions`,
      {
        expected_test_sha1: props.expectedTestSha1,
        assertion,
        ...(placement ? { placement } : {}),
      },
    )
    emit('saved', response)
    visible.value = false
  } catch (error) {
    emit('error', 'Could not save the Cycle vouch assertion', error)
  } finally {
    saving.value = false
  }
}

watch(visible, open => {
  if (open) void loadAuthoringContext()
}, { immediate: true })
watch(operator, () => {
  if (isPresent.value && left.source === 'row') left.source = 'role'
})
</script>

<template>
  <Dialog v-model:visible="visible" modal header="Add or change assertion" :style="{ width: 'min(54rem, 95vw)' }">
    <Message severity="info" :closable="false">
      Saving changes only the canonical test definition. Existing matching cells and evidence stay intact; new or changed cells become pending, and prior sign-off becomes stale until you rerun and sign off again.
    </Message>
    <p v-if="loading" class="muted">Loading the exact test and registry descriptors…</p>
    <Message v-else-if="loadError" severity="error" :closable="false">{{ loadError }}</Message>
    <div v-else-if="test && exactPack" class="assertion-form">
      <label class="wide">Authoring mode
        <select data-testid="author-mode" :value="editKey" @change="selectExisting(($event.target as HTMLSelectElement).value)">
          <option value="">Add a new assertion</option>
          <option v-for="assertion in assertions" :key="assertion.key" :value="assertion.key">Change · {{ assertion.label }}</option>
        </select>
      </label>
      <div class="registry-note wide"><strong>{{ exactPack.label }}</strong><span>{{ test.registry?.pack_id }} v{{ test.registry?.pack_version }} · exact descriptor hash</span></div>
      <label>Structural key
        <input v-model="key" data-testid="assertion-key" :readonly="Boolean(editKey)" placeholder="Assigned from the label when blank" />
      </label>
      <label>Operator
        <select v-model="operator" data-testid="assertion-operator">
          <option v-for="value in metadata?.operators ?? []" :key="value" :value="value">{{ value.replaceAll('_', ' ') }}</option>
        </select>
      </label>
      <label class="wide">Auditor-facing label<input v-model="assertionLabel" data-testid="assertion-label" /></label>

      <fieldset>
        <legend>Left operand</legend>
        <label>Source<select v-model="left.source" data-testid="left-source"><option value="role">One role</option><option value="roles">Role set</option><option v-if="!isPresent" value="row">Population column</option></select></label>
        <label v-if="left.source === 'row'">Column<select v-model="left.column"><option value="">Select column</option><option v-for="column in rowColumns(left)" :key="column.name" :value="column.name">{{ column.name }} · {{ semanticType(column) }}</option></select></label>
        <label v-else-if="left.source === 'role'">Role<select v-model="left.role" data-testid="left-role"><option value="">Select role</option><option v-for="role in roles" :key="role.role" :value="role.role">{{ role.role }} · {{ role.record_kind }}</option></select></label>
        <label v-else>Roles<select v-model="left.roles" multiple><option v-for="role in roles" :key="role.role" :value="role.role">{{ role.role }} · {{ role.record_kind }}</option></select></label>
        <label v-if="left.source !== 'row'">Typed field<select v-model="left.field" data-testid="left-field"><option value="">Select field</option><option v-for="field in fieldOptions(left)" :key="field.value" :value="field.value">{{ field.label }} · {{ field.semanticType }}</option></select></label>
        <label v-if="left.source === 'roles'">Entry quantifier<select v-model="left.entryQuantifier"><option v-for="value in metadata?.entry_quantifiers ?? []" :key="value" :value="value">{{ value }}</option></select></label>
      </fieldset>

      <fieldset v-if="!isPresent">
        <legend>Right operand</legend>
        <label>Source<select v-model="right.source" data-testid="right-source"><option value="role">One role</option><option value="roles">Role set</option><option value="row">Population column</option></select></label>
        <label v-if="right.source === 'row'">Column<select v-model="right.column"><option value="">Select column</option><option v-for="column in rowColumns(right)" :key="column.name" :value="column.name">{{ column.name }} · {{ semanticType(column) }}</option></select></label>
        <label v-else-if="right.source === 'role'">Role<select v-model="right.role" data-testid="right-role"><option value="">Select role</option><option v-for="role in roles" :key="role.role" :value="role.role">{{ role.role }} · {{ role.record_kind }}</option></select></label>
        <label v-else>Roles<select v-model="right.roles" multiple><option v-for="role in roles" :key="role.role" :value="role.role">{{ role.role }} · {{ role.record_kind }}</option></select></label>
        <label v-if="right.source !== 'row'">Typed field<select v-model="right.field" data-testid="right-field"><option value="">Select field</option><option v-for="field in fieldOptions(right)" :key="field.value" :value="field.value">{{ field.label }} · {{ field.semanticType }}</option></select></label>
        <label v-if="right.source === 'roles'">Entry quantifier<select v-model="right.entryQuantifier"><option v-for="value in metadata?.entry_quantifiers ?? []" :key="value" :value="value">{{ value }}</option></select></label>
      </fieldset>

      <label v-if="(left.source === 'roles' || right.source === 'roles') && !isPresent">Role quantifier<select v-model="roleQuantifier"><option v-for="value in metadata?.role_quantifiers ?? []" :key="value" :value="value">{{ value }}</option></select></label>
      <template v-if="operator === 'numeric_within'"><label>Absolute tolerance<input v-model.number="absoluteTolerance" type="number" min="0" /></label><label>Percent tolerance<input v-model.number="percentTolerance" type="number" min="0" /></label></template>
      <label v-if="operator === 'date_within'">Day tolerance<input v-model.number="dayTolerance" type="number" min="0" step="1" /></label>

      <template v-if="!editKey">
        <label>Placement<select v-model="placementMode"><option value="end">At the end</option><option value="before">Before a column</option><option value="after">After a column</option></select></label>
        <label v-if="placementMode !== 'end'">Relative column<select v-model="placementKey"><option value="">Select assertion</option><option v-for="assertion in placementOptions" :key="assertion.key" :value="assertion.key">{{ assertion.label }}</option></select></label>
      </template>
    </div>
    <template #footer>
      <Button label="Cancel" text @click="visible = false" />
      <Button label="Save assertion" aria-label="Save assertion" icon="pi pi-plus" :loading="saving" :disabled="!canSave" @click="save" />
    </template>
  </Dialog>
</template>

<style scoped>
.assertion-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; margin-top: 1rem; }
.assertion-form label, fieldset { display: grid; gap: .3rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.assertion-form input, .assertion-form select { width: 100%; min-height: 2.35rem; padding: .4rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }
.assertion-form select[multiple] { min-height: 6rem; }
.wide, fieldset { grid-column: 1 / -1; }
fieldset { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); }
legend { color: var(--aw-ink); font-size: var(--aw-text-sm); font-weight: 700; }
.registry-note { display: flex; align-items: center; justify-content: space-between; gap: .5rem; padding: .55rem .7rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.registry-note span, .muted { color: var(--aw-muted); font-size: var(--aw-text-xs); }
@media (max-width: 42rem) { .assertion-form, fieldset { grid-template-columns: 1fr; } .assertion-form > *, fieldset > * { grid-column: 1; } }
</style>
