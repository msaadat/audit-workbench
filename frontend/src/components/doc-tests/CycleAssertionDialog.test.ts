import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import type { CycleVouchMetadata, DocTest } from '../../types'
import CycleAssertionDialog from './CycleAssertionDialog.vue'

vi.mock('primevue/dialog', () => ({
  default: {
    props: ['visible'], emits: ['update:visible'],
    template: '<section v-if="visible"><slot /><footer><slot name="footer" /></footer></section>',
  },
}))

const registry = {
  pack_id: 'payroll', pack_version: 4, definition_hash: 'sha256:payroll',
}

const metadata: CycleVouchMetadata = {
  schema_version: 2,
  registry: {
    evidence_kinds: [],
    packs: [{
      id: 'payroll', label: 'Payroll', version: 4, definition_hash: 'sha256:payroll',
      normalizer_ids: [], identifier_kind_ids: [], field_kind_ids: ['payroll.amount.net_pay'],
      record_kind_ids: ['payroll.payslip', 'payroll.bank_payment'], identifier_kinds: [],
      field_kinds: [{
        id: 'payroll.amount.net_pay', label: 'Net pay', group: 'amounts', kind: 'net_pay',
        attributes: [{ id: 'value', semantic_type: 'number' }],
      }],
      record_kinds: [
        { id: 'payroll.payslip', label: 'Payslip', primary_identifier_kinds: [], available_field_kinds: ['payroll.amount.net_pay'], bindable: true },
        { id: 'payroll.bank_payment', label: 'Bank payment', primary_identifier_kinds: [], available_field_kinds: ['payroll.amount.net_pay'], bindable: true },
      ],
    }],
  },
  cardinalities: ['one', 'many'], reuse_rules: ['exclusive', 'allowed'],
  selection_modes: ['evidence_linked', 'sample'], sampling_methods: ['random', 'interval', 'stratified'],
  assurance_scopes: ['targeted_evidence_only', 'sampled_population'],
  operators: ['equal_exact', 'equal_normalized', 'numeric_within', 'date_on_or_before', 'date_within', 'present'],
  entry_quantifiers: ['one', 'any', 'all'], role_quantifiers: ['all', 'any'],
  limits: { max_graph_hops: 6, max_cycle_records: 25, max_traversed_edges: 100, max_roles: 20, max_assertions: 50, max_items: 500, min_cycle_record_kinds: 2 },
}

const test = {
  id: 'DT-PAYROLL', kind: 'cycle_vouch', schema_version: 2, registry,
  title: 'Payroll cycle', status: 'completed', semantic_id: 'cycle:payroll',
  rcm_refs: ['RCM-PAY'], procedure_refs: [], rcm_id: 'RCM-PAY', requirement_refs: ['RCM-PAY:payment'], procedure_key: 'payment',
  spec: {}, steps: [], items: [], coverage: {}, objective: '', criteria: '',
  definition: {
    population: {
      candidate_id: 'CAND-PAY', selection_reason: 'Payroll population', table: 'payroll_register',
      row_key: { column: 'PAYMENT_ID', identifier_kind: 'payroll.payment_id' }, cycle_keys: [],
      selection: { mode: 'evidence_linked', assurance_scope: 'targeted_evidence_only' },
    },
    roles: [
      { role: 'payslip', record_kind: 'payroll.payslip', required: true, cardinality: 'one', reuse_across_items: 'exclusive' },
      { role: 'bank_payment', record_kind: 'payroll.bank_payment', required: true, cardinality: 'one', reuse_across_items: 'exclusive' },
    ],
    assertions: [],
  },
  created: '', updated: '', sha1: 'test-sha1',
} as unknown as DocTest

const global = {
  stubs: {
    Message: { template: '<div><slot /></div>' },
  },
}

afterEach(() => vi.restoreAllMocks())

describe('CycleAssertionDialog', () => {
  it('authors from the exact runtime payroll descriptor and posts the typed SHA-guarded mutation', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/DT-PAYROLL')) return test
      if (url.endsWith('/tables/payroll_register/schema')) {
        return { columns: [{ name: 'NET_PAY', dtype: 'Float64', kind: 'numeric' }] }
      }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      test_id: 'DT-PAYROLL', test_sha1: 'next-sha1', definition_sha1: 'after',
      assertion_keys: ['payroll_amount_agrees'],
      mutation: {
        changed: true, new_assertion_keys: ['payroll_amount_agrees'], changed_assertion_keys: [],
        before_definition_sha1: 'before', after_definition_sha1: 'after',
        before_test_sha1: 'test-sha1', after_test_sha1: 'next-sha1',
        retained_result_count: 0, pending_result_count: 1, stale_disposition_count: 1,
      },
    })
    const wrapper = mount(CycleAssertionDialog, {
      props: {
        modelValue: true, workspaceId: 'WS-1', testId: 'DT-PAYROLL',
        expectedTestSha1: 'test-sha1', metadata,
      },
      global,
    })
    await flushPromises()

    expect(wrapper.text()).toContain('Payroll')
    expect(wrapper.text()).toContain('payroll.payslip')
    expect(wrapper.text()).not.toContain('procure_to_pay')
    await wrapper.get('[data-testid="assertion-key"]').setValue('payroll_amount_agrees')
    await wrapper.get('[data-testid="assertion-label"]').setValue('Payslip net pay agrees to bank payment')
    await wrapper.get('[data-testid="assertion-operator"]').setValue('numeric_within')
    await wrapper.get('[data-testid="left-role"]').setValue('payslip')
    await wrapper.get('[data-testid="left-field"]').setValue('amounts|net_pay|value')
    await wrapper.get('[data-testid="right-role"]').setValue('bank_payment')
    await wrapper.get('[data-testid="right-field"]').setValue('amounts|net_pay|value')
    await wrapper.get('button[aria-label="Save assertion"]').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/workspaces/WS-1/doc-tests/DT-PAYROLL/assertions', {
      expected_test_sha1: 'test-sha1',
      assertion: {
        key: 'payroll_amount_agrees', label: 'Payslip net pay agrees to bank payment',
        left: { source: 'role', role: 'payslip', field: { group: 'amounts', kind: 'net_pay', attribute: 'value' } },
        right: { source: 'role', role: 'bank_payment', field: { group: 'amounts', kind: 'net_pay', attribute: 'value' } },
        operator: 'numeric_within', tolerance: { absolute: 0, percent: 0 },
      },
    })
  })

  it('fails closed when the full test no longer matches the grid test_sha1', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/DT-PAYROLL')) return { ...test, sha1: 'changed' }
      if (url.endsWith('/tables/payroll_register/schema')) return { columns: [] }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const post = vi.spyOn(api, 'post')
    const wrapper = mount(CycleAssertionDialog, {
      props: { modelValue: true, workspaceId: 'WS-1', testId: 'DT-PAYROLL', expectedTestSha1: 'test-sha1', metadata },
      global,
    })
    await flushPromises()

    expect(wrapper.text()).toContain('grid changed')
    expect(wrapper.get('button[aria-label="Save assertion"]').attributes('disabled')).toBeDefined()
    expect(post).not.toHaveBeenCalled()
  })

  it('changes an existing structural key in place instead of replacing the assertion set', async () => {
    const existing = {
      ...test,
      definition: {
        ...test.definition!,
        assertions: [{
          key: 'net_pay_agrees', label: 'Net pay agrees', operator: 'numeric_within' as const,
          left: { source: 'role' as const, role: 'payslip', field: { group: 'amounts', kind: 'net_pay', attribute: 'value' } },
          right: { source: 'role' as const, role: 'bank_payment', field: { group: 'amounts', kind: 'net_pay', attribute: 'value' } },
          tolerance: { absolute: 0, percent: 0 },
        }],
      },
    } as DocTest
    vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/DT-PAYROLL')) return existing
      if (url.endsWith('/tables/payroll_register/schema')) return { columns: [] }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      test_id: 'DT-PAYROLL', test_sha1: 'next', definition_sha1: 'after', assertion_keys: ['net_pay_agrees'],
      mutation: { changed: true, new_assertion_keys: [], changed_assertion_keys: ['net_pay_agrees'] },
    })
    const wrapper = mount(CycleAssertionDialog, {
      props: { modelValue: true, workspaceId: 'WS-1', testId: 'DT-PAYROLL', expectedTestSha1: 'test-sha1', metadata },
      global,
    })
    await flushPromises()

    await wrapper.get('[data-testid="author-mode"]').setValue('net_pay_agrees')
    expect(wrapper.get<HTMLInputElement>('[data-testid="assertion-key"]').element.readOnly).toBe(true)
    await wrapper.get('[data-testid="assertion-label"]').setValue('Net pay agrees after auditor edit')
    await wrapper.get('button[aria-label="Save assertion"]').trigger('click')
    await flushPromises()

    const body = post.mock.calls[0][1] as { assertion: { key: string; label: string }; placement?: unknown }
    expect(body.assertion.key).toBe('net_pay_agrees')
    expect(body.assertion.label).toBe('Net pay agrees after auditor edit')
    expect(body).not.toHaveProperty('placement')
  })
})
