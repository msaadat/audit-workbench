import { describe, expect, it } from 'vitest'

import {
  BENCH_SECTIONS,
  FILE_SECTIONS,
  destinationForSection,
  routeForTarget,
  surfacePath,
  workspaceRoute,
} from './useWorkspaceNavigation'

/**
 * Navigation is destination-keyed, so a destination moving between surfaces is
 * a one-line change here and a broken link everywhere else. These pin the two
 * paths that carry the most inbound links — the workspace root and the chat —
 * and the invariant that every rail section resolves back to a destination.
 */

describe('surfacePath', () => {
  it('puts the engagement record at the workspace root', () => {
    expect(surfacePath('procurement', 'home')).toBe('/workspace/procurement')
  })

  it('gives the assistant its own path rather than the root', () => {
    // The root used to be the chat. Every bookmark and every "open the
    // workspace" link lands on the record now.
    expect(surfacePath('procurement', 'console')).toBe('/workspace/procurement/console')
  })

  it('names a work product directly under the workspace', () => {
    // The audit file is a host, not a surface, so it contributes no segment:
    // a reader sees the work product they asked for and nothing else.
    expect(surfacePath('procurement', 'file', 'coverage')).toBe('/workspace/procurement/coverage')
    expect(surfacePath('procurement', 'file', 'apm')).toBe('/workspace/procurement/apm')
  })

  it('keeps bench sections under the bench, which is still a surface', () => {
    expect(surfacePath('procurement', 'bench', 'documents')).toBe('/workspace/procurement/bench/documents')
    expect(surfacePath('procurement', 'bench')).toBe('/workspace/procurement/bench')
  })
})

describe('workspaceRoute', () => {
  it('routes the record destination to the root', () => {
    expect(workspaceRoute('procurement', 'record')).toEqual({
      path: '/workspace/procurement', query: {},
    })
  })

  it('routes the console destination to its own path', () => {
    expect(workspaceRoute('procurement', 'console')).toEqual({
      path: '/workspace/procurement/console', query: {},
    })
  })

  it('carries only the query keys the destination owns', () => {
    expect(workspaceRoute('procurement', 'rcm', { rcm: 'RCM-F08A71', test: 'DT-1' })).toEqual({
      path: '/workspace/procurement/coverage',
      query: { rcm: 'RCM-F08A71' },
    })
  })

  it('gives the record no query state to carry', () => {
    expect(workspaceRoute('procurement', 'record', { rcm: 'RCM-F08A71' })).toEqual({
      path: '/workspace/procurement', query: {},
    })
  })
})

describe('rail sections', () => {
  it('no longer lists the record, which is a surface of its own', () => {
    expect(FILE_SECTIONS).not.toContain('record')
  })

  it('resolves every file section back to a destination', () => {
    for (const section of FILE_SECTIONS) {
      expect(destinationForSection('file', section), section).not.toBeNull()
    }
  })

  it('resolves every bench section back to a destination', () => {
    for (const section of BENCH_SECTIONS) {
      expect(destinationForSection('bench', section), section).not.toBeNull()
    }
  })
})

describe('server-supplied targets', () => {
  /**
   * The backend names destinations directly now. These pin the two phase
   * targets that used to arrive as `planning` and `planning?view=rcm`, because
   * nothing translates that vocabulary any more — a backend that still sent it
   * would route the whole planning phase to the assistant, silently.
   */
  it('routes a phase target to the work product it names', () => {
    expect(routeForTarget('procurement', { tab: 'apm' })).toEqual({
      path: '/workspace/procurement/apm', query: {},
    })
    expect(routeForTarget('procurement', { tab: 'rcm', query: { rcm: 'RCM-F08A71' } })).toEqual({
      path: '/workspace/procurement/coverage', query: { rcm: 'RCM-F08A71' },
    })
  })

  it('sends an unknown target to the assistant rather than nowhere', () => {
    expect(routeForTarget('procurement', { tab: 'not-a-destination' })).toEqual({
      path: '/workspace/procurement/console', query: {},
    })
  })
})
