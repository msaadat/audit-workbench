import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import * as PrimeVueToast from 'primevue/usetoast'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import LinkTestDialog from './LinkTestDialog.vue'

const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver
globalThis.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false,
  addListener() {}, removeListener() {},
})) as unknown as typeof globalThis.matchMedia

function dataTest(id: string, title: string, rcmId: string | null) {
  return { id, title, objective: 'An objective.', table_refs: ['ap_invoices'], rcm_id: rcmId }
}
function docTest(id: string, title: string, rcmId: string | null) {
  return { id, title, objective: '', kind: 'vouching', rcm_id: rcmId }
}

function mountDialog() {
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/data-tests')) {
      return {
        items: [
          dataTest('DAT-1', 'Approver limits', null),
          dataTest('DAT-2', 'Duplicate invoices', 'RCM-B'),
          dataTest('DAT-3', 'Already on this row', 'RCM-A'),
        ],
      } as never
    }
    if (url.endsWith('/doc-tests')) {
      return { items: [docTest('DOC-1', 'Receipt vouching', null)] } as never
    }
    return {} as never
  })
  return mount(LinkTestDialog, {
    props: { modelValue: true, workspaceId: 'WS-1', rcmId: 'RCM-A' },
    global: {
      plugins: [PrimeVue],
      provide: { [PrimeVueToastSymbol]: { add: vi.fn(), remove: vi.fn(), removeGroup: vi.fn(), removeAllGroups: vi.fn() } },
    },
    attachTo: document.body,
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('LinkTestDialog', () => {
  it('offers both registers, and never a test already on this row', async () => {
    mountDialog()
    await flushPromises()

    const titles = [...document.querySelectorAll('.entry strong')].map(node => node.textContent)
    expect(titles).toEqual(['Approver limits', 'Receipt vouching', 'Duplicate invoices'])
    // The row it would be taken from is named on the entry, not implied.
    expect(document.querySelector('.moves')?.textContent).toBe('RCM-B')
  })

  it('patches the chosen test with this row and says so', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    const wrapper = mountDialog()
    await flushPromises()

    const entry = [...document.querySelectorAll<HTMLButtonElement>('.entry')]
      .find(node => node.textContent?.includes('Receipt vouching'))
    entry?.click()
    await flushPromises()

    const link = [...document.querySelectorAll<HTMLButtonElement>('.p-dialog-footer button')]
      .find(node => node.textContent?.includes('Link test'))
    expect(link).toBeTruthy()
    link?.click()
    await flushPromises()

    expect(patch).toHaveBeenCalledWith('/api/workspaces/WS-1/doc-tests/DOC-1', { rcm_id: 'RCM-A' })
    expect(wrapper.emitted('linked')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('calls a test that already covers another row a move, not a link', async () => {
    mountDialog()
    await flushPromises()

    const entry = [...document.querySelectorAll<HTMLButtonElement>('.entry')]
      .find(node => node.textContent?.includes('Duplicate invoices'))
    entry?.click()
    await flushPromises()

    expect(document.querySelector('.warn')?.textContent)
      .toContain('This test covers RCM-B')
    expect([...document.querySelectorAll('.p-dialog-footer button')].map(node => node.textContent?.trim()))
      .toContain('Move to this row')
  })
})
