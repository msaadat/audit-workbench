<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Tag from 'primevue/tag'

import { api } from '../../api'
import type { CycleRuleset, CycleRulesetListing, JoinKeyMeasurement } from '../../types'

const props = defineProps<{ workspaceId: string; approverId: string }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{
  approved: [ruleset: CycleRuleset]
  error: [summary: string, error: unknown]
}>()

const listing = ref<CycleRulesetListing | null>(null)
const selected = ref<CycleRuleset | null>(null)
const busy = ref(false)

const base = computed(() => `/api/workspaces/${props.workspaceId}/cycle-rulesets`)

/** Rules an auditor can still act on. A superseded or rejected one is history. */
const reviewable = computed(
  () => (listing.value?.items ?? []).filter(item => item.status === 'proposed'),
)

const concernsByRule = computed(() => {
  const grouped = new Map<string, string[]>()
  for (const concern of selected.value?.concerns ?? []) {
    grouped.set(concern.rule, [...(grouped.get(concern.rule) ?? []), concern.detail])
  }
  return grouped
})

/** Fan-out is the number a reviewer approves a join key on: about one means a
 *  transaction key, many means an entity identifier that would fuse the cycle. */
function fanOut(id: string): JoinKeyMeasurement | null {
  return selected.value?.measured?.join_keys?.[id] ?? null
}

function roleType(name: string): string {
  return selected.value?.roles.find(role => role.name === name)?.document_type ?? name
}

async function load(): Promise<void> {
  busy.value = true
  try {
    listing.value = await api.get<CycleRulesetListing>(base.value)
    const first = (listing.value.items ?? []).find(item => item.status === 'proposed')
    selected.value = first ? await api.get<CycleRuleset>(`${base.value}/${first.ruleset_id}`) : null
  } catch (error) {
    emit('error', 'Could not load the proposed cycle rules.', error)
  } finally {
    busy.value = false
  }
}

async function select(rulesetId: string): Promise<void> {
  try {
    selected.value = await api.get<CycleRuleset>(`${base.value}/${rulesetId}`)
  } catch (error) {
    emit('error', 'Could not load that ruleset.', error)
  }
}

async function approve(): Promise<void> {
  const ruleset = selected.value
  if (!ruleset) return
  busy.value = true
  try {
    const approved = await api.post<CycleRuleset>(
      `${base.value}/${ruleset.ruleset_id}/approve`,
      { approved_by: props.approverId },
    )
    emit('approved', approved)
    await load()
  } catch (error) {
    emit('error', 'Could not approve these rules.', error)
  } finally {
    busy.value = false
  }
}

async function reject(): Promise<void> {
  const ruleset = selected.value
  if (!ruleset) return
  busy.value = true
  try {
    await api.post(`${base.value}/${ruleset.ruleset_id}/reject`, {})
    await load()
  } catch (error) {
    emit('error', 'Could not reject these rules.', error)
  } finally {
    busy.value = false
  }
}

watch(visible, open => { if (open) void load() }, { immediate: true })
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    header="Cycle rules"
    :style="{ width: '60rem', maxWidth: '96vw' }"
  >
    <Message v-if="!busy && !reviewable.length" severity="info" :closable="false">
      No cycle rules are waiting for review.
    </Message>

    <template v-else-if="selected">
      <div v-if="reviewable.length > 1" class="ruleset-picker">
        <Button
          v-for="item in reviewable"
          :key="item.ruleset_id"
          :label="item.cycle_label || item.ruleset_id"
          size="small"
          :outlined="item.ruleset_id !== selected.ruleset_id"
          @click="select(item.ruleset_id)"
        />
      </div>

      <Message
        v-if="selected.concerns.length"
        severity="warn"
        :closable="false"
        class="mb-3"
      >
        Measured against this engagement's documents, {{ selected.concerns.length }}
        of these rules behave in a way worth checking before you approve them.
      </Message>

      <section class="rule-group">
        <h4>Roles</h4>
        <p class="rule-group__why">
          A role is a position in the cycle, not a document type — which is what
          lets one cycle hold two of the same type.
        </p>
        <ul class="rule-list">
          <li v-for="role in selected.roles" :key="role.name">
            <strong>{{ role.name }}</strong> — {{ role.document_type }}
            <Tag v-if="!role.required" value="optional" severity="secondary" />
            <Tag v-if="role.cardinality === 'many'" value="many" severity="secondary" />
          </li>
        </ul>
      </section>

      <section class="rule-group">
        <h4>Join keys</h4>
        <p class="rule-group__why">
          These decide which documents belong together. A key whose values reach
          about one record is a transaction reference; one reaching many is an
          entity identifier, and joining on it would fuse unrelated transactions.
        </p>
        <div v-for="key in selected.join_keys" :key="key.id" class="rule">
          <div class="rule__what">
            <code>{{ roleType(key.left.role) }}.{{ key.left.field }}</code>
            <span>=</span>
            <code>{{ roleType(key.right.role) }}.{{ key.right.field }}</code>
          </div>
          <p v-if="key.rationale" class="rule__why">{{ key.rationale }}</p>
          <dl v-if="fanOut(key.id)" class="rule__measured">
            <div>
              <dt>Reaches per value (p95)</dt>
              <dd :class="{ 'rule__measured--alarming': (fanOut(key.id)!.fan_out_p95 ?? 0) > 5 }">
                {{ fanOut(key.id)!.fan_out_p95 }}
              </dd>
            </div>
            <div><dt>Matched</dt><dd>{{ fanOut(key.id)!.matched_pairs }}</dd></div>
            <div><dt>Unmatched</dt><dd>{{ fanOut(key.id)!.left_unmatched }}</dd></div>
          </dl>
          <Message
            v-for="(detail, index) in concernsByRule.get(key.id) ?? []"
            :key="index"
            severity="warn"
            :closable="false"
          >{{ detail }}</Message>
        </div>
      </section>

      <section class="rule-group">
        <h4>Assertions</h4>
        <p class="rule-group__why">
          What must agree once the documents are linked. These are the tests.
        </p>
        <div v-for="check in selected.assertions" :key="check.id" class="rule">
          <div class="rule__what">
            <strong>{{ check.label || check.id }}</strong>
            <Tag :value="check.operator" severity="secondary" />
          </div>
          <p v-if="check.rationale" class="rule__why">{{ check.rationale }}</p>
          <Message
            v-for="(detail, index) in concernsByRule.get(check.id) ?? []"
            :key="index"
            severity="warn"
            :closable="false"
          >{{ detail }}</Message>
        </div>
      </section>
    </template>

    <template #footer>
      <span class="ruleset-footnote">
        Approving makes these rules the ones results are produced under.
      </span>
      <Button label="Reject" severity="secondary" text :disabled="!selected || busy" @click="reject" />
      <Button label="Approve" :loading="busy" :disabled="!selected" @click="approve" />
      <Button label="Close" text @click="visible = false" />
    </template>
  </Dialog>
</template>

<style scoped>
.ruleset-picker { display: flex; gap: 0.5rem; margin-bottom: 0.75rem; flex-wrap: wrap; }
.rule-group { margin-bottom: 1.25rem; }
.rule-group h4 { margin: 0 0 0.25rem; }
.rule-group__why { margin: 0 0 0.75rem; font-size: 0.85rem; color: var(--p-text-muted-color); }
.rule-list { margin: 0; padding-left: 1.1rem; }
.rule {
  padding: 0.6rem 0;
  border-top: 1px solid var(--p-content-border-color);
}
.rule__what { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.rule__why { margin: 0.25rem 0 0; font-size: 0.85rem; color: var(--p-text-muted-color); }
.rule__measured { display: flex; gap: 1.5rem; margin: 0.5rem 0 0; }
.rule__measured dt { font-size: 0.75rem; color: var(--p-text-muted-color); }
.rule__measured dd { margin: 0; font-variant-numeric: tabular-nums; font-weight: 600; }
.rule__measured--alarming { color: var(--p-red-500, #ef4444); }
.ruleset-footnote {
  margin-right: auto;
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
</style>
