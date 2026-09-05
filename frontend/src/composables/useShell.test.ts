import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'

import { resetShell, useEngagementCrumb, useShell, useTrail } from './useShell'

/**
 * The bar is drawn once, above the router view, and every surface underneath
 * takes turns saying what it should read. What has to hold is the hand-off.
 */

const Surface = defineComponent({
  props: { label: { type: String, required: true } },
  setup(props) {
    useTrail(() => [{ label: props.label }])
    return () => h('div')
  },
})

const Engagement = defineComponent({
  props: { name: { type: String, required: true } },
  setup(props) {
    useEngagementCrumb(() => ({ id: 'ws-1', name: props.name }))
    return () => h('div')
  },
})

describe('the shell trail', () => {
  it('follows the surface that is mounted', async () => {
    resetShell()
    const label = ref('Risk and control matrix')
    const wrapper = mount(Surface, { props: { label: label.value } })

    expect(useShell().trail.value).toEqual([{ label: 'Risk and control matrix' }])
    await wrapper.setProps({ label: 'Data tests' })
    expect(useShell().trail.value).toEqual([{ label: 'Data tests' }])

    wrapper.unmount()
    expect(useShell().trail.value).toEqual([])
  })

  /**
   * The one that bit: Vue runs `onUnmounted` as a post-render effect, so on a
   * route change the incoming surface has already published by the time the
   * outgoing one is torn down. An unguarded clear wiped the new trail and the
   * bar went blank on every move between two work products.
   */
  it('is not cleared by a surface that has already been replaced', () => {
    resetShell()
    const outgoing = mount(Surface, { props: { label: 'Risk and control matrix' } })
    const incoming = mount(Surface, { props: { label: 'Analysis' } })

    outgoing.unmount()

    expect(useShell().trail.value).toEqual([{ label: 'Analysis' }])
    incoming.unmount()
    expect(useShell().trail.value).toEqual([])
  })

  // The diagnostics view is a route of its own beside the workspace shell, so
  // the two of them hand the engagement over in exactly the same order.
  it('holds the engagement through the same hand-off', () => {
    resetShell()
    const outgoing = mount(Engagement, { props: { name: 'Procurement' } })
    const incoming = mount(Engagement, { props: { name: 'Procurement · Diagnostics' } })

    outgoing.unmount()

    expect(useShell().engagement.value).toEqual({ id: 'ws-1', name: 'Procurement · Diagnostics' })
    incoming.unmount()
    expect(useShell().engagement.value).toBeNull()
  })
})
