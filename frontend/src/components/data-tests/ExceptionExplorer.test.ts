import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type {
  DataTestExceptionDisposition,
  DataTestExceptionProfile,
  FramePayload,
} from '../../types'
import ExceptionExplorer from './ExceptionExplorer.vue'

const FRAME: FramePayload = {
  columns: ['invoice_no', 'amount', '_reason'],
  dtypes: ['Int64', 'Float64', 'String'],
  rows: [
    [1001, 900, 'amount over 500'],
    [1002, 12, 'rounding under 1.00'],
  ],
}

const PROFILE: DataTestExceptionProfile = {
  entity_key: 'invoice_no',
  record_count: 2,
  row_count: 2,
  population: 10,
  population_table: 'transactions',
  reason_source: 'predicate',
  reasons: [
    { label: 'amount over 500', rows: 1, records: 1, columns: ['amount'] },
    { label: 'rounding under 1.00', rows: 1, records: 1, columns: ['amount'] },
  ],
}

function ruling(overrides: Partial<DataTestExceptionDisposition> = {}): DataTestExceptionDisposition {
  return {
    scope: 'reason',
    key: 'amount over 500',
    state: 'accepted',
    note: 'Pre-approved emergency purchases.',
    rows: 1,
    records: 1,
    actor: 'auditor',
    source: 'auditor',
    at: '2026-08-15T10:00:00+00:00',
    evaluated_input_sha1: 'sha1:input',
    stale: false,
    ...overrides,
  }
}

function mountExplorer(dispositions: DataTestExceptionDisposition[] = []) {
  return mount(ExceptionExplorer, {
    props: { profile: PROFILE, frame: FRAME, dispositions },
    global: { stubs: { FrameTable: true } },
  })
}

describe('ExceptionExplorer rulings', () => {
  it('offers a ruling on every exception group', () => {
    const wrapper = mountExplorer()

    expect(wrapper.findAll('.reason-row')).toHaveLength(2)
    expect(wrapper.text()).toContain('2 still open')
    expect(wrapper.findAll('.verdict').every(node => node.text() === 'Not ruled on')).toBe(true)
  })

  it('confirming an exception needs no reason and emits immediately', async () => {
    const wrapper = mountExplorer()

    const buttons = wrapper.findAll('.reason-row')[0].findAll('button')
    await buttons.find(node => node.text() === 'Confirm exception')!.trigger('click')

    expect(wrapper.emitted('rule')?.[0]).toEqual([
      { key: 'amount over 500', state: 'exception', note: '' },
    ])
  })

  it('accepting a group withholds the emit until a reason is written', async () => {
    const wrapper = mountExplorer()
    const row = wrapper.findAll('.reason-row')[0]

    await row.findAll('button').find(node => node.text() === 'Accept')!.trigger('click')
    // The form is open but empty: retiring exceptions is the ruling that moves
    // the control conclusion, so it cannot be a bare click.
    expect(row.find('form.accept').exists()).toBe(true)
    expect(wrapper.emitted('rule')).toBeUndefined()

    await row.find('form.accept textarea').setValue('Rounding is immaterial.')
    await row.find('form.accept').trigger('submit')

    expect(wrapper.emitted('rule')?.[0]).toEqual([
      { key: 'amount over 500', state: 'accepted', note: 'Rounding is immaterial.' },
    ])
  })

  it('counts an accepted group as settled and a stale one as open again', async () => {
    const settled = mountExplorer([ruling()])
    expect(settled.text()).toContain('1 still open')
    expect(settled.findAll('.verdict')[0].text()).toBe('Accepted')

    const stale = mountExplorer([ruling({ stale: true })])
    // A ruling made against evidence that has since moved stands on the record
    // but stops counting, so the group is open again.
    expect(stale.text()).toContain('2 still open')
    expect(stale.findAll('.verdict')[0].text()).toBe('Not ruled on')
    expect(stale.text()).toContain('evidence that has since changed')
  })

  it('says when a ruling came from an unattended run rather than a person', () => {
    const wrapper = mountExplorer([ruling({ state: 'exception', source: 'agent', actor: 'agent' })])

    expect(wrapper.text()).toContain('Recorded by an unattended run')
  })
})
