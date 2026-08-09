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

function render(selectionMode: 'evidence_linked' | 'sample') {
  const props = cycle(selectionMode)
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
        Button: true,
        InputText: true,
        Select: true,
        Textarea: true,
        UiAdvancedSection: true,
        UiTestStatus: true,
      },
    },
  })
}

describe('DocTestItemDetail Cycle-vouch assurance', () => {
  it('keeps targeted evidence item-specific and disables a population conclusion', () => {
    const wrapper = render('evidence_linked')

    expect(wrapper.findComponent(Select).props('disabled')).toBe(true)
    expect(wrapper.get('.assurance-restriction').text()).toContain(
      'cannot support a population control conclusion or projected exception rate',
    )
    expect(wrapper.text()).toContain('Targeted evidence — not a sample')
  })

  it('enables an auditor conclusion only for a current signed sample', () => {
    const wrapper = render('sample')

    expect(wrapper.findComponent(Select).props('disabled')).toBe(false)
    expect(wrapper.find('.assurance-restriction').exists()).toBe(false)
    expect(wrapper.text()).toContain('Sampled population')
  })
})
