import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

import AuditFileView from './AuditFileView.vue'
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
  PlanningTab: { props: ['section'], template: '<div class="stub-planning">{{ section }}</div>' },
  DataTestsTab: { template: '<div class="stub-data-tests" />' },
  DocTestsTab: { template: '<div class="stub-doc-tests" />' },
  ChainView: { template: '<div class="stub-chain" />' },
  FindingsTab: { template: '<div class="stub-findings" />' },
  ReportTab: { template: '<div class="stub-report" />' },
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
  it.each([
    ['apm', '.stub-planning'],
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

  // The two planning sections share a component and are told apart by a prop,
  // so a broken hand-off would silently show the memorandum under /coverage.
  it('tells the two planning sections apart by the section it passes down', () => {
    const apm = render('apm')
    const rcm = render('coverage')
    expect(apm.find('.stub-planning').text()).toBe('apm')
    expect(rcm.find('.stub-planning').text()).toBe('rcm')
    apm.unmount()
    rcm.unmount()
  })

  /**
   * The rail used to say where you were by highlighting the entry beside you.
   * With it gone, this bar is the only navigation on the surface, so it has to
   * name the page and carry the way back on every one of them — not just the
   * ones a reader is likely to open first.
   */
  it.each([
    ['apm', 'Audit planning memorandum'],
    ['coverage', 'Risk and control matrix'],
    ['data-tests', 'Test programme'],
    ['doc-tests', 'Document test results'],
    ['findings', 'Findings register'],
    ['chain', 'Chain'],
    ['report', 'Report'],
  ])('names %s in the crumb bar and points it back at the record', (section, label) => {
    const wrapper = render(section)

    expect(wrapper.find('.crumb__cur').text()).toBe(label)
    const back = wrapper.find('.crumb__back')
    expect(back.text()).toContain('Engagement record')
    expect(back.attributes('href')).toBe('/record')
    wrapper.unmount()
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
