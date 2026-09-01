import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { reactive, ref } from 'vue'

import EngagementRecordTab from './EngagementRecordTab.vue'
import type {
  EngagementOpenPoint, EngagementRecordPayload, EngagementStage, EngagementStageHistory,
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

function history(
  overrides: Partial<EngagementStageHistory> = {},
): EngagementStageHistory {
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
    stats: [],
    ...overrides,
  }
}

/** A stage the engagement holds, with the runs that filed it behind it. */
function filed(overrides: Partial<EngagementStage> = {}): EngagementStage {
  return {
    id: 'stage:findings.drafted',
    capability: 'findings.drafted',
    order: 17,
    held: true,
    runnable: false,
    headline: 'Draft findings from the exceptions',
    blocked_reason: '',
    start: null,
    action: '',
    links: [],
    filed: {
      label: 'Findings register',
      destination: 'findings',
      unit: 'finding',
      unit_plural: 'findings',
      count: 35,
    },
    readiness: { state: 'satisfied', reasons: [], details: {} },
    summary: 'Prepared 1 evidence-linked finding draft (1 critical).',
    stats: [],
    highlights: [],
    live_body: true,
    open_points: [],
    history: history(),
    ...overrides,
  }
}

/** A stage whose work product does not exist yet. */
function owed(overrides: Partial<EngagementStage> = {}): EngagementStage {
  return {
    id: 'stage:report.working_draft',
    capability: 'report.working_draft',
    order: 19,
    held: false,
    runnable: true,
    headline: 'Write the report from the findings',
    blocked_reason: '',
    start: {
      prompt: 'Generate the report.',
      outcomes: ['report.working_draft'],
      alternates: [],
    },
    action: 'run',
    links: [],
    filed: { label: 'Report', destination: 'report', unit: '', unit_plural: '', count: null },
    readiness: { state: 'missing', reasons: [], details: {} },
    summary: '',
    stats: [],
    highlights: [],
    live_body: false,
    open_points: [],
    history: null,
    ...overrides,
  }
}

/** The one owed stage that offers a narrower run under its button. */
function documentsOwed(): EngagementStage {
  return owed({
    id: 'stage:documents.analysis_generated',
    capability: 'documents.analysis_generated',
    order: 2,
    headline: 'Analyse the imported documents',
    start: {
      prompt: 'Analyse the documents.',
      outcomes: [
        'documents.categorized', 'documents.types_classified',
        'documents.schemas_stamped', 'documents.analysis_generated',
      ],
      alternates: [{
        label: 'Planning documents only',
        prompt: 'Analyse the planning documents.',
        outcomes: ['documents.analysis_generated'],
        note: 'The RCM will read it later.',
      }],
    },
    filed: {
      label: 'Document analyses', destination: 'documents',
      unit: 'document', unit_plural: '', count: null,
    },
  })
}

/** A `Storage` for an environment whose window does not provide one. */
function installLocalStorage() {
  if (window.localStorage) return
  const store = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() { return store.size },
    },
  })
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
  stages: EngagementStage[],
  extra: Partial<EngagementRecordPayload> = {},
): EngagementRecordPayload {
  const settled = stages.filter(item => item.history)
  return {
    stages,
    open_points: [],
    next: null,
    counts: {},
    totals: {
      work_products: stages.filter(item => item.held).length,
      runs: 32,
      runs_that_filed: 19,
      attempts: settled.reduce((sum, item) => sum + (item.history?.attempts.length ?? 0), 0),
      elapsed_ms: 3_345_210,
      first_at: '2026-08-13T19:22:24+00:00',
      last_at: '2026-08-15T12:10:00+00:00',
    },
    ...extra,
  }
}

async function render(
  stages: EngagementStage[],
  extra: Partial<EngagementRecordPayload> = {},
) {
  get.mockResolvedValue(payload(stages, extra))
  const wrapper = mount(EngagementRecordTab, {
    props: { workspace: { id: 'procurement' } as never },
    global: {
      stubs: {
        UiPageHeader: { template: '<div><slot /></div>' },
        UiEmptyState: { template: '<div class="empty" />' },
        Button: { template: '<button />' },
        // Flattened so a test can click the primary and an alternate without
        // driving the popup PrimeVue teleports.
        SplitButton: {
          name: 'SplitButton',
          props: ['model'],
          emits: ['click'],
          template: `<span class="split">
            <button class="split-main" @click="$emit('click')" />
            <button
              v-for="item in model" :key="item.label"
              class="split-alt" @click="item.command()"
            >{{ item.label }}</button>
          </span>`,
        },
        RouterLink: { props: ['to'], template: '<a :href="String(to?.path)"><slot /></a>' },
      },
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

/**
 * A milestone's body is folded away by default, so anything below the headline
 * has to be asked for. Rows are shut on mount; this is how a test reads one.
 */
async function openRow(wrapper: Awaited<ReturnType<typeof render>>, index = 0) {
  await wrapper.findAll('.row .chev')[index].trigger('click')
  return wrapper
}

describe('EngagementRecordTab', () => {
  beforeEach(() => {
    get.mockReset(); push.mockReset(); send.mockReset()
    openDrawer.mockReset(); agentInit.mockReset()
    agentActive.value = false
    agentState.run = null
    // Density is a stored preference, so one test choosing Full would otherwise
    // decide what every test after it renders.
    //
    // This environment's jsdom global carries no `localStorage`, so the store
    // is supplied here. Without it the component still renders — every access
    // it makes is guarded, because a private-mode browser throws on exactly
    // this — but the two tests about remembering a density would be asserting
    // against a preference that was never stored.
    installLocalStorage()
    window.localStorage.clear()
  })

  it('states an irregular unit from the declared plural, not by appending s', async () => {
    const wrapper = await render([filed({
      filed: { label: 'Analysis library', destination: 'analysis', unit: 'analysis', unit_plural: 'analyses', count: 28 },
    })])

    // The pill carries the bare count; the spelled unit is what it is named.
    expect(wrapper.find('.card').attributes('title')).toBe('28 analyses')
    expect(wrapper.find('.mt em').text()).toBe('28')
    expect(wrapper.html()).not.toContain('analysiss')
  })

  it('renders sub-minute work in seconds rather than rounding it to 0m', async () => {
    // Fieldwork and the roll-up settle the instant their run starts, because
    // the tests they gather already ran. "0m" reads as a broken clock.
    const wrapper = await render([filed({ history: history({ elapsed_ms: 382, measured_attempts: 1 }) })])

    expect(wrapper.text()).toContain('<1s')
    expect(wrapper.text()).not.toContain('0m')
  })

  it('leaves the duration unstated when nothing about the work was timed', async () => {
    const wrapper = await render([filed({ history: history({ elapsed_ms: null, measured_attempts: 0 }) })])

    expect(wrapper.find('.took').text()).toBe('—')
  })

  it('says how many attempts a collapsed row stands for, and how many were timed', async () => {
    const wrapper = await render([filed({ history: history({ attempts: [
        { run_id: 'r1', run_status: 'cancelled', at: '2026-08-14T19:36:20+00:00', elapsed_ms: null },
        { run_id: 'r2', run_status: 'failed', at: '2026-08-14T20:02:09+00:00', elapsed_ms: null },
        { run_id: 'r3', run_status: 'completed', at: '2026-08-14T20:05:00+00:00', elapsed_ms: 382 },
      ],
      measured_attempts: 1 }) })])

    // Shut, the count is a chip; opened, it says what the count is made of.
    expect(wrapper.find('.sig').text()).toBe('3attempts')
    await openRow(wrapper)
    expect(wrapper.text()).toContain('3 attempts · 2 not timed')
  })

  it('stays silent about attempts when a work product took only one', async () => {
    const wrapper = await render([filed({ history: history({ attempts: [{ run_id: 'r1', run_status: 'completed', at: '2026-08-14T11:05:00+00:00', elapsed_ms: 60_000 }],
      measured_attempts: 1 }) })])

    expect(wrapper.text()).not.toContain('attempt')
  })

  it('reveals the individual runs behind a collapsed row on request', async () => {
    const wrapper = await render([filed()])

    await openRow(wrapper)
    expect(wrapper.find('.attempts').exists()).toBe(false)
    await wrapper.find('.tries').trigger('click')
    expect(wrapper.findAll('.attempts li')).toHaveLength(2)
  })

  it('links the filed artifact to the surface that opens it', async () => {
    const wrapper = await render([filed()])

    expect(wrapper.find('.card').attributes('href')).toBe('/findings')
  })

  it('does not link a destination this build does not know', async () => {
    const wrapper = await render([filed({
      filed: { label: 'Something new', destination: 'not-a-surface', unit: '', unit_plural: '', count: null },
    })])

    const card = wrapper.find('.card')
    expect(card.exists()).toBe(true)
    expect(card.attributes('href')).toBeUndefined()
  })

  it('shows a capability with no artifact mapping without inventing one', async () => {
    const wrapper = await render([filed({
      filed: null,
      headline: '',
      history: history({ headline: 'A stage the record has never seen' }),
    })])

    expect(wrapper.find('.none').exists()).toBe(true)
    expect(wrapper.text()).toContain('A stage the record has never seen')
  })

  it('omits the size of a work product that has no meaningful count', async () => {
    const wrapper = await render([filed({
      filed: { label: 'Audit planning memorandum', destination: 'apm', unit: '', unit_plural: '', count: null },
    })])

    expect(wrapper.text()).toContain('Audit planning memorandum')
    expect(wrapper.find('.mt em').exists()).toBe(false)
  })

  it('reports the runs that filed nothing rather than dropping them silently', async () => {
    const wrapper = await render([filed()])

    // 32 runs, 19 of which filed something.
    expect(wrapper.text()).toContain('13 runs filed nothing')
  })

  it('draws a stage the engagement holds that no run ever filed', async () => {
    // The run folder is gone, or the stage never narrated. The register is
    // still there, and the row still has to say so. This is the reading that
    // rendered an empty record over eleven real work products.
    const wrapper = await render([filed({ history: null })])

    expect(wrapper.find('.empty').exists()).toBe(false)
    expect(wrapper.find('.row').text()).toContain('Findings register')
    expect(wrapper.find('.mt em').text()).toBe('35')
    expect(wrapper.find('.took').text()).toBe('')
  })

  it('states what a held stage still owes without contradicting its count', async () => {
    // Readiness answers the scheduler's question, and on a register that is
    // thirty-five drafted and two short it reads "missing". Both are true.
    const wrapper = await render([filed({
      readiness: {
        state: 'missing',
        reasons: ['2 eligible observations need finding drafts'],
        details: { eligible: 37 },
      },
    })])
    await openRow(wrapper)

    expect(wrapper.find('.mt em').text()).toBe('35')
    expect(wrapper.find('.left').text()).toContain('2 eligible observations need finding drafts')
  })

  // ---------------------------------------------------------------- forward
  it('names the single most blocking thing above the ledger', async () => {
    const wrapper = await render([filed()], { next: { kind: 'open_point', ...point() } })

    const brief = wrapper.find('.brief')
    expect(brief.attributes('data-kind')).toBe('open_point')
    expect(brief.text()).toContain('41 of 60 conclusions')
  })

  it('hangs an open point off the row that created it', async () => {
    const wrapper = await render([filed({
      capability: 'results.rolled_up',
      open_points: [point()],
    })])

    expect(wrapper.find('.row .open').text()).toContain('never read')
  })

  it('takes the reader to where an open point is answered', async () => {
    const wrapper = await render([filed({ open_points: [point()] })])
    await wrapper.find('.row .open').trigger('click')

    expect(push).toHaveBeenCalledWith('rcm')
  })

  it('draws a stage that has not run below a now line', async () => {
    const wrapper = await render([filed(), owed()])

    expect(wrapper.find('.nowline').exists()).toBe(true)
    expect(wrapper.find('.row.ghost').text()).toContain('Report')
    expect(wrapper.find('.row.ghost .mt em').text()).toBe('not yet')
  })

  it('marks only the first runnable stage as the call to action', async () => {
    // A tail of buttons is a menu, not a next step.
    const wrapper = await render([
      filed(),
      owed({ id: 'a', capability: 'planning.apm_ready' }),
      owed({ id: 'b', capability: 'planning.rcm_ready' }),
    ])

    expect(wrapper.findAll('.row.ghost')).toHaveLength(2)
    expect(wrapper.findAll('.row.ghost.lead')).toHaveLength(1)
  })

  it('gives a blocked stage its reason instead of a button', async () => {
    const wrapper = await render([
      filed(),
      owed({ runnable: false, blocked_reason: 'Waits for the memorandum.' }),
    ])

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.text()).toContain('Waits for the memorandum.')
    expect(ghost.find('.waits').exists()).toBe(true)
    expect(ghost.find('button').exists()).toBe(false)
  })

  it('asks the assistant for the stage outcome when one is started', async () => {
    const wrapper = await render([filed(), owed()])
    await wrapper.find('.row.ghost button').trigger('click')

    expect(send).toHaveBeenCalledWith('Generate the report.', 'act', 'auto', {
      source: 'shortcut',
      requestedOutcomes: ['report.working_draft'],
    })
  })

  it('keeps the ledger on screen when a stage is started, and opens the sidecar', async () => {
    // It used to hand the reader to the console, because the record could not
    // show progress. It can, so the row they just started stays in view.
    const wrapper = await render([filed(), owed()])
    await wrapper.find('.row.ghost button').trigger('click')

    expect(openDrawer).toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
  })

  it('does not offer to start a stage again in the gap before the run lists it', async () => {
    // A run exists before its route resolves, so for a second or two the stage
    // just asked for is in no workflow yet. Clicking Run twice starts it twice.
    const wrapper = await render([filed(), owed()])
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

  it('asks for the complete outcome set when a split stage is clicked', async () => {
    // The click is the complete answer. A cheaper one reachable in a click and
    // a complete one reachable in two is what analysed 1 document of 9.
    const wrapper = await render([filed(), documentsOwed()])
    await wrapper.find('.split-main').trigger('click')

    expect(send).toHaveBeenCalledWith(
      'Analyse the documents.', 'act', 'auto',
      expect.objectContaining({
        requestedOutcomes: [
          'documents.categorized', 'documents.types_classified',
          'documents.schemas_stamped', 'documents.analysis_generated',
        ],
      }),
    )
  })

  it('asks for the narrower set when an alternate is chosen', async () => {
    const wrapper = await render([filed(), documentsOwed()])
    await wrapper.find('.split-alt').trigger('click')

    expect(send).toHaveBeenCalledWith(
      'Analyse the planning documents.', 'act', 'auto',
      expect.objectContaining({
        requestedOutcomes: ['documents.analysis_generated'],
      }),
    )
  })

  it('carries the alternate note, which is what says the work is deferred', async () => {
    const wrapper = await render([filed(), documentsOwed()])

    expect(wrapper.find('.split-alt').text()).toBe('Planning documents only')
    expect(wrapper.findComponent({ name: 'SplitButton' }).props('model'))
      .toEqual([expect.objectContaining({ note: 'The RCM will read it later.' })])
  })

  it('draws a plain button for a stage with nothing narrower to offer', async () => {
    const wrapper = await render([filed(), owed()])

    expect(wrapper.find('.split').exists()).toBe(false)
    expect(wrapper.find('.row.ghost button').exists()).toBe(true)
  })

  it('hands a just-started stage back to the run once its workflow names it', async () => {
    const wrapper = await render([filed(), owed()])
    await wrapper.find('.row.ghost button').trigger('click')
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.row.ghost').attributes('data-live')).toBe('running')
    wrapper.unmount()
  })

  it('wakes the agent store itself, which the collapsed sidecar never does', async () => {
    // `ConsoleThread` owns that call, and it is not mounted while the drawer is
    // collapsed — so without this a reload mid-run shows a ledger blind to it.
    await render([filed()])

    expect(agentInit).toHaveBeenCalled()
  })

  it('still reports a debt whose stage never filed', async () => {
    // It used to be orphaned into a row of its own, because the stage that owed
    // it had no row. Every stage is drawn now, so the debt sits where it came
    // from even when nothing filed that stage.
    const wrapper = await render([filed({
      history: null,
      open_points: [point({ key: 'draft_rcm', message: '27 of 27 rows are still marked draft.' })],
    })])

    expect(wrapper.find('.row .open').text()).toContain('27 of 27 rows')
  })

  it('says nothing above the ledger when there is nothing to do', async () => {
    const wrapper = await render([filed()])

    expect(wrapper.find('.brief').exists()).toBe(false)
    expect(wrapper.find('.nowline').exists()).toBe(false)
  })

  /* --- how much of a row is drawn ----------------------------------------- */

  it('states the headline of a shut row and folds the rest of it away', async () => {
    const wrapper = await render([filed({
      summary: 'Prepared 1 evidence-linked finding draft (1 critical).',
      highlights: [{ severity: 'warning', label: 'Invoices over PO', detail: '3 of 52 rows', artifact_ref: '' }],
    })])

    expect(wrapper.find('.row').classes()).toContain('shut')
    expect(wrapper.text()).toContain('Finding drafts prepared')
    expect(wrapper.find('.dsc').exists()).toBe(false)
    expect(wrapper.find('.hl').exists()).toBe(false)

    await openRow(wrapper)
    expect(wrapper.find('.row').classes()).not.toContain('shut')
    expect(wrapper.find('.dsc').text()).toContain('evidence-linked finding draft')
    expect(wrapper.findAll('.hl li')).toHaveLength(1)
  })

  it('counts what the fold hides, keeping the colour of what it stands for', async () => {
    const wrapper = await render([filed({
      stats: [
        { label: 'critical', value: 0, severity: 'error' },
        { label: 'high', value: 8, severity: 'warning' },
        { label: 'medium', value: 3, severity: 'info' },
      ],
      highlights: [
        { severity: 'warning', label: 'One', detail: 'a', artifact_ref: '' },
        { severity: 'error', label: 'Two', detail: 'b', artifact_ref: '' },
      ],
    })])

    const chips = wrapper.findAll('.sig')
    // The most severe non-zero tier leads; zero critical says nothing here.
    expect(chips[0].text()).toBe('8high')
    expect(chips[0].attributes('data-severity')).toBe('warning')
    // Two highlights, one of them severe: the pair is reported at its worst.
    expect(chips[1].text()).toBe('2flags')
    expect(chips[1].attributes('data-severity')).toBe('error')
  })

  it('leads the chip with the most severe tier, not the fullest one', async () => {
    // The shut row and the open one described the same matrix differently: a
    // chip reading "12 medium" over a strip led by 5 high, with the bigger
    // number attached to the milder finding.
    const wrapper = await render([filed({
      stats: [
        { label: 'critical', value: 0, severity: 'error' },
        { label: 'high', value: 5, severity: 'warning' },
        { label: 'medium', value: 12, severity: 'info' },
        { label: 'low', value: 5, severity: 'info' },
      ],
    })])

    expect(wrapper.find('.sig').text()).toBe('5high')
  })

  it('leaves a row with nothing hidden unchipped, so a chip means something', async () => {
    const wrapper = await render([filed({
      stats: [],
      highlights: [],
      history: history({
        attempts: [{ run_id: 'r1', run_status: 'completed', at: '2026-08-14T11:05:00+00:00', elapsed_ms: 60_000 }],
        measured_attempts: 1,
      }),
    })])

    expect(wrapper.find('.sig').exists()).toBe(false)
  })

  it('keeps an open point on the row even while the row is shut', async () => {
    // The debt is the one thing on a row that asks something of the reader.
    // Folding it away would be folding away the point of the page.
    const wrapper = await render([filed({ open_points: [point()] })])

    expect(wrapper.find('.row').classes()).toContain('shut')
    expect(wrapper.find('.open').text()).toContain('41 of 60 conclusions')
  })

  it('opens a row clicked anywhere, and leaves the pill to its own link', async () => {
    const wrapper = await render([filed()])

    await wrapper.find('.row .say').trigger('click')
    expect(wrapper.find('.dsc').exists()).toBe(true)

    // The pill navigates; it must not also shut the row under the reader. The
    // stubbed link is a real anchor, so the click is defused before jsdom
    // tries to follow it — the row handler still sees it bubble.
    const pill = wrapper.find('.row .card')
    pill.element.addEventListener('click', event => event.preventDefault())
    await pill.trigger('click')
    expect(wrapper.find('.dsc').exists()).toBe(true)
  })

  it('opens every row under Full, and remembers that for the next visit', async () => {
    const wrapper = await render([filed(), filed({ id: 'report:m2', capability: 'report.working_draft' })])

    await wrapper.findAll('.dens button')[1].trigger('click')
    expect(wrapper.findAll('.row.shut')).toHaveLength(0)
    expect(wrapper.findAll('.dsc')).toHaveLength(2)
    // Nothing is shut, so nothing offers to be opened.
    expect(wrapper.find('.chev').exists()).toBe(false)
    expect(window.localStorage.getItem('aw.record.density')).toBe('full')

    const second = await render([filed()])
    expect(second.find('.row.shut').exists()).toBe(false)
  })

  /* --- a stage whose result is a distribution ----------------------------- */

  it('states a distribution as a tally rather than as another paragraph', async () => {
    // A matrix is read as "1 critical, 8 high" before any single row is, and a
    // sentence cannot say that at a glance.
    const wrapper = await render([filed({
      stats: [
        { label: 'critical', value: 1, severity: 'error' },
        { label: 'high', value: 8, severity: 'warning' },
      ],
    })])

    await openRow(wrapper)
    const chips = wrapper.findAll('.tally li')
    expect(chips).toHaveLength(2)
    expect(chips[0].text()).toBe('1critical')
    expect(chips[0].attributes('data-severity')).toBe('error')
  })

  it('leaves a severe tier at zero uncoloured, since nothing is wrong there', async () => {
    const wrapper = await render([filed({ stats: [{ label: 'critical', value: 0, severity: 'error' }] })])

    await openRow(wrapper)
    expect(wrapper.find('.tally li').attributes('data-zero')).toBe('1')
  })

  it('draws no tally for a stage whose result is not a distribution', async () => {
    const wrapper = await render([filed()])

    expect(wrapper.find('.tally').exists()).toBe(false)
  })

  /* --- the run in flight -------------------------------------------------- */

  it('reports the run in progress instead of proposing work already under way', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([filed(), owed()], {
      next: { kind: 'stage', ...owed() },
    })

    const brief = wrapper.find('.brief')
    expect(brief.classes()).toContain('live')
    // The band takes the next step's place rather than sitting beside it.
    expect(wrapper.findAll('.brief')).toHaveLength(1)
    expect(brief.text()).toContain('Write the report from the findings')
    expect(brief.text()).toContain('Running')
    wrapper.unmount()
  })

  it('marks the pending row the run is writing, and takes its Run button away', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([filed(), owed()])

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
      { capability: 'report.working_draft', status: 'queued' },
    ])
    const wrapper = await render([filed(), owed()])

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBe('queued')
    expect(ghost.text()).toContain('Scheduled by the run in progress')
    expect(ghost.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('leaves a pending row the run never scheduled exactly as it was', async () => {
    // The run is working on a stage this ledger has no row for, so the owed row
    // it did not schedule must not pick up any of the run's markings.
    liveRun([{ capability: 'fieldwork.executed', status: 'running' }])
    const wrapper = await render([filed(), owed()])

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBeUndefined()
    expect(ghost.text()).toContain('not yet')
    wrapper.unmount()
  })

  it('drops the call-to-action highlight while a run is deciding the answer', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([filed(), owed()])

    expect(wrapper.find('.row.ghost').classes()).not.toContain('lead')
    wrapper.unmount()
  })

  it('says a filed work product is being produced again rather than looking settled', async () => {
    liveRun([{ capability: 'findings.drafted', status: 'running' }])
    const wrapper = await render([filed()])

    expect(wrapper.find('.again').text()).toContain('Running again')
    wrapper.unmount()
  })

  it('ignores a stage the run has already finished, whose commit filed the row', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'succeeded' }])
    const wrapper = await render([filed(), owed()])

    expect(wrapper.find('.brief.live').exists()).toBe(true)
    expect(wrapper.find('.row.ghost').attributes('data-live')).toBeUndefined()
    wrapper.unmount()
  })

  it('still names the run when it is waiting on a person, and asks for a reply', async () => {
    liveRun([{ capability: 'report.working_draft', status: 'running' }], { status: 'awaiting_approval' })
    const wrapper = await render([filed(), owed()])

    const brief = wrapper.find('.brief')
    expect(brief.attributes('data-wait')).toBe('1')
    expect(brief.text()).toContain('Waiting for your approval')
    wrapper.unmount()
  })

  it('has a row for the stage the run is executing without synthesizing one', async () => {
    // A running stage used to be covered by neither half of the ledger — no
    // milestone yet, and its work product could already exist enough to stop
    // being owed — so the client built the row itself out of a vocabulary the
    // server published for the purpose. The spine draws every stage, so the
    // row is already there and the overlay only has to mark it.
    liveRun([{ capability: 'fieldwork.executed', status: 'running', title: 'Fieldwork execution' }])
    const wrapper = await render([
      filed(),
      owed({
        id: 'stage:fieldwork.executed',
        capability: 'fieldwork.executed',
        headline: 'Run the tests against the data and documents',
        filed: { label: 'Fieldwork results', destination: 'doc-tests', unit: '', unit_plural: '', count: null },
      }),
    ])

    const ghost = wrapper.find('.row.ghost')
    expect(ghost.attributes('data-live')).toBe('running')
    expect(ghost.text()).toContain('Fieldwork results')
    // The sentence it carried while owed, so it does not rename itself at the
    // moment it starts.
    expect(ghost.text()).toContain('Run the tests against the data and documents')
    expect(ghost.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('leaves a machine step the record has no row for to the band alone', async () => {
    // A ledger row for a capability with no work product is a row about
    // nothing. `data.joins_ready` is not on the spine, so it gets none.
    liveRun([{ capability: 'data.joins_ready', status: 'running', title: 'Joins ready' }])
    const wrapper = await render([filed()])

    expect(wrapper.findAll('.row')).toHaveLength(1)
    expect(wrapper.find('.row').attributes('data-live')).toBeUndefined()
    expect(wrapper.find('.brief.live').exists()).toBe(true)
    wrapper.unmount()
  })

  it('marks a running stage in its own place rather than moving it', async () => {
    // The ledger's order is the plan's, and a stage starting does not change
    // where it belongs in the plan.
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([filed(), owed()])

    const rows = wrapper.findAll('.row')
    expect(rows.map(row => row.find('.mt b').text()))
      .toEqual(['Findings register', 'Report'])
    expect(rows[1].attributes('data-live')).toBe('running')
    wrapper.unmount()
  })

  it('states a held stage being produced again without making it look unwritten', async () => {
    liveRun([{ capability: 'findings.drafted', status: 'running' }])
    const wrapper = await render([filed(), owed()])

    const row = wrapper.findAll('.row')[0]
    expect(row.classes()).not.toContain('ghost')
    expect(row.text()).toContain('Running again')
    // The count it holds is not replaced by the fact that it is running.
    expect(row.find('.mt em').text()).toBe('being written')
    wrapper.unmount()
  })

  it('reports a run whose route has not resolved yet, which has no stages at all', async () => {
    agentActive.value = true
    agentState.run = {
      id: 'live', status: 'interpreting', created: '2026-08-15T12:20:00+00:00',
      started: null, activity: null, workflow: null,
    }
    const wrapper = await render([filed(), owed()])

    expect(wrapper.find('.brief.live').text()).toContain('Working out what to run')
    expect(wrapper.find('.row.ghost').attributes('data-live')).toBeUndefined()
    wrapper.unmount()
  })

  /**
   * The chain is the one destination the ledger cannot draw as a row: it files
   * nothing, so there is no work product for a row to be about. With the audit
   * file's rail gone, this link is the only way to reach it.
   */
  it('offers the chain from the bar, because no row can carry it', async () => {
    const wrapper = await render([filed()])

    const chain = wrapper.find('a.chain')
    expect(chain.exists()).toBe(true)
    expect(chain.text()).toContain('Chain')
    expect(chain.attributes('href')).toBe('/chain')
    // It is a link, not one of the bar's buttons: it navigates rather than
    // acting on the record, so it must survive a middle-click.
    expect(chain.element.tagName).toBe('A')
    wrapper.unmount()
  })

  /**
   * Sources is the head of the chain and the only row that opens two things:
   * there is no single Sources page, so the doors go to the two catalogues the
   * engagement actually keeps.
   */
  it('draws both doors on a row that opens more than one thing', async () => {
    const wrapper = await render([filed({
      id: 'stage:sources.imported',
      capability: 'sources.imported',
      filed: { label: 'Sources', destination: '', unit: '', unit_plural: '', count: null },
      links: [
        { label: 'Documents', destination: 'documents', count: 8, kind: 'artifact' },
        { label: 'Tables', destination: 'data', count: 6, kind: 'artifact' },
      ],
    })])

    const doors = wrapper.findAll('.door')
    expect(doors.map(door => door.text())).toEqual(['Documents8', 'Tables6'])
    expect(doors.map(door => door.attributes('href'))).toEqual(['/documents', '/data'])
    // The card itself opens nothing — there is no combined Sources page.
    expect(wrapper.find('.made .card').attributes('href')).toBeUndefined()
    wrapper.unmount()
  })

  /**
   * A query files nothing, so the record must not draw it in the teal it uses
   * for work the engagement holds — that would be a claim it cannot support.
   */
  it('marks a tool door as a tool rather than as something filed', async () => {
    const wrapper = await render([filed({
      id: 'stage:analysis.executed',
      capability: 'analysis.executed',
      filed: { label: 'Analysis library', destination: 'analysis', unit: 'analysis', unit_plural: 'analyses', count: 24 },
      links: [{ label: 'Query', destination: 'query', count: null, kind: 'tool' }],
    })])

    const door = wrapper.find('.door')
    expect(door.attributes('data-kind')).toBe('tool')
    expect(door.text()).toBe('Query')
    // No count: a tool has no population to state.
    expect(door.find('b').exists()).toBe(false)
    wrapper.unmount()
  })

  /**
   * The assistant cannot import. A row whose action is `import` has to hand the
   * shell its own dialog rather than send a command nothing would answer — the
   * record used to have no way to say that, and a new engagement's first
   * suggestion was to plan an audit of nothing.
   */
  it('asks the shell to import instead of sending the assistant a command', async () => {
    const wrapper = await render([owed({
      id: 'stage:sources.imported',
      capability: 'sources.imported',
      headline: 'Bring in the audit file',
      start: null,
      action: 'import',
      filed: { label: 'Sources', destination: '', unit: '', unit_plural: '', count: null },
    })])

    await wrapper.find('.took button').trigger('click')

    expect(wrapper.emitted('import-requested')).toHaveLength(1)
    expect(send).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
