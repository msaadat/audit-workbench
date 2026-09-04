import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import UiVerdictBar from './UiVerdictBar.vue'

function mountBar(props: Record<string, unknown> = {}, slots: Record<string, string> = {}) {
  return mount(UiVerdictBar, {
    props: { tone: 'bad', ...props },
    slots: {
      found: '<span>2 of 52 records failed</span><span>· 2 still open · run 4 Sep 09:26</span>',
      recorded: 'Concluded <b>Ineffective</b> by an unattended run.',
      ...slots,
    },
  })
}

describe('UiVerdictBar', () => {
  it('keeps what the run found apart from what is recorded', () => {
    const wrapper = mountBar()

    // Two facts of different kinds: one about the data, one about the file.
    expect(wrapper.find('.found').text()).toContain('2 of 52 records failed')
    expect(wrapper.find('.recorded').text()).toContain('by an unattended run')
    expect(wrapper.find('.dot').attributes('data-tone')).toBe('bad')
  })

  it('carries the page’s own recording controls', () => {
    const wrapper = mountBar({}, { actions: '<button>Accept conclusion</button>' })

    expect(wrapper.find('.actions button').text()).toBe('Accept conclusion')
  })

  it('attaches the stale sentence to the card rather than floating it', () => {
    const plain = mountBar()
    expect(plain.find('.stale').exists()).toBe(false)

    const stale = mountBar({
      stale: 'The conclusion was recorded against an earlier run. Accepting re-affirms it against this one.',
    })
    // Attached, so the qualification is read with the figures it qualifies.
    expect(stale.find('.verdict-bar > .stale').text()).toContain('recorded against an earlier run')
  })
})
