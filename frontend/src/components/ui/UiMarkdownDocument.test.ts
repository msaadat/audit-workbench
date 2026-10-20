import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import UiMarkdownDocument from './UiMarkdownDocument.vue'

/**
 * Excerpts of the treasury engagement's draft report and the procurement
 * memorandum, in the shapes `report.py` and the APM worker write them.
 */
const REPORT = [
  '# Preliminary Internal Audit Working Draft',
  '> **Preliminary working draft:** fieldwork, evidence, review, or auditor judgment remains open. This document is not a final audit opinion.',
  '## A. Executive Summary',
  '### 1. Introduction',
  'This report presents the results of the internal audit of Treasury.',
  '### 3. Audit Conclusion',
  'No overall rating is assigned because fieldwork remains open.',
  '## B. Detailed Findings',
  'No management responses have been received for the findings below.',
  '### 1. Dealers confirmed trades they executed',
  '**Severity:** High · **Reference:** [Finding F-3814A0](/workspace/ws_1/findings?finding=F-3814A0)',
  '#### Condition',
  'Of the 41 deals recorded for the period, three were flagged.',
  '#### Root Cause',
  'Not established by the evidence obtained; pending auditor follow-up.',
  '#### Recommendation',
  'Configure the confirmation workflow to reject a self-confirmation.',
  '**Supporting procedures:** the following findings were consolidated into this one.',
  '- Dealer confirmed transactions they executed (tests DAT-2B1DE2273D)',
].join('\n\n')

describe('UiMarkdownDocument', () => {
  it('sets the title on the cover, with the page\'s document control under it', () => {
    const wrapper = mount(UiMarkdownDocument, {
      props: {
        markdown: REPORT,
        cover: { eyebrow: 'Internal audit report', fields: [{ label: 'Entity', value: 'Meridian Bank Limited' }] },
        runningHead: ['Meridian Bank Limited · Treasury', 'Internal audit report'],
        endMark: 'End of report',
      },
    })

    expect(wrapper.find('.doc-cover h1#preliminary-internal-audit-working-draft').text())
      .toBe('Preliminary Internal Audit Working Draft')
    expect(wrapper.find('.doc-fields').text()).toContain('Meridian Bank Limited')
    expect(wrapper.find('.doc-runhead').text()).toContain('Internal audit report')
    // The notice the report writes under its title is a band, not a quote.
    expect(wrapper.find('.doc-band').text()).toContain('Preliminary working draft')
    expect(wrapper.find('.doc-end').text()).toBe('End of report')
  })

  it('divides the report into its lettered parts and keeps its own numbers', () => {
    const wrapper = mount(UiMarkdownDocument, { props: { markdown: REPORT } })

    expect(wrapper.findAll('.doc-part').map(node => node.text())).toEqual([
      'Part AExecutive Summary', 'Part BDetailed Findings',
    ])
    const conclusion = wrapper.find('[id="3-audit-conclusion"]')
    expect(conclusion.find('.doc-n').text()).toBe('3')
    expect(conclusion.find('.doc-t').text()).toBe('Audit Conclusion')
  })

  it('sets an assembled finding as a finding: severity, reference, labelled sections', async () => {
    const wrapper = mount(UiMarkdownDocument, { props: { markdown: REPORT } })
    const finding = wrapper.find('.doc-finding')

    expect(finding.find('.doc-finding-no').text()).toBe('Finding 1')
    expect(finding.find('.aw-severity').attributes('data-sev')).toBe('high')
    expect(finding.find('.doc-finding-title').text()).toBe('Dealers confirmed trades they executed')
    expect(finding.findAll('.doc-finding-grid > dt').map(node => node.text()))
      .toEqual(['Condition', 'Root Cause', 'Recommendation'])
    // The empty section is drawn as the gap it is.
    expect(finding.find('.doc-gap').text()).toContain('pending auditor follow-up')
    // What the assembler appended moves out of the narrative into its own block.
    expect(finding.find('.doc-finding-grid').text()).not.toContain('Supporting procedures')
    expect(finding.find('.doc-consolidated').text()).toContain('Dealer confirmed transactions they executed')

    await finding.find('.doc-finding-ref').trigger('click')
    expect(wrapper.emitted('open-finding')).toEqual([['F-3814A0']])
  })

  it('leaves a report finding without its severity line as an ordinary section', () => {
    const wrapper = mount(UiMarkdownDocument, {
      props: { markdown: '## B. Detailed Findings\n\n### 1. Hand-written finding\n\nWritten by an auditor.' },
    })

    expect(wrapper.find('.doc-finding').exists()).toBe(false)
    expect(wrapper.find('[id="1-hand-written-finding"]').text()).toContain('Hand-written finding')
  })

  it('draws the memorandum\'s process as steps when it names the cycle', () => {
    const markdown = [
      '# Audit Planning Memorandum',
      '## Engagement',
      '- Entity: Global Bank\n- Period: January 2024 to January 2025',
      '## Process flow and understanding',
      '1. **Requisition initiation and approval** — Raised in the ERP.\n2. **Purchase Order** — Raised on approval.',
      '## Key risks and planned response',
      '### Authorisation against stated limits',
      'Considered.',
    ].join('\n\n')
    const wrapper = mount(UiMarkdownDocument, {
      props: { markdown, document: { stepNames: ['Requisition initiation and approval', 'Purchase Order'] } },
    })

    expect(wrapper.find('.doc-kv').text()).toContain('Global Bank')
    expect(wrapper.findAll('.doc-steps .doc-lead').map(node => node.text()))
      .toEqual(['Requisition initiation and approval', 'Purchase Order'])
    // A memorandum numbers nothing itself, so every section is numbered for it.
    expect(wrapper.find('#authorisation-against-stated-limits .doc-n').text()).toBe('3.1')
  })
})
