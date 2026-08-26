import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { reactive, ref } from 'vue'

import EngagementRecordTab from './EngagementRecordTab.vue'
import type {
  EngagementOpenPoint, EngagementPendingStage, EngagementRecordEntry, EngagementRecordPayload,
} from '../types'

/**
 * The record's job is to state what was filed and what it cost without
 * overstating either. These cover the readings the demo engagement actually
 * produces — a sub-second stage, a stage nothing timed, six attempts collapsed
 * into one row — plus the two cases its own data never reaches.
 */

const get = vi.fn()
vi.mock('../api', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
  ApiError: class extends Error {},
}))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))
// The record lays the run in flight over the ledger, so the agent store is not
// incidental to it any more: these stand in for a live run.
const agentActive = ref(false)
const agentState = reactive<{ run: Record<string, unknown> | null }>({ run: null })
const openDrawer = vi.fn()
const agentInit = vi.fn()
vi.mock('../composables/useAgentRun', () => ({
  useAgentRun: () => ({
    state: agentState,
    isActive: agentActive,
    init: agentInit,
    openDrawer,
    onWorkspaceInvalidated: () => () => undefined,
  }),
}))

/** A run in flight, with `capability` naming the stage the ledger should mark. */
function liveRun(
  stages: Array<{ capability: string; status: string; title?: string }>,
  overrides: Record<string, unknown> = {},
) {
  agentActive.value = true
  agentState.run = {
    id: 'live',
    status: 'executing',
    created: '2026-08-15T12:20:00+00:00',
    started: '2026-08-15T12:20:00+00:00',
    activity: null,
    workflow: {
      stages: stages.map(item => ({
        capability: item.capability,
        status: item.status,
        title: item.title ?? item.capability,
        started_at: item.status === 'running' ? '2026-08-15T12:20:10+00:00' : null,
      })),
    },
    ...overrides,
  }
}
const push = vi.fn()
vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({
    to: (destination: string) => ({ path: `/${destination}` }),
    push: (...args: unknown[]) => push(...args),
  }),
}))
const send = vi.fn()
vi.mock('../composables/useAssistantChat', () => ({
  useAssistantChat: () => ({ send: (...args: unknown[]) => send(...args) }),
}))

function entry(overrides: Partial<EngagementRecordEntry> = {}): EngagementRecordEntry {
  return {
    id: 'findings.drafted:m1',
    capability: 'findings.drafted',
    at: '2026-08-15T12:10:00+00:00',
    first_at: '2026-08-14T11:05:00+00:00',
    status: 'completed',
    headline: 'Finding drafts prepared',
    summary: 'Prepared 1 evidence-linked finding draft (1 critical).',
    metrics: [],
    highlights: [],
    objective: 'Draft findings.',
    run_id: 'r3',
    chat_id: 'chat_r3',
    attempts: [
      { run_id: 'r1', run_status: 'completed', at: '2026-08-14T11:05:00+00:00', elapsed_ms: 60_000 },
      { run_id: 'r3', run_status: 'completed', at: '2026-08-15T12:10:00+00:00', elapsed_ms: 60_000 },
    ],
    elapsed_ms: 120_000,
    measured_attempts: 2,
    open_points: [],
    filed: {
      label: 'Findings register',
      destination: 'findings',
      unit: 'finding',
      unit_plural: 'findings',
      count: 35,
    },
    ...overrides,
  }
}

function stage(overrides: Partial<EngagementPendingStage> = {}): EngagementPendingStage {
  return {
    id: 'pending:dashboard.curated',
    capability: 'dashboard.curated',
    headline: 'Pick the analyses worth showing on the dashboard',
    blocked_reason: '',
    runnable: true,
    start: { prompt: 'Curate the dashboard.', outcomes: ['dashboard.curated'] },
    filed: { label: 'Dashboard curation', destination: 'dashboard', unit: '', unit_plural: '', count: null },
    order: 19,
    ...overrides,
  }
}

function point(overrides: Partial<EngagementOpenPoint> = {}): EngagementOpenPoint {
  return {
    key: 'unread_conclusions',
    capability: 'results.rolled_up',
    message: '41 of 60 conclusions were set by the assistant and never read.',
    action: 'Open them',
    destination: 'rcm',
    ...overrides,
  }
}

function payload(
  entries: EngagementRecordEntry[],
  extra: Partial<EngagementRecordPayload> = {},
): EngagementRecordPayload {
  return {
    entries,
    pending: [],
    open_points: [],
    orphaned_points: [],
    next: null,
    counts: {},
    totals: {
      work_products: entries.length,
      runs: 32,
      runs_that_filed: 19,
      attempts: entries.reduce((sum, item) => sum + item.attempts.length, 0),
      elapsed_ms: 3_345_210,
      first_at: '2026-08-13T19:22:24+00:00',
      last_at: '2026-08-15T12:10:00+00:00',
    },
    ...extra,
  }
}

async function render(
  entries: EngagementRecordEntry[],
  extra: Partial<EngagementRecordPayload> = {},
) {
  get.mockResolvedValue(payload(entries, extra))
  const wrapper = mount(EngagementRecordTab, {
    props: { workspace: { id: 'procurement' } as never },
    global: {
      stubs: {
        UiPageHeader: { template: '<div><slot /></div>' },
        UiEmptyState: { template: '<div class="empty" />' },
        Button: { template: '<button />' },
        RouterLink: { props: ['to'], template: '<a :href="String(to?.path)"><slot /></a>' },
      },
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

describe('EngagementRecordTab', () => {
  beforeEach(() => {
    get.mockReset(); push.mockReset(); send.mockReset()
    openDrawer.mockReset(); agentInit.mockReset()
    agentActive.value = false
    agentState.run = null
  })

  it('states an irregular unit from the declared plural, not by appending s', async () => {
    const wrapper = await render([entry({
      filed: { label: 'Analysis library', destination: 'analysis', unit: 'analysis', unit_plural: 'analyses', count: 28 },
    })])

    expect(wrapper.text()).toContain('28 analyses')
    expect(wrapper.text()).not.toContain('analysiss')
  })

  it('renders sub-minute work in seconds rather than rounding it to 0m', async () => {
    // Fieldwork and the roll-up settle the instant their run starts, because
    // the tests they gather already ran. "0m" reads as a broken clock.
    const wrapper = await render([entry({ elapsed_ms: 382, measured_attempts: 1 })])

    expect(wrapper.text()).toContain('<1s')
    expect(wrapper.text()).not.toContain('0m')
  })

  it('leaves the duration unstated when nothing about the work was timed', async () => {
    const wrapper = await render([entry({ elapsed_ms: null, measured_attempts: 0 })])

    expect(wrapper.find('.took').text()).toBe('—')
  })

  it('says how many attempts a collapsed row stands for, and how many were timed', async () => {
    const wrapper = await render([entry({
      attempts: [
        { run_id: 'r1', run_status: 'cancelled', at: '2026-08-14T19:36:20+00:00', elapsed_ms: null },
        { run_id: 'r2', run_status: 'failed', at: '2026-08-14T20:02:09+00:00', elapsed_ms: null },
        { run_id: 'r3', run_status: 'completed', at: '2026-08-14T20:05:00+00:00', elapsed_ms: 382 },
      ],
      measured_attempts: 1,
    })])

    expect(wrapper.text()).toContain('3 attempts · 2 not timed')
  })

  it('stays silent about attempts when a work product took only one', async () => {
    const wrapper = await render([entry({
      attempts: [{ run_id: 'r1', run_status: 'completed', at: '2026-08-14T11:05:00+00:00', elapsed_ms: 60_000 }],
      measured_attempts: 1,
    })])

    expect(wrapper.text()).not.toContain('attempt')
  })

  it('reveals the individual runs behind a collapsed row on request', async () => {
    const wrapper = await render([entry()])

    expect(wrapper.find('.attempts').exists()).toBe(false)
    await wrapper.find('.tries').trigger('click')
    expect(wrapper.findAll('.attempts li')).toHaveLength(2)
  })

  it('links the filed artifact to the surface that opens it', async () => {
    const wrapper = await render([entry()])

    expect(wrapper.find('.card').attributes('href')).toBe('/findings')
  })

  it('does not link a destination this build does not know', async () => {
    const wrapper = await render([entry({
      filed: { label: 'Something new', destination: 'not-a-surface', unit: '', unit_plural: '', count: null },
    })])

    const card = wrapper.find('.card')
    expect(card.exists()).toBe(true)
    expect(card.attributes('href')).toBeUndefined()
  })

  it('shows a capability with no artifact mapping without inventing one', async () => {
    const wrapper = await render([entry({ filed: null, headline: 'A stage the record has never seen' })])

    expect(wrapper.find('.none').exists()).toBe(true)
    expect(wrapper.text()).toContain('A stage the record has never seen')
  })

  it('omits the size of a work product that has no meaningful count', async () => {
    const wrapper = await render([entry({
      filed: { label: 'Audit planning memorandum', destination: 'apm', unit: '', unit_plural: '', count: null },
    })])

    expect(wrapper.text()).toContain('Audit planning memorandum')
    expect(wrapper.find('.mt em').exists()).toBe(false)
  })

  it('reports the runs that filed nothing rather than dropping them silently', async () => {
    const wrapper = await render([entry()])

    // 32 runs, 19 of which filed something.
    expect(wrapper.text()).toContain('13 runs filed nothing')
  })

  it('breaks the ledger by day only where the day changes', async () => {
    const wrapper = await render([
      entry({ id: 'a', at: '2026-08-14T09:00:00+00:00' }),
      entry({ id: 'b', at: '2026-08-14T15:00:00+00:00' }),
      entry({ id: 'c', at: '2026-08-15T09:00:00+00:00' }),
    ])

    expect(wrapper.findAll('.daybreak')).toHaveLength(2)
  })

  // ---------------------------------------------------------------- forward
  it('names the single most blocking thing above the ledger', async () => {
    const wrapper = await render([entry()], { next: { kind: 'open_point', ...point() } })

    const brief = wrapper.find('.brief')
    expect(brief.attributes('data-kind')).toBe('open_point')
    expect(brief.text()).toContain('41 of 60 conclusions')
  })

  it('hangs an open point off the row that created it', async () => {
    const wrapper = await render([entry({
      capability: 'results.rolled_up',
      open_points: [point()],
    })])

    expect(wrapper.find('.row .open').text()).toContain('never read')
  })

  it('takes the reader to where an open point is answered', async () => {
    const wrapper = await render([entry({ open_points: [point()] })])
    await wrapper.find('.row .open').trigger('click')

    expect(push).toHaveBeenCalledWith('rcm')
  })

  it('draws a stage that has not run below a now line', async () => {
    const wrapper = await render([entry()], { pending: [stage()] })

    expect(wrapper.find('.nowline').exists()).toBe(true)
    expect(wrapper.find('.row.ghost').text()).toContain('Dashboard curation')
    expect(wrapper.find('.row.ghost .mt em').text()).toBe('not yet')
  })

  it('marks only the first runnable stage as the call to action', async () => {
    // A tail of buttons is a menu, not a next step.
    const wrapper = await render([entry()], {
      pending: [
        stage({ id: 'a', capability: 'planning.apm_ready' }),
        stage({ id: 'b', capability: 'planning.rcm_ready' }),
      ],
    })

    expect(wrapper.findAll('.row.ghost')).toHaveLength(2)
    expect(wrapper.findAll('.row.ghost.lead')).toHaveLength(1)
  })

  it('gives a blocked stage its reason instead of a button', async () => {
    const wrapper = await render([entry()], {
      pending: [stage({ runnable: false, blocked_reason: 'Waits for the memorandum.' })],
    })

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.text()).toContain('Waits for the memorandum.')
    expect(ghost.find('.waits').exists()).toBe(true)
    expect(ghost.find('button').exists()).toBe(false)
  })

  it('asks the assistant for the stage outcome when one is started', async () => {
    const wrapper = await render([entry()], { pending: [stage()] })
    await wrapper.find('.row.ghost button').trigger('click')

    expect(send).toHaveBeenCalledWith('Curate the dashboard.', 'act', 'auto', {
      source: 'shortcut',
      requestedOutcomes: ['dashboard.curated'],
    })
  })

  it('keeps the ledger on screen when a stage is started, and opens the sidecar', async () => {
    // It used to hand the reader to the console, because the record could not
    // show progress. It can, so the row they just started stays in view.
    const wrapper = await render([entry()], { pending: [stage()] })
    await wrapper.find('.row.ghost button').trigger('click')

    expect(openDrawer).toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })

  it('does not offer to start a stage again in the gap before the run lists it', async () => {
    // A run exists before its route resolves, so for a second or two the stage
    // just asked for is in no workflow yet. Clicking Run twice starts it twice.
    const wrapper = await render([entry()], { pending: [stage()] })
    await wrapper.find('.row.ghost button').trigger('click')
    agentActive.value = true
    agentState.run = {
      id: 'live', status: 'interpreting', created: '2026-08-15T12:20:00+00:00',
      started: null, activity: null, workflow: null,
    }
    await wrapper.vm.$nextTick()

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBe('queued')
    expect(ghost.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('hands a just-started stage back to the run once its workflow names it', async () => {
    const wrapper = await render([entry()], { pending: [stage()] })
    await wrapper.find('.row.ghost button').trigger('click')
    liveRun([{ capability: 'dashboard.curated', status: 'running' }])
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.row.ghost').attributes('data-live')).toBe('running')
    wrapper.unmount()
  })

  it('wakes the agent store itself, which the collapsed sidecar never does', async () => {
    // `ConsoleThread` owns that call, and it is not mounted while the drawer is
    // collapsed — so without this a reload mid-run shows a ledger blind to it.
    await render([entry()])

    expect(agentInit).toHaveBeenCalled()
  })

  it('still reports a debt whose stage never filed', async () => {
    const wrapper = await render([entry()], {
      orphaned_points: [point({ key: 'draft_rcm', message: '27 of 27 rows are still marked draft.' })],
    })

    expect(wrapper.find('.row.orphan').text()).toContain('27 of 27 rows')
  })

  it('says nothing above the ledger when there is nothing to do', async () => {
    const wrapper = await render([entry()])

    expect(wrapper.find('.brief').exists()).toBe(false)
    expect(wrapper.find('.nowline').exists()).toBe(false)
  })

  /* --- the run in flight -------------------------------------------------- */

  it('reports the run in progress instead of proposing work already under way', async () => {
    liveRun([{ capability: 'dashboard.curated', status: 'running' }])
    const wrapper = await render([entry()], {
      pending: [stage()],
      next: { kind: 'stage', ...stage() },
    })

    const brief = wrapper.find('.brief')
    expect(brief.classes()).toContain('live')
    // The band takes the next step's place rather than sitting beside it.
    expect(wrapper.findAll('.brief')).toHaveLength(1)
    expect(brief.text()).toContain('Pick the analyses worth showing on the dashboard')
    expect(brief.text()).toContain('Running')
    wrapper.unmount()
  })

  it('marks the pending row the run is writing, and takes its Run button away', async () => {
    liveRun([{ capability: 'dashboard.curated', status: 'running' }])
    const wrapper = await render([entry()], { pending: [stage()] })

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBe('running')
    expect(ghost.text()).toContain('being written')
    // Offering to start a stage that is already running starts it twice.
    expect(ghost.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('separates a stage the run has merely scheduled from the one it is writing', async () => {
    liveRun([
      { capability: 'report.working_draft', status: 'running' },
      { capability: 'dashboard.curated', status: 'queued' },
    ])
    const wrapper = await render([entry()], { pending: [stage()] })

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBe('queued')
    expect(ghost.text()).toContain('Scheduled by the run in progress')
    expect(ghost.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('leaves a pending row the run never scheduled exactly as it was', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([entry()], { pending: [stage()] })

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBeUndefined()
    expect(ghost.text()).toContain('not yet')
    wrapper.unmount()
  })

  it('drops the call-to-action highlight while a run is deciding the answer', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([entry()], { pending: [stage()] })

    expect(wrapper.find('.row.ghost').classes()).not.toContain('lead')
    wrapper.unmount()
  })

  it('says a filed work product is being produced again rather than looking settled', async () => {
    liveRun([{ capability: 'findings.drafted', status: 'running' }])
    const wrapper = await render([entry()])

    expect(wrapper.find('.again').text()).toContain('Running again')
    wrapper.unmount()
  })

  it('ignores a stage the run has already finished, whose commit filed the row', async () => {
    liveRun([{ capability: 'dashboard.curated', status: 'succeeded' }])
    const wrapper = await render([entry()], { pending: [stage()] })

    expect(wrapper.find('.brief.live').exists()).toBe(true)
    expect(wrapper.find('.row.ghost').attributes('data-live')).toBeUndefined()
    wrapper.unmount()
  })

  it('still names the run when it is waiting on a person, and asks for a reply', async () => {
    liveRun([{ capability: 'dashboard.curated', status: 'running' }], { status: 'awaiting_approval' })
    const wrapper = await render([entry()], { pending: [stage()] })

    const brief = wrapper.find('.brief')
    expect(brief.attributes('data-wait')).toBe('1')
    expect(brief.text()).toContain('Waiting for your approval')
    wrapper.unmount()
  })

  it('reports a run whose route has not resolved yet, which has no stages at all', async () => {
    agentActive.value = true
    agentState.run = {
      id: 'live', status: 'interpreting', created: '2026-08-15T12:20:00+00:00',
      started: null, activity: null, workflow: null,
    }
    const wrapper = await render([entry()], { pending: [stage()] })

    expect(wrapper.find('.brief.live').text()).toContain('Working out what to run')
    expect(wrapper.find('.row.ghost').attributes('data-live')).toBeUndefined()
    wrapper.unmount()
  })
})
