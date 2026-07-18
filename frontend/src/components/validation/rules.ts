import type { CheckMeta, ValidationRule } from '../../types'

/** Client-side id for a draft rule; the backend keeps it on save. */
export function newRuleId(): string {
  return Math.random().toString(36).slice(2, 12)
}

function bound(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null
  return Number(value).toLocaleString()
}

/** Short chip label for a rule — mirrors the backend's rule_label(). */
export function ruleLabel(rule: ValidationRule, checks: CheckMeta[]): string {
  const meta = checks.find((c) => c.id === rule.check)
  const label = meta?.label ?? rule.check
  const p = rule.params ?? {}
  switch (rule.check) {
    case 'numeric_sign':
      return `Sign: ${String(p.mode ?? 'positive').replace('_', '-')}`
    case 'range':
    case 'length':
    case 'row_count': {
      const parts: string[] = []
      const low = bound(p.min)
      const high = bound(p.max)
      if (low) parts.push(`≥ ${low}`)
      if (high) parts.push(`≤ ${high}`)
      return parts.length ? `${label} ${parts.join(' and ')}` : label
    }
    case 'allowed_values': {
      const count = Array.isArray(p.values) ? p.values.length : 0
      return `In list (${count} value${count === 1 ? '' : 's'})`
    }
    case 'pattern':
      return `Pattern /${String(p.regex ?? '')}/`
    case 'date_range': {
      const parts: string[] = []
      if (p.min) parts.push(`≥ ${p.min}`)
      if (p.max) parts.push(`≤ ${p.max}`)
      if (p.not_in_future) parts.push('not in future')
      return parts.length ? `${label}: ${parts.join(', ')}` : label
    }
    case 'unique_key':
      return `Key: ${((p.columns as string[]) ?? []).join(' + ')}`
    case 'referential':
      return `In ${p.lookup_table ?? '?'}.${p.lookup_column ?? '?'}`
    case 'compare_fields': {
      const symbol =
        { ge: '≥', gt: '>', eq: '=', ne: '≠', le: '≤', lt: '<' }[String(p.op ?? 'ge')] ?? '≥'
      return `${symbol} ${p.other ?? '?'}`
    }
    case 'conditional_required':
      return `Required when ${p.when_column ?? '?'} ${
        { ge: '≥', gt: '>', eq: '=', ne: '≠', le: '≤', lt: '<' }[String(p.when_op ?? 'eq')] ?? '='
      } ${p.when_value ?? '?'}`
    case 'expression':
      return String(p.name ?? '').trim() || label
    default:
      return label
  }
}
