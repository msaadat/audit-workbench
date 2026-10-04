import { describe, expect, it } from 'vitest'

import { sentenceCase } from './format'

describe('sentenceCase', () => {
  it('capitalises the first word of a stored value and reads underscores as spaces', () => {
    expect(sentenceCase('high')).toBe('High')
    expect(sentenceCase('exception_noted')).toBe('Exception noted')
    expect(sentenceCase('same condition')).toBe('Same condition')
  })

  it('keeps acronyms in capitals wherever they fall', () => {
    expect(sentenceCase('fx_contract')).toBe('FX contract')
    expect(sentenceCase('payment_instruction_ssi')).toBe('Payment instruction SSI')
  })

  it('says nothing for an empty value', () => {
    expect(sentenceCase('')).toBe('')
    expect(sentenceCase(null)).toBe('')
    expect(sentenceCase(undefined)).toBe('')
  })
})
