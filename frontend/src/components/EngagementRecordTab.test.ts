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
const openPanel = vi.fn()
const agentInit = vi.fn()
vi.mock('../composables/useAgentRun', () => ({
  useAgentRun: () => ({
    state: agentState,
    isActive: agentActive,
    init: agentInit,
    openPanel,
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
    phase: 'fieldwork',
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
    phase: 'writeup',
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
    phase: 'documents',
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

/** The default spine, phase by phase, exactly as the server groups it. */
const PLAN: Array<[phase: string, capability: string, label: string]> = [
  ['sources', 'sources.imported', 'Sources'],
  ['sources', 'analysis.executed', 'Analysis library'],
  ['documents', 'documents.analysis_generated', 'Document analyses'],
  ['planning', 'planning.apm_ready', 'Audit planning memorandum'],
  ['planning', 'planning.rcm_ready', 'Risk and control matrix'],
  ['planning', 'tests.specified', 'Test programme'],
  ['fieldwork', 'doc_tests.executed', 'Document test results'],
  ['fieldwork', 'fieldwork.executed', 'Fieldwork results'],
  ['fieldwork', 'results.rolled_up', 'Control conclusions'],
  ['fieldwork', 'findings.drafted', 'Findings register'],
  ['writeup', 'report.working_draft', 'Report'],
  ['writeup', 'audit.verified', 'Verification'],
]

/** What the demo engagement holds: the sources read, the documents analysed. */
const HELD = ['sources.imported', 'analysis.executed', 'documents.analysis_generated']

/**
 * The whole plan as one payload — twelve stages across five phases, the named
 * ones held and the first stage after them the lead. The phase sections are
 * about the shape of the whole record, so they are the one thing that cannot
 * be tested on a two-row fixture.
 */
function spine(held: string[]): EngagementStage[] {
  let leadTaken = false
  return PLAN.map(([phase, capability, label]) => {
    const isHeld = held.includes(capability)
    const lead = !isHeld && !leadTaken
    if (lead) leadTaken = true
    return (isHeld ? filed : owed)({
      id: `stage:${capability}`,
      capability,
      phase,
      held: isHeld,
      runnable: lead,
      headline: `Do ${label}`,
      blocked_reason: isHeld || lead ? '' : 'Waits for the memorandum.',
      start: lead
        ? { prompt: `Run ${capability}.`, outcomes: [capability], alternates: [] }
        : null,
      filed: { label, destination: '', unit: '', unit_plural: '', count: null },
      history: isHeld ? history({ elapsed_ms: 60_000 }) : null,
    })
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

/** The phases the server ships, in the order it ships them. */
const PHASE_TITLES: Array<[string, string]> = [
  ['sources', 'Understand the data'],
  ['documents', 'Read the documents'],
  ['planning', 'Plan the engagement'],
  ['fieldwork', 'Do the fieldwork'],
  ['writeup', 'Write it up'],
]

function payload(
  stages: EngagementStage[],
  extra: Partial<EngagementRecordPayload> = {},
): EngagementRecordPayload {
  const settled = stages.filter(item => item.history)
  const present = new Set(stages.map(item => item.phase))
  return {
    stages,
    // Exactly what the server does: plan order, and only the phases this
    // engagement has stages for.
    phases: PHASE_TITLES
      .filter(([id]) => present.has(id))
      .map(([id, title]) => ({ id, title, summary: '' })),
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

/**
 * Mount the tab against one payload.
 *
 * `phases` defaults to open, because the record only ever draws the phase
 * being worked and every test below this one is about a row rather than about
 * which phases are folded. The tests that *are* about the phases pass
 * `{ phases: 'as drawn' }` and read what the component chose.
 */
async function render(
  stages: EngagementStage[],
  extra: Partial<EngagementRecordPayload> = {},
  { phases = 'open' }: { phases?: 'open' | 'as drawn' } = {},
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
  if (phases === 'open') {
    for (const header of wrapper.findAll('.phead')) {
      if (header.attributes('aria-expanded') !== 'true') await header.trigger('click')
    }
  }
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
    openPanel.mockReset(); agentInit.mockReset()
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

    // The row carries the bare count; the spelled unit is what it is named.
    expect(wrapper.find('.ct').attributes('title')).toBe('28 analyses')
    expect(wrapper.find('.ct').text()).toBe('28')
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

    expect(wrapper.find('.stamp').text()).toContain('—')
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

    expect(wrapper.find('.wp').attributes('href')).toBe('/findings')
  })

  it('does not link a destination this build does not know', async () => {
    const wrapper = await render([filed({
      filed: { label: 'Something new', destination: 'not-a-surface', unit: '', unit_plural: '', count: null },
    })])

    const label = wrapper.find('.wp')
    expect(label.text()).toBe('Something new')
    expect(label.attributes('href')).toBeUndefined()
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
    expect(wrapper.find('.ct').exists()).toBe(false)
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
    expect(wrapper.find('.ct').text()).toBe('35')
    expect(wrapper.find('.stamp').exists()).toBe(false)
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

    expect(wrapper.find('.ct').text()).toBe('35')
    expect(wrapper.find('.left').text()).toContain('2 eligible observations need finding drafts')
  })

  // ---------------------------------------------------------------- forward
  it('names the single most blocking thing under the record', async () => {
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

  it('draws a stage that has not run as owed, in the phase that owes it', async () => {
    // The now line said where the ledger crossed from held into owed. The
    // phase states say it instead, and say which phase it happened in.
    const wrapper = await render([filed(), owed()])

    expect(wrapper.find('.row.ghost .wp').text()).toBe('Report')
    // Nothing is filed, so there is no count to state.
    expect(wrapper.find('.row.ghost .ct').exists()).toBe(false)
    expect(wrapper.findAll('.phase').map(phase => phase.attributes('data-state')))
      .toEqual(['done', 'current'])
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
    // `Waits for the memorandum.` was the whole content of nine rows at once.
    // Beside the row it is one phrase, and the row says what it is instead.
    expect(ghost.find('.dep').text()).toBe('after the memorandum')
    expect(ghost.text()).not.toContain('Waits for')
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

    expect(openPanel).toHaveBeenCalled()
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

  it('says nothing beside the record when there is nothing to do', async () => {
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

  it('opens a row clicked anywhere, and leaves the label to its own link', async () => {
    const wrapper = await render([filed()])

    await wrapper.find('.row .name').trigger('click')
    expect(wrapper.find('.dsc').exists()).toBe(true)

    // The label navigates; it must not also shut the row under the reader. The
    // stubbed link is a real anchor, so the click is defused before jsdom
    // tries to follow it — the row handler still sees it bubble.
    const label = wrapper.find('.row .wp')
    label.element.addEventListener('click', event => event.preventDefault())
    await label.trigger('click')
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
    expect(ghost.find('.again').text()).toContain('The assistant is working on it now.')
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
    expect(ghost.find('.again').exists()).toBe(false)
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
    expect(rows.map(row => row.find('.wp').text()))
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
    expect(row.find('.ct').text()).toBe('35')
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
    // The label itself opens nothing — there is no combined Sources page.
    expect(wrapper.find('.name .wp').attributes('href')).toBeUndefined()
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

    await wrapper.find('.act button').trigger('click')

    expect(wrapper.emitted('import-requested')).toHaveLength(1)
    expect(send).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  /* --- what one row says --------------------------------------------------- */

  it('states when a stage settled and what it took, in one reading', async () => {
    // Two columns of the old ledger — a Time that read "—" on nine rows of
    // twelve, and a Took beside it — are one phrase beside the row instead.
    const wrapper = await render([filed()])

    expect(wrapper.find('.stamp').text()).toContain('· 2m')
  })

  it('draws the Run button only on the lead stage, never on the ones behind it', async () => {
    // A tail of buttons is a menu, not a next step. The stage behind the lead
    // is reachable — its phase is open and it is one row down — but it is not
    // asked for.
    const wrapper = await render([
      filed(),
      owed({ id: 'a', capability: 'planning.apm_ready' }),
      owed({ id: 'b', capability: 'planning.rcm_ready' }),
    ])

    const actions = wrapper.findAll('.act button').filter(button => !button.classes('chev'))
    expect(actions).toHaveLength(1)
    expect(wrapper.findAll('.row')[1].find('.act button').exists()).toBe(true)
    expect(wrapper.findAll('.row')[2].find('.act button').exists()).toBe(false)
  })

  it('offers to import more on a Sources row the engagement already holds', async () => {
    // The one row whose work is never finished: more of the audit file can
    // always arrive, and no assistant command brings it.
    const wrapper = await render([filed({
      id: 'stage:sources.imported',
      capability: 'sources.imported',
      phase: 'sources',
      action: 'import',
      filed: { label: 'Sources', destination: '', unit: '', unit_plural: '', count: null },
    })])

    await wrapper.find('.act button').trigger('click')

    expect(wrapper.emitted('import-requested')).toHaveLength(1)
    expect(send).not.toHaveBeenCalled()
  })

  it('draws no band for a stage next step, because its phase header says it', async () => {
    const wrapper = await render([filed(), owed()], { next: { kind: 'stage', ...owed() } })

    expect(wrapper.find('.brief').exists()).toBe(false)
    // The phase holding it carries the badge and the sentence instead.
    expect(wrapper.find('.phase[data-state="current"] .pel').text())
      .toBe('Nothing is blocking it.')
  })

  it('keeps a review debt on screen while a run is in flight', async () => {
    // Work under way replaces proposed work, because proposing what is already
    // running is stale. It does not replace a debt: only a person can read what
    // the assistant decided, and the run is not doing it for them.
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render([filed(), owed()], {
      next: { kind: 'open_point', ...point() },
    })

    expect(wrapper.find('.brief.live').exists()).toBe(true)
    expect(wrapper.find('.brief[data-kind="open_point"]').text()).toContain('41 of 60 conclusions')
    wrapper.unmount()
  })

  /* --- the whole plan, as one strip ---------------------------------------- */
  /*
   * The phases fold, so four of the five say nothing about their size. The
   * strip is where the whole engagement stays visible: one segment per stage,
   * each phase as wide as the number of stages it holds.
   */

  it('draws one segment per stage, each phase as wide as the stages it holds', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.findAll('.seg')).toHaveLength(12)
    expect(wrapper.findAll('.sphase').map(phase =>
      phase.findAll('.seg').map(segment => segment.attributes('data-state')).join(','),
    )).toEqual([
      'held,held', 'held', 'lead,owed,owed', 'owed,owed,owed,owed', 'owed,owed',
    ])
    expect(wrapper.find('.segs').attributes('style')).toContain('2fr 1fr 3fr 4fr 2fr')
  })

  it('names the phase being worked under the stretch of strip that is its', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const labels = wrapper.findAll('.slabels span')

    expect(labels.map(label => label.text())).toEqual([
      'Understand the data', 'Read the documents', 'Plan the engagement',
      'Do the fieldwork', 'Write it up',
    ])
    expect(labels.map(label => label.attributes('data-current')))
      .toEqual([undefined, undefined, 'true', undefined, undefined])
  })

  it('colours a stage under way rather than leaving it grey in the plan', async () => {
    liveRun([{ capability: 'planning.apm_ready', status: 'running' }])
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.findAll('.sphase')[2].findAll('.seg').map(s => s.attributes('data-state')))
      .toEqual(['live', 'owed', 'owed'])
    wrapper.unmount()
  })

  it('takes a filed stage out of the teal while it is being written again', async () => {
    liveRun([{ capability: 'sources.imported', status: 'running' }])
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    // Teal is what the engagement holds; work happening now is neither that
    // nor what it owes, so the segment leaves the teal while it runs.
    expect(wrapper.findAll('.sphase')[0].findAll('.seg').map(s => s.attributes('data-state')))
      .toEqual(['live', 'held'])
    wrapper.unmount()
  })

  it('draws the strip and nothing that says the same thing in words', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    const strip = wrapper.find('.strip')
    expect(strip.exists()).toBe(true)
    // The phase names, and nothing else: no count, no clock, no run tally.
    expect(strip.text()).not.toMatch(/\d/)
    expect(strip.findAll('.slabels span')).toHaveLength(5)
  })

  it('shows how far through the run it is, as the number its step count is', async () => {
    liveRun([
      { capability: 'planning.apm_ready', status: 'succeeded' },
      { capability: 'planning.rcm_ready', status: 'succeeded' },
      { capability: 'tests.specified', status: 'running' },
      { capability: 'doc_tests.executed', status: 'queued' },
    ])
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.find('.brief.live').text()).toContain('step 3 of 4')
    expect(wrapper.find('.pbar i').attributes('style')).toBe('width: 50%;')
    wrapper.unmount()
  })

  it('draws no bar for a run of one stage, where it could only be empty or full', async () => {
    liveRun([{ capability: 'planning.apm_ready', status: 'running' }])
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.find('.brief.live').exists()).toBe(true)
    expect(wrapper.find('.pbar').exists()).toBe(false)
    wrapper.unmount()
  })

  it('leaves the footer what it cost, and the strip what the engagement holds', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const footer = wrapper.find('.summary')

    expect(footer.text()).toContain('55m of assistant time')
    expect(footer.text()).toContain('32 runs')
    expect(footer.text()).not.toContain('work products')
  })

  /* --- the phases the record is drawn in ----------------------------------- */
  /*
   * Twelve rows of which nine said "not yet" gave the work outstanding three
   * quarters of the screen. The phases are what that becomes: the finished
   * work folds to a line, the phase being worked is open, and the phases after
   * it are drawn as the plan they are.
   */

  it('draws one section per phase, in plan order', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.findAll('.phase .pt').map(title => title.text())).toEqual([
      'Understand the data', 'Read the documents', 'Plan the engagement',
      'Do the fieldwork', 'Write it up',
    ])
    expect(wrapper.findAll('.phase').map(phase => phase.attributes('data-state')))
      .toEqual(['done', 'done', 'current', 'later', 'later'])
  })

  it('folds away only the phases whose turn has not come', async () => {
    // What is done is what the engagement holds, and the reader came to check
    // it. What cannot start yet has nothing to read but its own header.
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.findAll('.phead').map(header => header.attributes('aria-expanded')))
      .toEqual(['true', 'true', 'true', 'false', 'false'])
    expect(wrapper.findAll('.row .wp').map(label => label.text())).toEqual([
      'Sources', 'Analysis library', 'Document analyses',
      'Audit planning memorandum', 'Risk and control matrix', 'Test programme',
    ])
  })

  it('badges the phase being worked, and it holds the only call to action', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const current = wrapper.findAll('.phase')[2]

    expect(wrapper.findAll('.pnext')).toHaveLength(1)
    expect(current.find('.pnext').text()).toBe('Next')
    expect(current.find('.pel').text()).toBe('Nothing is blocking it.')
    expect(wrapper.findAll('.row.ghost.lead')).toHaveLength(1)
    expect(current.findAll('.row.ghost.lead')).toHaveLength(1)
  })

  it('counts a phase as a fraction, and one that cannot start in stages', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const phases = wrapper.findAll('.phase')

    expect(phases[0].find('.pst').text()).toBe('2/2')
    // `1/1` is a fraction with nothing to compare, on a header read down a
    // column of five.
    expect(phases[1].find('.pst').exists()).toBe(false)
    expect(phases[2].find('.pst').text()).toBe('0/3')
    expect(phases[3].find('.pst').text()).toBe('4 stages · after planning')
    expect(phases[4].find('.pst').text()).toBe('2 stages · after fieldwork')
  })

  it('says what a folded phase covers rather than hiding it', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const later = wrapper.findAll('.phase')[3]

    expect(later.find('.pnames').text()).toBe(
      'Document test results · Fieldwork results · Control conclusions · Findings register',
    )
    expect(later.findAll('.row')).toHaveLength(0)
  })

  it('opens a phase whose turn has not come when its header is clicked', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const header = wrapper.findAll('.phead')[3]
    expect(header.attributes('aria-expanded')).toBe('false')

    await header.trigger('click')

    expect(header.attributes('aria-expanded')).toBe('true')
    expect(wrapper.findAll('.phase')[3].findAll('.row')).toHaveLength(4)
  })

  it('folds a finished phase away when its header is clicked', async () => {
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })
    const header = wrapper.findAll('.phead')[0]
    expect(header.attributes('aria-expanded')).toBe('true')

    await header.trigger('click')

    expect(header.attributes('aria-expanded')).toBe('false')
    expect(wrapper.findAll('.phase')[0].findAll('.row')).toHaveLength(0)
  })

  it('puts the phase with the run in flight forward, not the one that is next', async () => {
    // Nothing is the lead stage while a run is in flight, so the phase being
    // worked is the one the run is in — even where it is the last of the five.
    liveRun([{ capability: 'report.working_draft', status: 'running' }])
    const wrapper = await render(spine(HELD), {}, { phases: 'as drawn' })

    expect(wrapper.findAll('.phase').map(phase => phase.attributes('data-state')))
      .toEqual(['done', 'done', 'later', 'later', 'current'])
    expect(wrapper.findAll('.phase')[4].find('.pnext').exists()).toBe(true)
  })
})
