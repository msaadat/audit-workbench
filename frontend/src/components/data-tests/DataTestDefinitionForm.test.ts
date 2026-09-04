import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import type { RcmRow, WorkspaceSummary } from '../../types'
import DataTestDefinitionForm from './DataTestDefinitionForm.vue'
import type { DataTestDraft } from './DataTestDefinitionForm.vue'

const ANALYTIC = {
  id: 'date_order', label: 'Date order check', icon: 'pi pi-calendar', group: 'Dates',
  description: 'Rows where one date column falls before another.',
  params: [
    { name: 'earlier', label: 'Earlier date', kind: 'column' },
    { name: 'later', label: 'Later date', kind: 'column' },
  ],
}

const workspace = {
  id: 'WS-1', name: 'Fixture', tables: [{ name: 'invoice_data' }],
} as unknown as WorkspaceSummary
const rcmRows = [{ id: 'RCM-1', risk: 'Invoices may be received early.' }] as unknown as RcmRow[]

function draft(overrides: Partial<DataTestDraft> = {}): DataTestDraft {
  return {
    title: '', objective: '', criteria: '', engine: 'analytics', rcmId: '', table: '',
    analytics: { test_id: '', params: {} }, steps: [{ label: '', instruction: '', code: '' }],
    ...overrides,
  }
}

function render(value: DataTestDraft, props: Record<string, unknown> = {}) {
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url === '/api/analytics') return [ANALYTIC] as never
    if (url.includes('/schema')) return { columns: [{ name: 'INVOICE_DATE' }, { name: 'DATE_RECEIVED' }] } as never
    return {} as never
  })
  return mount(DataTestDefinitionForm, {
    props: { modelValue: value, workspace, rcmRows, session: 'DAT-1', ...props },
    global: {
      stubs: {
        InputText: true, Textarea: true, Select: true, MultiSelect: true,
        InputNumber: true, PolarsStepEditor: true,
      },
    },
  })
}

afterEach(() => vi.restoreAllMocks())

describe('DataTestDefinitionForm', () => {
  it('asks what to run before what to call it', async () => {
    const wrapper = render(draft())
    await flushPromises()

    // Asking for a title first meant naming something that had not been chosen.
    expect(wrapper.findAll('.aw-label').map(node => node.text()))
      .toEqual(['Analytic', 'Counts as coverage for', 'Title and objective'])
  })

  it('puts the table in the parameter grid it constrains, not in a scope block', async () => {
    const wrapper = render(draft({ analytics: { test_id: 'date_order', params: {} } }))
    await flushPromises()

    // The table is the parameter every analytic takes, and the one the column
    // pickers read their options from.
    const grid = wrapper.get('.parameter-grid')
    expect(grid.findAll('label')[0].text()).toContain('Table')
  })

  it('outlines the missing values in place rather than listing them in the footer', async () => {
    const wrapper = render(draft({ analytics: { test_id: 'date_order', params: {} } }))
    await flushPromises()

    expect(wrapper.findAll('label[data-missing="true"]').length).toBeGreaterThan(0)
    expect(wrapper.emitted('valid')?.at(-1)).toEqual([false])
  })

  it('is ready once the analytic, the table, the parameters and the naming are answered', async () => {
    const wrapper = render(draft({
      title: 'Invoices received on or after invoice date',
      objective: 'Identify invoices received before their invoice date.',
      table: 'invoice_data',
      analytics: {
        test_id: 'date_order',
        params: { earlier: 'INVOICE_DATE', later: 'DATE_RECEIVED' },
      },
    }))
    await flushPromises()

    expect(wrapper.emitted('valid')?.at(-1)).toEqual([true])
  })

  it('says what an empty coverage row means, where the row is chosen', async () => {
    const wrapper = render(draft())
    await flushPromises()
    expect(wrapper.text()).toContain('Exploratory results do not count as coverage')

    await wrapper.setProps({ modelValue: draft({ rcmId: 'RCM-1' }) })
    expect(wrapper.text()).not.toContain('Exploratory results do not count as coverage')
  })

  it('switches to Polars without leaving the drawer, and names the incomplete steps', async () => {
    const wrapper = render(draft({ engine: 'polars' }))
    await flushPromises()

    expect(wrapper.get('.missing').text()).toContain('step 1 (label, instruction, code)')
    expect(wrapper.get('.link').text()).toBe('Back to the analytics library')
  })
})
