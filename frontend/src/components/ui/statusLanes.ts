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
  /**
   * What `value` is out of, where the caption is an "of N …" sentence. The
   * resting bar reads `24 / 30` from these two; the caption's words are the
   * part the expanded card exists to show. Left unset where the lane counts
   * no population — the bar then falls back to the sentence.
   */
  total?: string
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

/**
 * One narrowing the page offers, as a menu row rather than a lane chip.
 *
 * The lane chips show a distribution and happen to filter. This is the whole
 * vocabulary, including the axes a lane cannot carry without muddling itself —
 * "effective" and "set by the agent" are both true of the same test, so they
 * belong in separate groups rather than one chip row.
 */
export interface StatusFilterOption {
  key: string
  /** Names the subset. The count travels beside it, not baked into the text. */
  label: string
  value: number
  tone: Tone
}

export interface StatusFilterGroup {
  key: string
  label: string
  options: StatusFilterOption[]
}

/**
 * A chip in the review bar.
 *
 * The bar is the page's filter row, so a chip names a filter rather than
 * inventing a count of its own: the label and the number both come from the
 * matching `StatusFilterOption`, which is derived beside the lanes from one
 * tally. A view supplies only which filters to promote, in what order, and in
 * what tone — the six questions worth a permanent row on that page.
 *
 * `agent` is a tone the lane model has no use for: a lane is a proportion of
 * work and the agent is not a stage of it. It exists here because "the agent
 * decided this and nobody has read it" is the one narrowing every fieldwork
 * page offers, and it is neither good news nor bad.
 */
export type ChipTone = Tone | 'agent'

export interface ReviewChip {
  filter: string
  tone: ChipTone
  /**
   * What the chip calls the subset, where the bar should not read as the menu.
   * The menu names an axis member ("With exceptions"); the bar states a fact
   * about the page ("Exceptions open"). Omitted, the option's own label serves.
   */
  label?: string
}

export interface StatusModel {
  lanes: StatusLane[]
  disclosures: StatusDisclosure[]
  /**
   * Every filter the page offers, grouped by axis. Derived beside the lanes
   * from the same tally, so the menu and the chips can never disagree, and
   * emitted into the same active set the chips drive.
   */
  filters?: StatusFilterGroup[]
}

export function portion(part: number, whole: number): number {
  return whole > 0 ? (part / whole) * 100 : 0
}

/**
 * Every action the lanes want taken, in lane order.
 *
 * The page header renders these beside its own buttons. A button is already a
 * complete sentence — "Draft findings (8)" says what is outstanding and what
 * closes it — so it does not need a row of its own under the status, and
 * putting it on top means the one urgent control is not the last thing read.
 */
export function statusActions(model: StatusModel): StatusAction[] {
  return model.lanes.flatMap(lane => lane.actions)
}

/**
 * Add or drop one narrowing, holding at most one per axis.
 *
 * Narrowings compose: "exceptions nobody has concluded on" is two questions
 * about the same row and neither answers the other, so the active set is a
 * list rather than a single key. Within one axis they cannot compose — a test
 * is not both effective and ineffective — so picking a second option from the
 * same group replaces the first rather than emptying the list.
 *
 * A page with no groups declared has one axis by definition, so every pick
 * replaces: that is what the pages that never had a second axis already did.
 */
export function toggleFilter(
  active: readonly string[], key: string, groups?: StatusFilterGroup[],
): string[] {
  if (active.includes(key)) return active.filter(value => value !== key)
  const group = groups?.find(item => item.options.some(option => option.key === key))
  if (!group) return [key]
  const siblings = new Set(group.options.map(option => option.key))
  return [...active.filter(value => !siblings.has(value)), key]
}
