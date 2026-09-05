import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

import WorkbenchView from './WorkbenchView.vue'
import { resetShell, useShell } from '../composables/useShell'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import type { WorkspaceContext } from '../composables/useWorkspaceContext'

/**
 * This host lost its rail when the record became the index. Every section it
 * serves is now a door on a record row — documents and tables from Sources, the
 * analysis library from its own row, Query as the tool beside it — so a section
 * that stops resolving is a door that opens nothing.
 */

const replace = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ replace }),
  RouterLink: { props: ['to'], template: '<a :href="String(to?.path)"><slot /></a>' },
}))
// Only the router binding is stubbed: `BENCH_SECTIONS` is the real list, which
// is what this host answers for.
vi.mock('../composables/useWorkspaceNavigation', async importActual => ({
  ...(await importActual<object>()),
  useWorkspaceNav: () => ({ to: (destination: string) => ({ path: `/${destination}` }) }),
}))

const stubs = {
  DocumentsTab: { template: '<div class="stub-documents" />' },
  DataTab: { template: '<div class="stub-tables" />' },
  QueryTab: { template: '<div class="stub-query" />' },
  AnalysisTab: { template: '<div class="stub-analysis" />' },
}

function render(section: string) {
  return mount(WorkbenchView, {
    props: { id: 'procurement', section },
    global: {
      stubs,
      provide: {
        [workspaceContextKey as symbol]: {
          workspace: ref({ id: 'procurement' }),
          phases: ref([]),
          sectionById: ref({}),
          reload: vi.fn(),
          reloadStatus: vi.fn(),
          requestImport: vi.fn(),
        } as unknown as WorkspaceContext,
      },
    },
  })
}

describe('WorkbenchView', () => {
  beforeEach(resetShell)

  it.each([
    ['documents', 'Documents'],
    ['tables', 'Source tables'],
    ['query', 'Query'],
    ['analysis', 'Analysis'],
  ])('names %s in the shell trail', (section, label) => {
    const wrapper = render(section)

    expect(useShell().trail.value).toEqual([{ label }])
    wrapper.unmount()
    expect(useShell().trail.value).toEqual([])
  })

  it('draws no rail, because the record is the index', () => {
    const wrapper = render('documents')
    expect(wrapper.find('.ui-surface__rail').exists()).toBe(false)
    wrapper.unmount()
  })

  /**
   * Both hold in-memory state — a half-built query survives a trip to Tables —
   * so they are hidden rather than unmounted, and stay in one host rather than
   * being split across the two record doors that reach them.
   */
  it('keeps tables and query mounted together whichever of them is showing', () => {
    const wrapper = render('query')

    expect(wrapper.find('.stub-tables').exists()).toBe(true)
    expect(wrapper.find('.stub-query').exists()).toBe(true)
    // Documents is not: nothing there survives a remount.
    expect(wrapper.find('.stub-documents').exists()).toBe(false)
    wrapper.unmount()
  })

  it('sends a section it does not answer for back to the first one', () => {
    replace.mockClear()
    const wrapper = render('tiles')
    expect(replace).toHaveBeenCalledWith({ path: '/documents' })
    wrapper.unmount()
  })
})
