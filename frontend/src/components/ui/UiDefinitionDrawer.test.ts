import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import UiDefinitionDrawer from './UiDefinitionDrawer.vue'

/** A Drawer that renders inline, so the shell it wraps can be asserted. */
const DrawerStub = {
  props: ['visible'],
  template: '<div class="drawer-host"><slot /></div>',
}
const ButtonStub = {
  props: ['label', 'disabled'],
  emits: ['click'],
  template: '<button :disabled="disabled || undefined" @click="$emit(\'click\')">{{ label }}</button>',
}

function mountDrawer(props: Record<string, unknown> = {}) {
  return mount(UiDefinitionDrawer, {
    props: { modelValue: true, eyebrow: 'NEW TEST', title: 'Untitled', ...props },
    slots: { default: '<p class="body">the fields</p>' },
    global: { stubs: { Drawer: DrawerStub, Button: ButtonStub } },
  })
}

function labels(wrapper: ReturnType<typeof mountDrawer>) {
  return wrapper.findAll('.foot button').map(node => node.text())
}

describe('UiDefinitionDrawer', () => {
  it('promises what the primary will do, which is all that differs between the two', () => {
    // `New test` was a 56rem modal and `Edit definition` a right drawer, for
    // the same fields.
    expect(labels(mountDrawer())).toEqual(['Cancel', 'Save only', 'Create and run'])
    expect(labels(mountDrawer({ editing: true }))).toEqual(['Cancel', 'Save only', 'Save and run'])
  })

  it('says what an edit costs, and only where it costs it', () => {
    const consequence = 'Changing the definition marks the recorded conclusion out of date.'
    expect(mountDrawer({ consequence }).find('.consequence').exists()).toBe(false)
    expect(mountDrawer({ consequence, editing: true }).find('.consequence').text()).toBe(consequence)
  })

  it('holds both saves back until the definition could actually run', async () => {
    const wrapper = mountDrawer()
    const saves = wrapper.findAll('.foot button').slice(1)

    expect(saves.every(node => node.attributes('disabled') !== undefined)).toBe(true)
    // No blocker sentence: a missing value is outlined where the value goes.
    expect(wrapper.find('.foot').text()).not.toMatch(/Pick|Complete|Add a/)

    await wrapper.setProps({ ready: true })
    await wrapper.findAll('.foot button')[2].trigger('click')
    expect(wrapper.emitted('save')?.[0]).toEqual([true])
    await wrapper.findAll('.foot button')[1].trigger('click')
    expect(wrapper.emitted('save')?.[1]).toEqual([false])
  })

  it('closes from Cancel and from its own close control', async () => {
    const byClose = mountDrawer()
    await byClose.find('.close').trigger('click')
    expect(byClose.emitted('update:modelValue')?.[0]).toEqual([false])

    const byCancel = mountDrawer()
    await byCancel.findAll('.foot button')[0].trigger('click')
    expect(byCancel.emitted('update:modelValue')?.[0]).toEqual([false])
  })
})
