import { describe, expect, it } from 'vitest'

import iconsCss from './icons.css?raw'

// The icon set is generated from what src/ names (scripts/build-icons.mjs), so
// the one way to ship a blank icon is to name one and not regenerate. These
// read the tree rather than render it: a missing class draws nothing, which no
// component test would notice.

const MODIFIERS = new Set(['spin'])

const files = Object.entries(
  import.meta.glob<string>(['./**/*.vue', './**/*.ts', '!./**/*.test.ts'], { query: '?raw', import: 'default', eager: true }),
).map(([path, text]) => ({ path: path.slice(2), text }))

const generated = new Set([...iconsCss.matchAll(/^\.aw-icon-([a-z0-9-]+) \{/gm)].map(m => m[1]))

describe('icons', () => {
  it('reads the source tree', () => {
    expect(files.length).toBeGreaterThan(50)
    expect(generated.size).toBeGreaterThan(50)
  })

  it('generates every icon the source names', () => {
    const missing = files.flatMap(({ path, text }) =>
      [...text.matchAll(/(?<![\w-])aw-icon-([a-z0-9]+(?:-[a-z0-9]+)*)/g)]
        .map(m => m[1])
        .filter(name => !MODIFIERS.has(name) && !generated.has(name))
        .map(name => `${path}: aw-icon-${name}`),
    )
    expect(missing, 'run `npm run icons`').toEqual([])
  })

  it('names no PrimeIcons class', () => {
    const glyph = /(?<![\w-])pi-[a-z][a-z0-9-]*/g
    const base = /class="(?:[^"]*\s)?pi(?=[\s"])/g
    const leftovers = files.flatMap(({ path, text }) =>
      [...text.matchAll(glyph), ...text.matchAll(base)].map(m => `${path}: ${m[0]}`),
    )
    expect(leftovers).toEqual([])
  })
})
