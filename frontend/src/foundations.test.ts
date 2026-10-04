import { describe, expect, it } from 'vitest'

// The foundations in style.css are rules a component can quietly break — a
// filled amber button, a twelfth copy of the page header — and nothing renders
// wrong enough for a component test to notice. These read the source instead.

const files = Object.entries(
  import.meta.glob<string>(['./**/*.vue'], { query: '?raw', import: 'default', eager: true }),
).map(([path, text]) => ({ path: path.slice(2), text }))

/** Every `<Button …>` opening tag, attributes collapsed onto one line. */
function buttons() {
  return files.flatMap(({ path, text }) =>
    [...text.matchAll(/<Button\b([\s\S]*?)\/?>/g)].map(m => ({
      where: `${path}:${text.slice(0, m.index).split('\n').length}`,
      attrs: m[1].replace(/\s+/g, ' '),
    })),
  )
}

const has = (attrs: string, flag: string) => new RegExp(`(?<![\\w:-])${flag}(?![\\w-])`).test(attrs)

describe('foundations', () => {
  it('reads the source tree', () => {
    expect(files.length).toBeGreaterThan(50)
    expect(buttons().length).toBeGreaterThan(200)
  })

  // Warning, success and the like mark state; a filled button in one of them
  // reads as an alert rather than an action. The one filled button on a
  // screen is the primary (teal); danger is outlined; the rest are neutral.
  it('fills no button with a state colour', () => {
    const offenders = buttons().filter(({ attrs }) => {
      if (has(attrs, 'text') || has(attrs, 'link')) return false
      const literal = attrs.match(/(?<!:)severity="(\w+)"/)?.[1]
      const dynamic = attrs.match(/:severity="([^"]*)"/)?.[1] ?? ''
      const outlined = has(attrs, 'outlined') || /:outlined=/.test(attrs)
      if (literal && ['warn', 'warning', 'success', 'info', 'help', 'contrast'].includes(literal)) return true
      if (literal === 'danger' && !outlined) return true
      return /'(warn|success|info|help|contrast)'/.test(dynamic) || (/'danger'/.test(dynamic) && !outlined)
    })
    expect(offenders.map(b => b.where)).toEqual([])
  })

  it('draws the page header from one rule', () => {
    const copies = files
      .filter(({ path, text }) => path !== 'views/DebugView.vue' && /<style[\s\S]*^\.page-head(\s|\{)/m.test(text))
      .map(({ path }) => path)
    expect(copies).toEqual([])
  })
})
