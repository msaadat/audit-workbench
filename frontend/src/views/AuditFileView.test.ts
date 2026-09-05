import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

import AuditFileView from './AuditFileView.vue'
import { resetShell, useShell } from '../composables/useShell'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import type { WorkspaceContext } from '../composables/useWorkspaceContext'

/**
 * This host lost its rail when the record became the index. What it still owns
 * is which component answers for which path — every one of them reached from a
 * record row or from the record's chain link, so a section that stops resolving
 * is a work product nothing can open any more.
 */

const replace = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ replace }),
  RouterLink: { props: ['to'], template: '<a :href="String(to?.path)"><slot /></a>' },
}))
// Only the router binding is stubbed. `FILE_SECTIONS` is the real list — it is
// what this host now answers for, so a test that supplied its own would prove
// nothing about the sections the app actually routes to.
vi.mock('../composables/useWorkspaceNavigation', async importActual => ({
  ...(await importActual<object>()),
  useWorkspaceNav: () => ({ to: (destination: string) => ({ path: `/${destination}` }) }),
}))

const stubs = {
  PlanningTab: { template: '<div class="stub-planning" />' },
  ApmView: { template: '<div class="stub-apm" />' },
  DataTestsTab: { template: '<div class="stub-data-tests" />' },
  DocTestsTab: { template: '<div class="stub-doc-tests" />' },
  ChainView: { template: '<div class="stub-chain" />' },
  FindingsTab: { template: '<div class="stub-findings" />' },
  ReportView: { template: '<div class="stub-report" />' },
}

function render(section: string) {
  return mount(AuditFileView, {
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

describe('AuditFileView', () => {
  beforeEach(resetShell)

  it.each([
    ['apm', '.stub-apm'],
    ['coverage', '.stub-planning'],
    ['data-tests', '.stub-data-tests'],
    ['doc-tests', '.stub-doc-tests'],
    ['findings', '.stub-findings'],
    ['chain', '.stub-chain'],
    ['report', '.stub-report'],
  ])('renders %s, which a record door or the chain link opens', (section, selector) => {
    const wrapper = render(section)
    expect(wrapper.find(selector).exists()).toBe(true)
    wrapper.unmount()
  })

  // The memorandum and the matrix were one component told apart by a prop, so
  // a broken hand-off silently showed the memorandum under /coverage. They are
  // two views now, and the test is that neither answers for the other.
  it('answers for the memorandum and the matrix with different views', () => {
    const apm = render('apm')
    const rcm = render('coverage')
    expect(apm.find('.stub-planning').exists()).toBe(false)
    expect(rcm.find('.stub-apm').exists()).toBe(false)
    apm.unmount()
    rcm.unmount()
  })

  /**
   * The rail used to say where you were by highlighting the entry beside you.
   * With it gone, the trail in the shell bar is the only thing that says which
   * work product this is, so this host has to publish it on every one of them
   * — not just the ones a reader is likely to open first. The way back is the
   * engagement crumb the bar draws before it, which is why nothing here draws
   * a link of its own any more.
   */
  it.each([
    ['apm', 'Audit planning memorandum'],
    ['coverage', 'Risk and control matrix'],
    ['data-tests', 'Data tests'],
    ['doc-tests', 'Document tests'],
    ['findings', 'Findings'],
    ['chain', 'Chain'],
    ['report', 'Draft audit report'],
  ])('names %s in the shell trail', (section, label) => {
    const wrapper = render(section)

    expect(useShell().trail.value).toEqual([{ label }])
    wrapper.unmount()
    // A surface withdraws its trail when it leaves, or the bar would keep
    // naming a page nobody is on.
    expect(useShell().trail.value).toEqual([])
  })

  it('draws no rail, because the record is the index', () => {
    const wrapper = render('apm')
    expect(wrapper.find('.ui-surface__rail').exists()).toBe(false)
    wrapper.unmount()
  })

  it('sends a section it does not answer for back to the first one', () => {
    replace.mockClear()
    const wrapper = render('dashboard')
    expect(replace).toHaveBeenCalledWith({ path: '/apm' })
    wrapper.unmount()
  })
})
