import type { DataTestStep } from '../../types'

export function emptyPolarsStep(): DataTestStep {
  return { label: '', instruction: '', code: 'result = df.head(100)' }
}

/** A step is executable only once it is named, explained, and has code. */
export function polarsStepsValid(steps: DataTestStep[]): boolean {
  return steps.length > 0 && steps.every(step =>
    step.label.trim() && step.instruction.trim() && step.code.trim())
}

export function missingStepFields(steps: DataTestStep[]): string[] {
  const missing: string[] = []
  steps.forEach((step, index) => {
    const gaps = [
      !step.label.trim() && 'label',
      !step.instruction.trim() && 'instruction',
      !step.code.trim() && 'code',
    ].filter(Boolean) as string[]
    if (gaps.length) missing.push(`step ${index + 1} (${gaps.join(', ')})`)
  })
  return missing
}
