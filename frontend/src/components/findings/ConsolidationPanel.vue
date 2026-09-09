<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Listbox from 'primevue/listbox'

import type {
  AuditFinding, ConsolidationGroup, ConsolidationMember, ConsolidationPayload, ConsolidationRelation,
} from '../../types'
import { plural } from '../../format'

/**
 * Suggested consolidations, one card per undecided group.
 *
 * The model proposed; the auditor decides. Each card shows what the members
 * share — the records, or only the process — the title the merge would take,
 * the root-cause hypothesis, and the rationale, with the lead defaulting to the
 * model's pick. Nothing merges without Accept. A dismissed suggestion stays
 * dismissed until the findings change, which the header reports as the basis
 * state: current, or outgrown and in need of a refresh.
 *
 * Manual mode is the same card built from the findings the auditor picked, so
 * a group they see and the model did not takes the same merge path.
 */

const props = defineProps<{
  payload: ConsolidationPayload | null
  /** Findings that still stand on their own, offered for a manual group. */
  candidates: AuditFinding[]
  busy: boolean
  agentBusy: boolean
  /** Whether the manual card is open. */
  manual: boolean
}>()
const emit = defineEmits<{
  refresh: []
  accept: [group: ConsolidationGroup, choice: { lead_finding_id: string; title: string; include_confirmed: boolean }]
  dismiss: [group: ConsolidationGroup]
  consolidate: [choice: { finding_ids: string[]; lead_finding_id: string; relation: ConsolidationRelation; title: string; include_confirmed: boolean }]
  closeManual: []
}>()

const RELATION_LABEL: Record<ConsolidationRelation, string> = {
  same_condition: 'same condition',
  shared_cause: 'shared cause',
}

const groups = computed(() => (props.payload?.suggestion?.groups ?? []).filter(group => !group.decision))
const decided = computed(() => (props.payload?.suggestion?.groups ?? []).filter(group => group.decision))

/** Per-card edits: the lead and the title, seeded from the suggestion. */
const edits = reactive<Record<string, { lead: string; title: string }>>({})
watch(groups, items => {
  for (const group of items) {
    if (!edits[group.group_id]) {
      edits[group.group_id] = { lead: group.lead_finding_id, title: group.proposed_title }
    }
  }
}, { immediate: true })

const manualChoice = reactive<{ ids: string[]; lead: string; relation: ConsolidationRelation; title: string }>({
  ids: [], lead: '', relation: 'shared_cause', title: '',
})
watch(() => manualChoice.ids, ids => {
  if (!ids.includes(manualChoice.lead)) manualChoice.lead = ids[0] ?? ''
})
const manualOptions = computed(() => props.candidates.map(item => ({
  label: `${item.id} · ${item.title}`, value: item.id,
})))
const manualLeadOptions = computed(() => manualChoice.ids.map(id => {
  const item = props.candidates.find(candidate => candidate.id === id)
  return { label: item ? `${item.id} · ${item.title}` : id, value: id }
}))

function membersOf(group: ConsolidationGroup): ConsolidationMember[] {
  return group.members ?? group.finding_ids.map(id => ({ id, title: id, severity: 'medium' as const, process: '', test_refs: [] }))
}
function confirmedIn(group: ConsolidationGroup): boolean {
  return membersOf(group).some(member => member.auditor_confirmed)
}
function sharedEntries(group: ConsolidationGroup): Array<{ key: string; ids: string[] }> {
  return Object.entries(group.shared_entities ?? {}).map(([key, ids]) => ({ key, ids }))
}

function accept(group: ConsolidationGroup) {
  const edit = edits[group.group_id] ?? { lead: group.lead_finding_id, title: group.proposed_title }
  emit('accept', group, {
    lead_finding_id: edit.lead,
    title: edit.title.trim(),
    include_confirmed: confirmedIn(group),
  })
}
function consolidate() {
  if (manualChoice.ids.length < 2 || !manualChoice.lead) return
  emit('consolidate', {
    finding_ids: [...manualChoice.ids],
    lead_finding_id: manualChoice.lead,
    relation: manualChoice.relation,
    title: manualChoice.title.trim(),
    include_confirmed: manualChoice.ids.some(id => props.candidates.find(item => item.id === id)?.auditor_confirmed),
  })
  manualChoice.ids = []
  manualChoice.title = ''
}

/** What the header says about the suggestion set the page is looking at. */
const basisState = computed(() => {
  const payload = props.payload
  if (!payload) return null
  if (payload.drafts < 2) return { tone: 'idle', text: 'Consolidation needs at least two draft findings.' }
  if (payload.current) {
    return groups.value.length
      ? { tone: 'warn', text: `${plural(groups.value.length, 'suggested consolidation')} awaiting a decision. Suggestions current.` }
      : { tone: 'ok', text: decided.value.length ? 'Every suggestion has been decided. Suggestions current.' : 'The findings have been reviewed: each stands on its own.' }
  }
  return {
    tone: 'warn',
    text: payload.stale_suggestion
      ? 'Findings changed since the last consolidation review — refresh the suggestions.'
      : 'The draft findings have not been reviewed for consolidation.',
  }
})
</script>

<template>
  <section v-if="basisState && (payload?.drafts ?? 0) >= 2" class="consolidation" data-testid="consolidation-panel">
    <header class="head">
      <div class="copy">
        <h2 class="aw-label">Suggested consolidations</h2>
        <p class="state" :data-tone="basisState.tone">{{ basisState.text }}</p>
      </div>
      <Button
        :label="payload?.current ? 'Review again' : 'Refresh suggestions'"
        icon="pi pi-sparkles"
        size="small"
        outlined
        severity="secondary"
        :disabled="busy || agentBusy"
        @click="emit('refresh')"
      />
    </header>

    <article v-for="group in groups" :key="group.group_id" class="card" data-testid="consolidation-group">
      <div class="card-head">
        <span class="chip" :data-relation="group.relation">{{ RELATION_LABEL[group.relation] }}</span>
        <span class="basis">{{ group.basis === 'entity' ? 'backed by shared records' : 'shared process only' }}</span>
      </div>

      <ul class="members">
        <li v-for="member in membersOf(group)" :key="member.id" :class="{ lead: member.id === (edits[group.group_id]?.lead ?? group.lead_finding_id) }">
          <label class="member">
            <input
              type="radio"
              :name="`lead-${group.group_id}`"
              :value="member.id"
              :checked="member.id === (edits[group.group_id]?.lead ?? group.lead_finding_id)"
              @change="edits[group.group_id] = { ...(edits[group.group_id] ?? { title: group.proposed_title }), lead: member.id }"
            />
            <span class="member-id aw-figure">{{ member.id }}</span>
            <span class="member-title">{{ member.title }}</span>
            <span class="member-meta">{{ member.severity }}<template v-if="member.process"> · {{ member.process }}</template></span>
            <span v-if="member.auditor_confirmed" class="pill">confirmed</span>
          </label>
        </li>
      </ul>

      <dl class="facts">
        <template v-if="sharedEntries(group).length">
          <dt>Shared records</dt>
          <dd>
            <span v-for="entry in sharedEntries(group)" :key="entry.key" class="shared">
              <span class="key">{{ entry.key }}</span> {{ entry.ids.join(', ') }}
            </span>
          </dd>
        </template>
        <dt>Proposed title</dt>
        <dd>
          <InputText
            :modelValue="edits[group.group_id]?.title ?? group.proposed_title"
            class="title-input"
            aria-label="Proposed title"
            @update:modelValue="edits[group.group_id] = { ...(edits[group.group_id] ?? { lead: group.lead_finding_id }), title: String($event ?? '') }"
          />
        </dd>
        <template v-if="group.root_cause_hypothesis">
          <dt>Root cause hypothesis</dt>
          <dd>{{ group.root_cause_hypothesis }}</dd>
        </template>
        <dt>Why</dt>
        <dd>{{ group.rationale }}</dd>
      </dl>

      <p v-if="confirmedIn(group)" class="note">A confirmed finding is in this group. Accepting merges it and leaves the combined finding unconfirmed.</p>

      <div class="actions">
        <Button label="Accept" icon="pi pi-check" size="small" :disabled="busy" @click="accept(group)" />
        <Button label="Dismiss" size="small" text severity="secondary" :disabled="busy" @click="emit('dismiss', group)" />
      </div>
    </article>

    <article v-if="manual" class="card manual" data-testid="consolidation-manual">
      <div class="card-head">
        <span class="chip" data-relation="manual">consolidate selected</span>
        <span class="basis">Pick the findings that report one issue, then the one that leads.</span>
      </div>
      <Listbox
        v-model="manualChoice.ids"
        :options="manualOptions"
        optionLabel="label"
        optionValue="value"
        multiple
        checkmark
        filter
        filterPlaceholder="Search findings"
        class="picker"
      />
      <dl class="facts">
        <dt>Lead</dt>
        <dd>
          <label v-for="option in manualLeadOptions" :key="option.value" class="member">
            <input v-model="manualChoice.lead" type="radio" name="manual-lead" :value="option.value" />
            <span class="member-title">{{ option.label }}</span>
          </label>
          <span v-if="!manualLeadOptions.length" class="muted">Choose at least two findings.</span>
        </dd>
        <dt>Relation</dt>
        <dd>
          <label class="member"><input v-model="manualChoice.relation" type="radio" name="manual-relation" value="shared_cause" /> shared cause</label>
          <label class="member"><input v-model="manualChoice.relation" type="radio" name="manual-relation" value="same_condition" /> same condition</label>
        </dd>
        <dt>Title</dt>
        <dd><InputText v-model="manualChoice.title" class="title-input" placeholder="Keep the lead's title" aria-label="Combined title" /></dd>
      </dl>
      <div class="actions">
        <Button label="Consolidate" icon="pi pi-check" size="small" :disabled="busy || manualChoice.ids.length < 2" @click="consolidate" />
        <Button label="Cancel" size="small" text severity="secondary" @click="emit('closeManual')" />
      </div>
    </article>
  </section>
</template>

<style scoped>
.consolidation {
  display: flex; flex-direction: column; gap: .625rem;
  padding: .75rem .875rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.head { display: flex; align-items: center; justify-content: space-between; gap: .75rem; }
.copy { display: flex; flex-direction: column; gap: .2rem; min-width: 0; }
.state { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }
.state[data-tone='warn'] { color: var(--aw-warn-ink); }
.state[data-tone='ok'] { color: var(--aw-ok, var(--aw-ink-soft)); }

.card {
  display: flex; flex-direction: column; gap: .5rem;
  padding: .625rem .75rem;
  border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft);
}
.card.manual { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.card-head { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.chip {
  padding: 0 .45rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-panel); color: var(--aw-ink-strong);
  font-size: var(--aw-text-2xs); font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
}
.basis { color: var(--aw-muted); font-size: var(--aw-text-xs); }

.members { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: .25rem; }
.member { display: flex; align-items: center; gap: .45rem; min-width: 0; font-size: var(--aw-text-sm); cursor: pointer; }
.member-id { color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 700; }
.member-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--aw-ink); }
.members li.lead .member-title { font-weight: 600; }
.member-meta { color: var(--aw-muted); font-size: var(--aw-text-xs); white-space: nowrap; }
.pill { padding: 0 .375rem; border-radius: var(--aw-radius-pill); background: var(--aw-panel); color: var(--aw-warn-ink); font-size: var(--aw-text-2xs); font-weight: 700; text-transform: uppercase; }

.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: .25rem .75rem; margin: 0; font-size: var(--aw-text-sm); }
.facts dt { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; padding-top: .15rem; }
.facts dd { margin: 0; min-width: 0; color: var(--aw-ink); display: flex; flex-direction: column; gap: .25rem; }
.shared { display: block; }
.shared .key { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); margin-right: .3rem; }
.title-input { width: 100%; }
.note { margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); }
.actions { display: flex; gap: .5rem; }
.picker { width: 100%; }
.picker :deep(.p-listbox-list-container) { max-height: 14rem; }
.muted { color: var(--aw-muted); font-size: var(--aw-text-xs); }
</style>
