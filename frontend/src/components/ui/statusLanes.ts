/**
 * The shape of a page's status bar, independent of which page it describes.
 *
 * Every fieldwork surface answers the same three questions in its own
 * vocabulary — has the work run, does it support a conclusion, has the
 * conclusion been written up — so the rendering is shared and only the
 * derivation is per page. A page contributes a `StatusModel`; `UiStatusLanes`
 * draws it and reports back which count was clicked.
 *
 * Filter keys are plain strings here. Each page narrows them to its own union
 * in its derivation module, where the predicates that consume them live.
 */

/** Idle: nothing to say yet. Done: nothing outstanding. The rest owe work. */
export type LaneState = 'idle' | 'gap' | 'alarm' | 'done'
export type Tone = 'neutral' | 'ok' | 'warn' | 'bad'

/** A count that doubles as a filter over the list below the bar. */
export interface StatusChip {
  key: string
  label: string
  tone: Tone
}

export interface StatusAction {
  key: string
  label: string
  tone: 'primary' | 'ghost' | 'warn'
  /** The rows or tests the action is scoped to, where it is scoped to any. */
  ids?: string[]
  /** Held back when no assistant is available, unlike deterministic work. */
  needsAgent?: boolean
}

export interface StatusLane {
  key: string
  label: string
  state: LaneState
  /** The number the lane leads with, and the sentence it completes. */
  value: string
  caption: string
  /** Meter fill. Portions are percentages of the whole and need not total 100. */
  segments: Array<{ tone: Tone; portion: number }>
  chips: StatusChip[]
  actions: StatusAction[]
  /** Stands in for the actions once the lane has nothing outstanding. */
  rest: string
}

/**
 * What qualifies the lanes rather than blocking them — who decided, what the
 * evidence cannot support, what nobody has signed. Never a badge on a lane:
 * these do not gate completion, and dressing them as gates would make a
 * finished engagement look unfinished.
 */
export interface StatusDisclosure {
  key: string
  /** Short word in the leading tag: "Agent", "Limit", "Sign-off". */
  mark: string
  tone: 'agent' | 'warn' | 'muted'
  message: string
  filter: string
  /**
   * What can be done about the disclosure, where anything can be. A lane's
   * action closes a gap; this one settles a qualification the page is only
   * obliged to state, so it stays a quiet control on the strip rather than
   * being promoted into a lane it does not gate.
   */
  action?: StatusAction
}

export interface StatusModel {
  lanes: StatusLane[]
  disclosures: StatusDisclosure[]
}

export function portion(part: number, whole: number): number {
  return whole > 0 ? (part / whole) * 100 : 0
}
