import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PrimeVue from 'primevue/config'

import type { AuditFinding, ConsolidationGroup, ConsolidationPayload } from '../../types'
import ConsolidationPanel from './ConsolidationPanel.vue'

function group(overrides: Partial<ConsolidationGroup> = {}): ConsolidationGroup {
  return {
    group_id: 'CG-1',
    observation_ids: ['OBS-A', 'OBS-B'],
    lead_observation_id: 'OBS-B',
    relation: 'shared_cause',
    basis: 'entity',
    proposed_title: 'Transactions processed for an inactive vendor',
    root_cause_hypothesis: 'Vendor status is not enforced at PO or payment.',
    rationale: 'Both flag vendor V0977.',
    shared_entities: { VENDOR_ID: ['V0977'] },
    decision: null,
    members: [
      { id: 'OBS-A', title: 'Inactive vendor on PO', process: 'Purchase order', test_refs: ['DAT-3'], exception_count: 1 },
      { id: 'OBS-B', title: 'Inactive vendor paid', process: 'Payment', test_refs: ['DAT-4'], exception_count: 2 },
    ],
    ...overrides,
  }
}

function payload(groups: ConsolidationGroup[], overrides: Partial<ConsolidationPayload> = {}): ConsolidationPayload {
  return {
    basis_sha1: 'abc', current: true, candidates: 4, groupable: 4, drafts: 0,
    suggestion: { basis_sha1: 'abc', groups, singletons: ['OBS-C', 'OBS-D'] },
    stale_suggestion: null,
    undecided: groups.filter(item => !item.decision).length,
    ...overrides,
  }
}

function mountPanel(props: Partial<InstanceType<typeof ConsolidationPanel>['$props']> = {}) {
  return mount(ConsolidationPanel, {
    props: {
      payload: payload([group()]), candidates: [] as AuditFinding[], busy: false, agentBusy: false, manual: false,
      ...props,
    },
    global: { plugins: [PrimeVue] },
  })
}

describe('ConsolidationPanel', () => {
  it('shows one card per undecided group with the shared records, the relation, and the model lead', () => {
    const wrapper = mountPanel()
    const card = wrapper.find('[data-testid="consolidation-group"]')
    expect(card.find('.chip').text()).toBe('Shared cause')
    expect(card.text()).toContain('backed by shared records')
    expect(card.text()).toContain('VENDOR_ID')
    expect(card.text()).toContain('V0977')
    expect(card.text()).toContain('Both flag vendor V0977.')
    expect(card.text()).toContain('2 exceptions')
    const leads = card.findAll('input[type="radio"]')
    expect(leads.map(node => (node.element as HTMLInputElement).checked)).toEqual([false, true])
    expect(wrapper.find('.state').text()).toContain('1 suggested consolidation awaiting a decision')
    expect(wrapper.find('.state').text()).toContain('drafted separately')
  })

  it('accepts with the lead and title the auditor chose', async () => {
    const wrapper = mountPanel()
    const card = wrapper.find('[data-testid="consolidation-group"]')
    await card.findAll('input[type="radio"]')[0].setValue(true)
    await card.find('input[aria-label="Proposed title"]').setValue('Vendor status not enforced')
    await card.findAll('button').find(node => node.text() === 'Accept')!.trigger('click')

    const [emittedGroup, choice] = wrapper.emitted('accept')![0] as [ConsolidationGroup, { lead_observation_id: string; title: string; include_confirmed: boolean }]
    expect(emittedGroup.group_id).toBe('CG-1')
    expect(choice).toEqual({ lead_observation_id: 'OBS-A', title: 'Vendor status not enforced', include_confirmed: false })
    expect(card.find('.note').exists()).toBe(false)
  })

  it('says when accepting merges drafts that already exist, and flags a confirmed one', () => {
    const drafted = group({
      members: [
        { id: 'OBS-A', title: 'Inactive vendor on PO', process: 'Purchase order', test_refs: ['DAT-3'], finding_id: 'F-A', severity: 'medium' },
        { id: 'OBS-B', title: 'Inactive vendor paid', process: 'Payment', test_refs: ['DAT-4'], finding_id: 'F-B', severity: 'high', auditor_confirmed: true },
      ],
    })
    const wrapper = mountPanel({ payload: payload([drafted]) })
    const card = wrapper.find('[data-testid="consolidation-group"]')
    expect(card.text()).toContain('drafted as F-A')
    expect(card.text()).toContain('A confirmed finding is in this group')

    const unconfirmed = mountPanel({ payload: payload([group({ members: drafted.members!.map(member => ({ ...member, auditor_confirmed: false })) })]) })
    expect(unconfirmed.find('.note').text()).toContain('2 members are already drafted')
  })

  it('dismisses without merging, and hides decided groups', async () => {
    const wrapper = mountPanel({ payload: payload([group(), group({ group_id: 'CG-2', observation_ids: ['OBS-C', 'OBS-D'], lead_observation_id: 'OBS-C', decision: 'dismissed', members: undefined })]) })
    expect(wrapper.findAll('[data-testid="consolidation-group"]')).toHaveLength(1)
    await wrapper.findAll('button').find(node => node.text() === 'Dismiss')!.trigger('click')
    expect((wrapper.emitted('dismiss')![0] as [ConsolidationGroup])[0].group_id).toBe('CG-1')
  })

  it('says the exceptions have outgrown the suggestions and offers a refresh', async () => {
    const wrapper = mountPanel({ payload: payload([], { current: false, suggestion: null, stale_suggestion: { basis_sha1: 'old', groups: 2 } }) })
    expect(wrapper.find('.state').text()).toContain('Exceptions changed since the last consolidation review')
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('has nothing to ask once every exception is drafted or grouped', () => {
    const wrapper = mountPanel({ payload: payload([], { current: false, suggestion: null, groupable: 0, drafts: 4 }) })
    expect(wrapper.find('.state').text()).toContain('nothing is left to review')
  })

  it('stays closed with fewer than two exceptions', () => {
    const wrapper = mountPanel({ payload: payload([], { candidates: 1, groupable: 1, current: false, suggestion: null }) })
    expect(wrapper.find('[data-testid="consolidation-panel"]').exists()).toBe(false)
  })
})
