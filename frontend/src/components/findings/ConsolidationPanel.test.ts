import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PrimeVue from 'primevue/config'

import type { AuditFinding, ConsolidationGroup, ConsolidationPayload } from '../../types'
import ConsolidationPanel from './ConsolidationPanel.vue'

function group(overrides: Partial<ConsolidationGroup> = {}): ConsolidationGroup {
  return {
    group_id: 'CG-1',
    finding_ids: ['F-A', 'F-B'],
    lead_finding_id: 'F-B',
    relation: 'shared_cause',
    basis: 'entity',
    proposed_title: 'Transactions processed for an inactive vendor',
    root_cause_hypothesis: 'Vendor status is not enforced at PO or payment.',
    rationale: 'Both flag vendor V0977.',
    shared_entities: { VENDOR_ID: ['V0977'] },
    decision: null,
    members: [
      { id: 'F-A', title: 'Inactive vendor on PO', severity: 'medium', process: 'Purchase order', test_refs: ['DAT-3'] },
      { id: 'F-B', title: 'Inactive vendor paid', severity: 'high', process: 'Payment', test_refs: ['DAT-4'], auditor_confirmed: true },
    ],
    ...overrides,
  }
}

function payload(groups: ConsolidationGroup[], overrides: Partial<ConsolidationPayload> = {}): ConsolidationPayload {
  return {
    basis_sha1: 'abc', current: true, drafts: 4,
    suggestion: { basis_sha1: 'abc', groups, singletons: ['F-C', 'F-D'] },
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
    expect(card.find('.chip').text()).toBe('shared cause')
    expect(card.text()).toContain('backed by shared records')
    expect(card.text()).toContain('VENDOR_ID')
    expect(card.text()).toContain('V0977')
    expect(card.text()).toContain('Both flag vendor V0977.')
    const leads = card.findAll('input[type="radio"]')
    expect(leads.map(node => (node.element as HTMLInputElement).checked)).toEqual([false, true])
    expect(wrapper.find('.state').text()).toContain('1 suggested consolidation awaiting a decision')
  })

  it('accepts with the lead and title the auditor chose, flagging a confirmed member', async () => {
    const wrapper = mountPanel()
    const card = wrapper.find('[data-testid="consolidation-group"]')
    await card.findAll('input[type="radio"]')[0].setValue(true)
    await card.find('input[aria-label="Proposed title"]').setValue('Vendor status not enforced')
    await card.findAll('button').find(node => node.text() === 'Accept')!.trigger('click')

    const [emittedGroup, choice] = wrapper.emitted('accept')![0] as [ConsolidationGroup, { lead_finding_id: string; title: string; include_confirmed: boolean }]
    expect(emittedGroup.group_id).toBe('CG-1')
    expect(choice).toEqual({ lead_finding_id: 'F-A', title: 'Vendor status not enforced', include_confirmed: true })
    expect(card.text()).toContain('A confirmed finding is in this group')
  })

  it('dismisses without merging, and hides decided groups', async () => {
    const wrapper = mountPanel({ payload: payload([group(), group({ group_id: 'CG-2', finding_ids: ['F-C', 'F-D'], lead_finding_id: 'F-C', decision: 'dismissed', members: undefined })]) })
    expect(wrapper.findAll('[data-testid="consolidation-group"]')).toHaveLength(1)
    await wrapper.findAll('button').find(node => node.text() === 'Dismiss')!.trigger('click')
    expect((wrapper.emitted('dismiss')![0] as [ConsolidationGroup])[0].group_id).toBe('CG-1')
  })

  it('says the findings have outgrown the suggestions and offers a refresh', async () => {
    const wrapper = mountPanel({ payload: payload([], { current: false, suggestion: null, stale_suggestion: { basis_sha1: 'old', groups: 2 } }) })
    expect(wrapper.find('.state').text()).toContain('Findings changed since the last consolidation review')
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('refresh')).toHaveLength(1)
  })

  it('stays closed with fewer than two drafts', () => {
    const wrapper = mountPanel({ payload: payload([], { drafts: 1, current: false, suggestion: null }) })
    expect(wrapper.find('[data-testid="consolidation-panel"]').exists()).toBe(false)
  })
})
