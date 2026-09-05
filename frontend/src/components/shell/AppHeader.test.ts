import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'

import AppHeader from './AppHeader.vue'
import { resetShell, useShell } from '../../composables/useShell'

/**
 * The one bar. It replaced two headers that disagreed about what a header is
 * and three crumb bars that each wrote their own, so what is worth holding
 * here is the seams: what the bar says when it holds nothing, what it puts in
 * the kebab rather than on the bar, and that the switcher opens with the
 * engagement you are in whatever the listing does.
 */

const { routeState, push, replace } = vi.hoisted(() => ({
  routeState: { fullPath: '/', path: '/', params: {} as Record<string, string> },
  push: vi.fn(),
  replace: vi.fn(),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return {
    ...actual,
    useRoute: () => routeState,
    useRouter: () => ({ push, replace }),
    RouterLink: { props: ['to'], template: '<a :href="String(to)"><slot /></a>' },
  }
})

const session = vi.hoisted(() => ({
  state: { user: null as null | { email: string }, singleUser: true, ready: true },
  signOut: vi.fn(),
}))
vi.mock('../../composables/useSession', () => ({ useSession: () => session }))

function render() {
  return mount(AppHeader, { global: { plugins: [PrimeVue] } })
}

/** What the kebab holds, flattened to labels. */
function kebabLabels(wrapper: ReturnType<typeof render>): string[] {
  const menu = wrapper.findComponent({ name: 'Menu' })
  return (menu.props('model') as { label?: string; separator?: boolean }[])
    .map(item => (item.separator ? '—' : item.label ?? ''))
}

afterEach(() => {
  resetShell()
  session.state.user = null
  session.state.singleUser = true
  routeState.path = '/'
  vi.restoreAllMocks()
})

describe('AppHeader', () => {
  /**
   * Inside an engagement the trail is the identity. The old workspace header
   * spent about 330 px on a brand mark, a wordmark, a rule and an eyebrow
   * before it got to the engagement's name — which was not a link.
   */
  it('shows the wordmark only where there is no engagement', async () => {
    const wrapper = render()
    expect(wrapper.find('.app-shell__wordmark').exists()).toBe(true)

    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.app-shell__wordmark').exists()).toBe(false)
    expect(wrapper.find('.app-shell__crumb--engagement').text()).toBe('Procurement')
  })

  it('marks the engagement as the current page until a surface names one', async () => {
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[aria-current="page"]').text()).toBe('Procurement')

    useShell().setTrail([{ label: 'Risk and control matrix' }])
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[aria-current="page"]').text()).toBe('Risk and control matrix')
  })

  it('draws a trail piece with a destination as a link and the last one as text', async () => {
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    useShell().setTrail([
      { label: 'Risk and control matrix', to: '/workspace/ws-1/coverage' },
      { label: 'RCM-A', mono: true },
    ])
    await wrapper.vm.$nextTick()

    const crumbs = wrapper.findAll('.app-shell__crumb')
    expect(crumbs.map(node => node.text())).toEqual(['Procurement', 'Risk and control matrix', 'RCM-A'])
    expect(crumbs[1].element.tagName).toBe('A')
    expect(crumbs[2].element.tagName).toBe('SPAN')
    expect(crumbs[2].classes()).toContain('app-shell__crumb--mono')
  })

  /**
   * Diagnostics is an engagement's page, so it is offered only inside one; the
   * account rows exist only where there is somebody to switch to.
   */
  it('offers diagnostics only inside an engagement', async () => {
    const wrapper = render()
    expect(kebabLabels(wrapper)).not.toContain('Diagnostics')

    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()
    expect(kebabLabels(wrapper)).toContain('Diagnostics')
  })

  it('hides the account rows on a single-user installation', async () => {
    const single = render()
    expect(kebabLabels(single)).not.toContain('Sign out')
    single.unmount()

    session.state.singleUser = false
    session.state.user = { email: 'auditor@example.com' }
    const shared = render()
    expect(kebabLabels(shared)).toContain('Sign out')
    expect(kebabLabels(shared)).toContain('Signed in as auditor@example.com')
  })

  it('names the theme it is on, because the row is the control', () => {
    expect(kebabLabels(render())).toContain('Theme · System')
  })

  /**
   * The rails that used to list a surface's sections beside it went with the
   * rails. This is the control that replaced them: one menu, on every page,
   * naming every view in the engagement.
   */
  it('lists every view in the engagement, grouped as the record groups them', async () => {
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()

    await wrapper.find('.app-shell__switch').trigger('click')

    const names = wrapper.findAll('.switcher__name').map(node => node.text())
    expect(names).toEqual([
      'Engagement record',
      'Audit planning memorandum', 'Cycle design', 'Risk and control matrix',
      'Data tests', 'Document tests', 'Findings', 'Chain', 'Draft audit report',
      'Documents', 'Source tables', 'Query', 'Analysis',
    ])
  })

  it('links each view at its own path', async () => {
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()
    await wrapper.find('.app-shell__switch').trigger('click')

    const hrefs = wrapper.findAll('.switcher__row').map(node => node.attributes('href'))
    expect(hrefs[0]).toBe('/workspace/ws-1')
    expect(hrefs).toContain('/workspace/ws-1/coverage')
    expect(hrefs).toContain('/workspace/ws-1/bench/analysis')
  })

  /**
   * A row page lives under the matrix's own section, so the matrix is what it
   * marks — the same thing its trail names.
   */
  it('marks the view you are on, including from one of its rows', async () => {
    routeState.path = '/workspace/ws-1/coverage/RCM-A'
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()
    await wrapper.find('.app-shell__switch').trigger('click')

    const current = wrapper.findAll('.switcher__row--current')
    expect(current).toHaveLength(1)
    expect(current[0].text()).toBe('Risk and control matrix')
  })

  it('does not mark the record from a page under it', async () => {
    routeState.path = '/workspace/ws-1/apm'
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()
    await wrapper.find('.app-shell__switch').trigger('click')

    expect(wrapper.findAll('.switcher__row--current').map(node => node.text()))
      .toEqual(['Audit planning memorandum'])
  })

  it('closes the menu on Escape', async () => {
    const wrapper = render()
    useShell().setEngagement({ id: 'ws-1', name: 'Procurement' })
    await wrapper.vm.$nextTick()
    await wrapper.find('.app-shell__switch').trigger('click')
    expect(wrapper.find('.switcher').exists()).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.switcher').exists()).toBe(false)
  })
})
