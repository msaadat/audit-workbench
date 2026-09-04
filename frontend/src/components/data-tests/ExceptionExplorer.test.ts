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

/** A FrameTable that keeps the props under test, so the Ruling column shows. */
const FrameTableStub = {
  props: ['frame', 'visibleColumns', 'columnLabels', 'hiddenColumns', 'expandable', 'scrollHeight'],
  template: '<div class="frame"'
    + ' :data-columns="visibleColumns.join(\',\')"'
    + ' :data-rows="JSON.stringify(frame.rows)" />',
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
    global: { stubs: { FrameTable: FrameTableStub } },
  })
}

function rulings(wrapper: ReturnType<typeof mountExplorer>, index: number) {
  return wrapper.findAll('.reason-card')[index].findAll('.rulings button')
}

describe('ExceptionExplorer rulings', () => {
  it('offers one ruling control per exception group', () => {
    const wrapper = mountExplorer()

    expect(wrapper.findAll('.reason-card')).toHaveLength(2)
    expect(wrapper.find('.tally').text()).toContain('2 rows still open')
    expect(wrapper.findAll('.ruling-line').every(node => node.text().startsWith('Not ruled on'))).toBe(true)
    // Three positions of one control, not three separate offers.
    expect(rulings(wrapper, 0).map(node => node.text()))
      .toEqual(['Accept', 'Confirm exception', 'Needs review'])
  })

  it('confirming an exception needs no reason and emits immediately', async () => {
    const wrapper = mountExplorer()

    await rulings(wrapper, 0)[1].trigger('click')

    expect(wrapper.emitted('rule')?.[0]).toEqual([
      { key: 'amount over 500', state: 'exception', note: '' },
    ])
  })

  it('accepting a group withholds the emit until a reason is written', async () => {
    const wrapper = mountExplorer()

    await rulings(wrapper, 0)[0].trigger('click')
    // The form is open but empty: retiring exceptions is the ruling that moves
    // the control conclusion, so it cannot be a bare click.
    const card = wrapper.findAll('.reason-card')[0]
    expect(card.find('form.accept').exists()).toBe(true)
    expect(wrapper.emitted('rule')).toBeUndefined()

    await card.find('form.accept textarea').setValue('Rounding is immaterial.')
    await card.find('form.accept').trigger('submit')

    expect(wrapper.emitted('rule')?.[0]).toEqual([
      { key: 'amount over 500', state: 'accepted', note: 'Rounding is immaterial.' },
    ])
  })

  it('marks the ruling in force and offers Clear only once there is one', async () => {
    const wrapper = mountExplorer([ruling()])
    const pressed = rulings(wrapper, 0).filter(node => node.attributes('aria-pressed') === 'true')

    expect(pressed.map(node => node.text())).toEqual(['Accept'])
    expect(wrapper.findAll('.reason-card')[1].find('.link').exists()).toBe(false)

    await wrapper.findAll('.reason-card')[0].find('.link').trigger('click')
    expect(wrapper.emitted('rule')?.[0]).toEqual([
      { key: 'amount over 500', state: 'pending', note: 'Pre-approved emergency purchases.' },
    ])
  })

  it('counts an accepted group as settled and a stale one as open again', () => {
    const settled = mountExplorer([ruling()])
    expect(settled.find('.tally').text()).toContain('1 row still open')
    expect(settled.findAll('.ruling-line')[0].text()).toContain('Accepted')

    const stale = mountExplorer([ruling({ stale: true })])
    // A ruling made against evidence that has since moved stands on the record
    // but stops counting, so the group is open again.
    expect(stale.find('.tally').text()).toContain('2 rows still open')
    expect(stale.findAll('.ruling-line')[0].text()).toContain('Not ruled on')
    expect(stale.text()).toContain('evidence that has since changed')
  })

  it('says when a ruling came from an unattended run rather than a person', () => {
    const wrapper = mountExplorer([ruling({ state: 'exception', source: 'agent', actor: 'agent' })])

    expect(wrapper.find('.by-agent').text()).toContain('unattended run')
  })
})

describe('ExceptionExplorer table', () => {
  it('carries each row’s ruling as its last column', () => {
    const wrapper = mountExplorer([ruling()])
    const frame = wrapper.get('.frame')

    // The rulings are per reason group; without this column a reader had to go
    // back up to the groups to work out which rows one had covered.
    expect(frame.attributes('data-columns')?.split(',').at(-1)).toBe('_ruling')
    expect(JSON.parse(frame.attributes('data-rows') ?? '[]')).toEqual([
      [1001, 900, 'amount over 500', 'Accepted'],
      [1002, 12, 'rounding under 1.00', 'Open'],
    ])
  })

  it('narrows the table to the reason that was picked', async () => {
    const wrapper = mountExplorer()

    await wrapper.findAll('.reason-name')[1].trigger('click')

    expect(JSON.parse(wrapper.get('.frame').attributes('data-rows') ?? '[]'))
      .toEqual([[1002, 12, 'rounding under 1.00', 'Open']])
    expect(wrapper.find('.filtered').text()).toContain('rounding under 1.00')
  })
})
