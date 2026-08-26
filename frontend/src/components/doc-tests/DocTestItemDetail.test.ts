import { shallowMount } from '@vue/test-utils'
import Select from 'primevue/select'
import { describe, expect, it, vi } from 'vitest'

vi.mock('primevue/select', () => ({
  default: {
    name: 'Select',
    props: ['disabled'],
    template: '<div class="select-double" :data-disabled="disabled" />',
  },
}))
vi.mock('primevue/textarea', () => ({
  default: { name: 'Textarea', template: '<textarea />' },
}))
vi.mock('primevue/inputtext', () => ({
  default: { name: 'InputText', template: '<input />' },
}))
vi.mock('primevue/button', () => ({
  default: { name: 'Button', template: '<button />' },
}))

// shallowMount auto-stubs child components, so the disposition controls need
// real DOM to be clicked. These explicit stubs override the auto-stub.
const CLICKABLE_BUTTON = {
  props: ['label', 'disabled'],
  emits: ['click'],
  template: '<button :disabled="disabled || undefined" @click="$emit(\'click\')">{{ label }}</button>',
}
const BOUND_TEXTAREA = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
}

import type { DocTest, DocTestItem } from '../../types'
import DocTestItemDetail from './DocTestItemDetail.vue'

function cycle(selectionMode: 'evidence_linked' | 'sample') {
  const item = {
    id: 'ITEM-1',
    label: 'PAY-001',
    document_ids: [],
    evidence_refs: [],
    checks: [],
    attributes: [],
    evaluation: {
      state: 'passed',
      definition_sha1: 'sha1:definition',
      result_sha1: 'sha1:result',
    },
    disposition: {
      state: 'confirmed',
      evaluated_definition_sha1: 'sha1:definition',
      evaluated_result_sha1: 'sha1:result',
      stale: false,
    },
    result_by_assertion: {},
    role_bindings: [],
    missing_roles: [],
  } as unknown as DocTestItem
  const test = {
    id: 'DT-CYCLE',
    title: 'Payroll cycle',
    kind: 'cycle_vouch',
    status: 'completed',
    control_conclusion: 'no_conclusion',
    conclusion: '',
    items: [item],
    rcm_refs: ['RCM-1'],
    procedure_refs: [],
    evidence_requests: [],
    definition: {
      registry: {
        pack_id: 'payroll', version: 1, definition_hash: 'sha256:pack',
      },
      population: {
        candidate_id: 'CANDIDATE-1',
        selection_reason: 'Test selection',
        table: 'payroll',
        row_key: { column: 'PAYMENT_ID', identifier_kind: 'payroll.payment_id' },
        cycle_keys: [],
        selection: selectionMode === 'sample'
          ? { mode: 'sample', method: 'random', size: 1, seed: 7, assurance_scope: 'sampled_population' }
          : { mode: 'evidence_linked', assurance_scope: 'targeted_evidence_only' },
      },
      roles: [],
      assertions: [],
    },
    coverage: {
      population_rows: 10,
      selected_rows: 1,
      rows_with_evidence: 1,
      complete_cycles: 1,
      assurance_scope: selectionMode === 'sample' ? 'sampled_population' : 'targeted_evidence_only',
    },
  } as unknown as DocTest
  return { test, item }
}

function mount(props: { test: DocTest; item: DocTestItem }) {
  return shallowMount(DocTestItemDetail, {
    props: {
      ...props,
      documents: [],
      findings: [],
      running: false,
      busy: false,
    },
    global: {
      directives: { tooltip: () => undefined },
      stubs: {
        Button: CLICKABLE_BUTTON,
        Textarea: BOUND_TEXTAREA,
        InputText: true,
        Select: true,
        UiAdvancedSection: true,
        UiTestStatus: true,
      },
    },
  })
}

function render(selectionMode: 'evidence_linked' | 'sample') {
  return mount(cycle(selectionMode))
}

/** An item-first test whose run reached `evaluationState`. */
function itemFirst(
  evaluationState: string,
  disposition: Record<string, unknown> = {},
) {
  const item = {
    id: 'ITEM-1',
    label: 'Invoice 1001',
    instruction: 'Agree the invoice to the ledger.',
    document_ids: [],
    evidence_refs: [],
    checks: [],
    attributes: [],
    evaluation: {
      state: evaluationState,
      note: 'Deterministic local comparison completed.',
      input_sha1: 'sha1:inputs',
      ran_at: '2026-08-09T00:00:00Z',
    },
    disposition: {
      state: 'pending', note: '', actor: null, at: null,
      evaluated_input_sha1: null, stale: false,
      ...disposition,
    },
  } as unknown as DocTestItem
  const test = {
    id: 'DT-1',
    title: 'Invoice support',
    kind: 'vouching',
    status: 'completed',
    control_conclusion: 'no_conclusion',
    conclusion: '',
    items: [item],
    rcm_refs: [],
    procedure_refs: [],
    evidence_requests: [],
    spec: {},
  } as unknown as DocTest
  return { test, item }
}

function dispositionButtons(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.dispositions button').map(node => node.text())
}

describe('DocTestItemDetail Cycle-vouch assurance', () => {
  it('states the targeted scope without withholding the conclusion', () => {
    const wrapper = render('evidence_linked')

    // Narrow selection is a fact the coverage line reports, not a restriction:
    // whether it can carry a conclusion is the auditor's judgment to make.
    expect(wrapper.findComponent(Select).props('disabled')).toBe(false)
    expect(wrapper.find('.assurance-restriction').exists()).toBe(false)
    expect(wrapper.text()).toContain('Targeted evidence — not a sample')
  })

  it('enables an auditor conclusion for a current signed sample', () => {
    const wrapper = render('sample')

    expect(wrapper.findComponent(Select).props('disabled')).toBe(false)
    expect(wrapper.find('.assurance-restriction').exists()).toBe(false)
    expect(wrapper.text()).toContain('Sampled population')
  })
})

describe('DocTestItemDetail auditor disposition', () => {
  it('offers every call on an item the runner already marked an exception', () => {
    const wrapper = mount(itemFirst('failed'))

    // The regression this replaces: a runner verdict used to hide the very
    // controls that would overturn it.
    expect(dispositionButtons(wrapper)).toEqual(['Confirm', 'Exception', 'Needs review'])
    expect(wrapper.findAll('.dispositions button').every(node =>
      node.attributes('disabled') === undefined)).toBe(true)
  })

  it('offers every call on an item nothing has run yet', () => {
    const wrapper = mount(itemFirst('not_run'))

    expect(dispositionButtons(wrapper)).toEqual(['Confirm', 'Exception', 'Needs review'])
    expect(wrapper.text()).toContain('has not been run yet')
  })

  it('leads with the run while no call has been made', () => {
    const wrapper = mount(itemFirst('failed'))

    const readings = wrapper.findAll('.reading dt').map(node => node.text())
    expect(readings).toEqual(['Run result', 'Your call'])
    expect(wrapper.text()).toContain('Not recorded')
  })

  it('demotes the run to a past-tense note once the auditor has settled it', () => {
    const wrapper = mount(itemFirst('inconclusive', {
      state: 'confirmed', at: '2026-08-09T09:00:00Z', actor: 'auditor',
    }))

    // Your call leads and is the only chip; the run stays on the record as
    // prose so a settled item cannot still read as unresolved.
    expect(wrapper.findAll('.reading dt').map(node => node.text())).toEqual(['Your call'])
    expect(wrapper.find('.reading--muted').text()).toBe('The run could not settle this.')
    expect(wrapper.find('.rail-provenance').text()).toContain('auditor')
  })

  it('lets the run lead again when the call went stale', () => {
    const wrapper = mount(itemFirst('failed', { state: 'confirmed', stale: true }))

    expect(wrapper.findAll('.reading dt').map(node => node.text())).toEqual(['Run result', 'Your call'])
  })

  it('records a call that contradicts the run on the first click, then asks for a reason', async () => {
    const wrapper = mount(itemFirst('failed'))

    await wrapper.findAll('.dispositions button')[0].trigger('click')

    // Deciding and writing up are separate acts: the call lands immediately and
    // carries no note, rather than being held back until prose exists.
    expect(wrapper.emitted('setState')?.[0]).toEqual(['confirmed'])
    expect(wrapper.find('.reason-label').exists()).toBe(false)
  })

  it('prompts for a reason on a departure already on the file, without blocking', async () => {
    const wrapper = mount(itemFirst('failed', { state: 'confirmed', at: '2026-08-09T09:00:00Z' }))

    expect(wrapper.find('.rail-prompt').text()).toContain('departs from the run')

    const addReason = wrapper.findAll('button').find(node => node.text() === 'Add a reason')
    await addReason?.trigger('click')
    await wrapper.find('.reason-label textarea').setValue('Vendor reissued the invoice.')
    const save = wrapper.findAll('button').find(node => node.text() === 'Save reason')
    await save?.trigger('click')

    // The note rides along with the call it belongs to, which is unchanged.
    expect(wrapper.emitted('setState')?.[0]).toEqual([
      'confirmed', 'Vendor reissued the invoice.',
    ])
  })

  it('drops the reason prompt once one is on the file', () => {
    const wrapper = mount(itemFirst('failed', {
      state: 'confirmed', at: '2026-08-09T09:00:00Z', note: 'Vendor reissued the invoice.',
    }))

    expect(wrapper.find('.rail-prompt').exists()).toBe(false)
    expect(wrapper.find('.rail-reason').text()).toContain('Vendor reissued')
    expect(wrapper.findAll('button').some(node => node.text() === 'Edit reason')).toBe(true)
  })

  it('offers no note editor against a stale call, which has to be re-made', () => {
    const wrapper = mount(itemFirst('not_run', {
      state: 'confirmed', stale: true, at: '2026-08-09T09:00:00Z',
    }))

    expect(wrapper.findAll('button').some(node =>
      ['Add a reason', 'Edit reason'].includes(node.text()))).toBe(false)
  })

  it('records agreement with the run on the first click', async () => {
    const wrapper = mount(itemFirst('failed'))

    await wrapper.findAll('.dispositions button')[1].trigger('click')

    // No second step to hunt for: agreeing needs no justification, so the
    // click that used to only arm the control now records it.
    expect(wrapper.emitted('setState')?.[0]).toEqual(['exception'])
    expect(wrapper.find('.reason-label').exists()).toBe(false)
  })

  it('records a call on an item the run could not settle on the first click', async () => {
    const wrapper = mount(itemFirst('inconclusive'))

    await wrapper.findAll('.dispositions button')[0].trigger('click')

    expect(wrapper.emitted('setState')?.[0]).toEqual(['confirmed'])
    expect(wrapper.find('.reason-label').exists()).toBe(false)
  })

  it('warns that a sign-off went stale rather than hiding it', () => {
    const wrapper = mount(itemFirst('not_run', {
      state: 'confirmed', stale: true, note: 'Agreed.', at: '2026-08-09T09:00:00Z',
    }))

    expect(wrapper.find('.rail-stale').text()).toContain('no longer counts as current')
    // The decision itself is still on the record.
    expect(wrapper.find('.rail-reason').text()).toContain('Agreed.')
  })

  it('omits parking from a cycle item, whose disposition stays binary', () => {
    const wrapper = render('sample')

    expect(dispositionButtons(wrapper)).toEqual(['Confirm', 'Exception'])
  })
})
