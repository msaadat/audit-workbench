import type { WorkspaceDestination } from '../../composables/useWorkspaceNavigation'
import type { EngagementOpenPoint, EngagementStage } from '../../types'
import type { UiState } from '../ui/UiStateIcon.vue'

export type RecordLayout = 'list' | 'board' | 'phases' | 'overview'

export type RecordPhaseState = 'done' | 'current' | 'later'

export interface RecordFactPart {
  figure: string
  text: string
  severity: string
}

export interface RecordFact {
  kind: 'dep' | 'count' | 'door' | 'owed' | 'text' | 'tool'
  figure: string
  text: string
  title?: string
  to?: WorkspaceDestination | null
  parts?: RecordFactPart[]
}

export interface RecordStageItem {
  stage: EngagementStage
  stepLabel: string
  state: UiState
  live: '' | 'queued' | 'running'
  liveElapsed: string
  liveActivity: string
  isLead: boolean
  destination: WorkspaceDestination | null
  facts: RecordFact[]
  primaryStat: string
  attentionReason: string
}

export interface RecordPhaseGroup {
  id: string
  index: number
  title: string
  state: RecordPhaseState
  statusLabel: string
  held: number
  total: number
  fill: number
  live: boolean
  openPoints: number
  headline: string
  items: RecordStageItem[]
}

/**
 * The run in flight, said once. `fraction` is the one progress figure the
 * record shows: the unit count where the run reports one (4 of 32 controls),
 * else the stage count, and null where neither says anything.
 */
export interface RecordRunNow {
  title: string
  status: string
  detail: string
  fraction: number | null
  waiting: boolean
}

export interface RecordStartRequest {
  prompt: string
  outcomes: string[]
}

export type RecordOpenPointHandler = (point: EngagementOpenPoint) => void
