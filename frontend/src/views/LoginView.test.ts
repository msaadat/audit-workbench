import Aura from '@primeuix/themes/aura'
import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api'
import LoginView from './LoginView.vue'

/**
 * The sign-in screen is the one surface a locked-out user sees, so a failure
 * that renders as nothing is indistinguishable from a broken button. These pin
 * that every rejection says something, and that the form never keeps a password
 * around after a failed attempt.
 */

const { routeState, replace } = vi.hoisted(() => ({
  routeState: { query: {} as Record<string, string>, params: {} },
  replace: vi.fn(),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return { ...actual, useRoute: () => routeState, useRouter: () => ({ replace }) }
})

const signIn = vi.fn()
const acceptInvite = vi.fn()
vi.mock('../composables/useSession', () => ({
  useSession: () => ({ signIn, acceptInvite }),
}))

const apiGet = vi.fn()
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

// PrimeVue's inputs read their theme config through the plugin, so the plugin
// has to be installed rather than the components stubbed — the point of these
// tests is what the real form renders.
function mountLogin(props: Record<string, unknown> = {}) {
  return mount(LoginView, {
    props,
    global: { plugins: [[PrimeVue, { theme: { preset: Aura } }]] },
  })
}

async function submitWith(wrapper: ReturnType<typeof mountLogin>, email: string, password: string) {
  await wrapper.find('input[type="email"]').setValue(email)
  await wrapper.find('input[type="password"]').setValue(password)
  await wrapper.find('form').trigger('submit')
  await flushPromises()
}

describe('LoginView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    routeState.query = {}
  })

  it('shows the server’s reason when sign-in is rejected', async () => {
    signIn.mockRejectedValue(new ApiError('That email and password do not match an account.'))
    const wrapper = mountLogin()

    await submitWith(wrapper, 'someone@example.com', 'wrong-password')

    expect(wrapper.text()).toContain('That email and password do not match an account.')
    expect(replace).not.toHaveBeenCalled()
  })

  it('still says something when the failure is not an API error', async () => {
    signIn.mockRejectedValue(new TypeError('Failed to fetch'))
    const wrapper = mountLogin()

    await submitWith(wrapper, 'someone@example.com', 'whatever')

    expect(wrapper.text()).toContain('Something went wrong')
  })

  it('does not keep the password after a failed attempt', async () => {
    signIn.mockRejectedValue(new ApiError('nope'))
    const wrapper = mountLogin()

    await submitWith(wrapper, 'someone@example.com', 'wrong-password')

    expect((wrapper.find('input[type="password"]').element as HTMLInputElement).value).toBe('')
  })

  it('returns to the route the guard interrupted', async () => {
    routeState.query = { redirect: '/workspace/ws_1/console' }
    signIn.mockResolvedValue({ id: 'u_1' })
    const wrapper = mountLogin()

    await submitWith(wrapper, 'someone@example.com', 'a-good-password')

    expect(replace).toHaveBeenCalledWith('/workspace/ws_1/console')
  })

  it.each([
    ['an absolute URL', 'https://attacker.example/steal'],
    // Starts with a slash, but a browser reads it as protocol-relative.
    ['a protocol-relative URL', '//attacker.example/steal'],
  ])('refuses %s as a redirect', async (_label, redirect) => {
    // Otherwise the login screen forwards to whatever a crafted link asks for.
    routeState.query = { redirect }
    signIn.mockResolvedValue({ id: 'u_1' })
    const wrapper = mountLogin()

    await submitWith(wrapper, 'someone@example.com', 'a-good-password')

    expect(replace).toHaveBeenCalledWith('/')
  })

  it('asks an invited auditor for a password against their known email', async () => {
    apiGet.mockResolvedValue({ email: 'invited@example.com' })
    const wrapper = mountLogin({ token: 'invite-token' })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/auth/invite/invite-token')
    expect(wrapper.text()).toContain('invited@example.com')
    // No email field: the address is fixed by the invitation.
    expect(wrapper.find('input[type="email"]').exists()).toBe(false)
  })

  it('reports an invitation that cannot be read', async () => {
    apiGet.mockRejectedValue(new ApiError('This invitation is not valid. Ask for a new one.'))
    const wrapper = mountLogin({ token: 'stale' })
    await flushPromises()

    expect(wrapper.text()).toContain('This invitation is not valid')
  })
})
