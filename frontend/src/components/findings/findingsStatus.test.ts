import { describe, expect, it } from 'vitest'

import type { AuditFinding } from '../../types'
import { filterFindings, findingsStatus } from './findingsStatus'

function finding(id: string, overrides: Partial<AuditFinding> = {}): AuditFinding {
  return {
    id, semantic_id: id, created_by: 'agent', agent_run_id: null,
    title: 'Finding', severity: 'high', narrative: 'Narrative',
    management_response: 'Management accepts the point.',
    rcm_refs: ['RCM-1'], test_refs: ['DAT-1'], procedure_refs: [], execution_refs: [],
    evidence_refs: [{ id: 'EV-1' }], evidence_warnings: [],
    cause_pending: false, auditor_confirmed: true, source: 'agent',
    source_observation_id: null, created: '', updated: '',
    ...overrides,
  } as AuditFinding
}

const lanes = (items: AuditFinding[]) =>
  Object.fromEntries(findingsStatus(items).lanes.map(lane => [lane.key, lane]))

describe('findings status', () => {
  it('says nothing is owed on an empty file, and offers to draft', () => {
    const model = findingsStatus([])
    const byKey = Object.fromEntries(model.lanes.map(lane => [lane.key, lane]))

    expect(model.lanes.map(lane => lane.state)).toEqual(['idle', 'idle', 'idle'])
    expect(byKey.confirmed.actions).toEqual([{
      key: 'generate_findings', label: 'Generate all findings', tone: 'primary', needsAgent: true,
    }])
    expect(model.disclosures).toEqual([])
  })

  it('offers the confirm on exactly the findings that are missing it', () => {
    const lane = lanes([finding('F-1'), finding('F-2', { auditor_confirmed: false })]).confirmed

    expect(`${lane.value} ${lane.caption}`).toBe('1 of 2 findings confirmed for reporting')
    expect(lane.state).toBe('gap')
    expect(lane.actions).toEqual([{
      key: 'confirm_all', label: 'Confirm 1', tone: 'primary', ids: ['F-2'],
    }])
  })

  it('counts a finding as unsupported when any one of its links is missing', () => {
    const lane = lanes([
      finding('F-1'),
      finding('F-2', { evidence_refs: [] }),
      finding('F-3', { rcm_refs: [] }),
      finding('F-4', { test_refs: [] }),
      finding('F-5', { evidence_warnings: ['Source hash moved.'] }),
    ]).support

    expect(lane.state).toBe('alarm')
    expect(`${lane.value} ${lane.caption}`).toBe('1 of 5 traced to evidence, a risk and a test')
    expect(lane.chips.map(chip => chip.label)).toEqual([
      '1 without evidence', '1 not linked to a risk',
      '1 not linked to a test', '1 with stale evidence',
    ])
    // Linking is per finding and has no batch behind it.
    expect(lane.actions).toEqual([])
  })

  it('treats a confirmed, fully supported finding as unfinished while follow-up is open', () => {
    // The live shape of the procurement file: everything agreed and evidenced,
    // every root cause still open. Two lanes rest and the third does not.
    const model = lanes([
      finding('F-1', { cause_pending: true, management_response: '' }),
      finding('F-2', { cause_pending: true, management_response: '   ' }),
    ])

    expect(model.confirmed.state).toBe('done')
    expect(model.support.state).toBe('done')
    expect(model.follow_up.state).toBe('alarm')
    expect(`${model.follow_up.value} ${model.follow_up.caption}`)
      .toBe('0 of 2 have a settled cause and a response')
    expect(model.follow_up.chips.map(chip => chip.label))
      .toEqual(['2 root cause pending', '2 awaiting management response'])
    // Neither a cause nor management's own words can be generated.
    expect(model.follow_up.actions).toEqual([])
  })

  it('rests the follow-up lane only when both halves are in', () => {
    expect(lanes([finding('F-1')]).follow_up.state).toBe('done')
    expect(lanes([finding('F-1', { cause_pending: true })]).follow_up.state).toBe('alarm')
    expect(lanes([finding('F-1', { management_response: '' })]).follow_up.state).toBe('alarm')
  })

  it('discloses who wrote the findings and whose evidence has drifted', () => {
    const model = findingsStatus([
      finding('F-1'),
      finding('F-2', { source: 'manual', evidence_warnings: ['Source hash moved.'] }),
    ])

    expect(model.disclosures.map(item => item.key)).toEqual(['agent', 'evidence'])
    expect(model.disclosures[0].message).toContain('1 of 2 findings was drafted by the assistant')
  })
})

describe('findings filters', () => {
  const items = [
    finding('F-1'),
    finding('F-2', { auditor_confirmed: false, cause_pending: true }),
    finding('F-3', { evidence_refs: [], management_response: '', source: 'manual' }),
  ]
  const ids = (filter: Parameters<typeof filterFindings>[1]) =>
    filterFindings(items, filter).map(item => item.id)

  it('returns every finding when nothing is filtered', () => {
    expect(ids(null)).toEqual(['F-1', 'F-2', 'F-3'])
  })

  it('selects the findings each count stands for', () => {
    expect(ids('unconfirmed')).toEqual(['F-2'])
    expect(ids('confirmed')).toEqual(['F-1', 'F-3'])
    expect(ids('cause_pending')).toEqual(['F-2'])
    expect(ids('no_evidence')).toEqual(['F-3'])
    expect(ids('no_response')).toEqual(['F-3'])
    expect(ids('agent_authored')).toEqual(['F-1', 'F-2'])
  })

  it('treats a whitespace-only management response as no response', () => {
    expect(filterFindings([finding('F-9', { management_response: '  \n ' })], 'no_response'))
      .toHaveLength(1)
  })
})
