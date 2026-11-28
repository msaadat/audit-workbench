import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import FileBadge, { fileFormat } from './FileBadge.vue'

describe('FileBadge', () => {
  it('names each format the engagements hold by its extension', () => {
    expect(fileFormat('VINV001-202404_Invoice.pdf')).toMatchObject({ kind: 'pdf', ext: 'PDF' })
    expect(fileFormat('01_expense_reimbursement_sop.DOCX')).toMatchObject({ kind: 'word', ext: 'DOC', name: 'Word' })
    expect(fileFormat('ledger.xlsx')).toMatchObject({ kind: 'sheet', ext: 'XLS' })
    expect(fileFormat('payments.csv')).toMatchObject({ kind: 'sheet', ext: 'CSV' })
    expect(fileFormat('receipt_scan.jpeg')).toMatchObject({ kind: 'image', ext: 'IMG' })
    expect(fileFormat('notes.md')).toMatchObject({ kind: 'text', ext: 'TXT' })
    expect(fileFormat('archive.zip')).toMatchObject({ kind: 'other', ext: 'ZIP' })
    expect(fileFormat('no_extension')).toMatchObject({ kind: 'other', ext: 'FILE' })
  })

  it('says its format in words, not only in colour', () => {
    const wrapper = mount(FileBadge, { props: { file: 'policy.docx' } })
    expect(wrapper.attributes('aria-label')).toBe('Word file')
    expect(wrapper.find('.ext').text()).toBe('DOC')
  })
})
