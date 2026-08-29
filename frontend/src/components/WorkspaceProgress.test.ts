import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { WorkspaceProgress as Progress } from '../types'
import WorkspaceProgress from './WorkspaceProgress.vue'

/**
 * The index card's strip. It says four things and no more, so what these pin is
 * that each one is the backend's own answer rather than a second opinion, and
 * that a workspace the backend could not answer for says so.
 */

function strip(props: { tables: number; progress?: Progress | null }) {
  const wrapper = mount(WorkspaceProgress, { props })
  return wrapper.findAll('.segment').map(segment => ({
    label: segment.text(),
    state: segment.attributes('data-state'),
    title: segment.attributes('title'),
  }))
}

const working: Progress = {
  planning: 'complete',
  fieldwork: 'attention',
  report: 'in_progress',
}

describe('WorkspaceProgress', () => {
  it('draws the four stages in the order the engagement runs them', () => {
    expect(strip({ tables: 18, progress: working }).map(item => item.label)).toEqual([
      'Data', 'Planning', 'Fieldwork', 'Report',
    ])
  })

  it('takes each phase state from the backend without recomputing it', () => {
    expect(strip({ tables: 18, progress: working }).map(item => item.state)).toEqual([
      'complete', 'complete', 'attention', 'in_progress',
    ])
  })

  it('answers the data stage from the table count the listing already carries', () => {
    const [data] = strip({ tables: 0, progress: working })

    expect(data.state).toBe('not_started')
    expect(data.title).toBe('Data: nothing imported yet')
  })

  it('names the state in words, since colour alone is not an answer', () => {
    expect(strip({ tables: 18, progress: working }).map(item => item.title)).toEqual([
      'Data: 18 tables imported',
      'Planning: Complete',
      'Fieldwork: Needs attention',
      'Report: In progress',
    ])
  })

  it('reads a workspace it has no status for as unavailable, not as unstarted', () => {
    const segments = strip({ tables: 18, progress: null })

    expect(segments.map(item => item.state)).toEqual([
      'complete', 'unknown', 'unknown', 'unknown',
    ])
    expect(segments[1].title).toBe('Planning: Unavailable')
  })
})
