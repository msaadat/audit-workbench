import type { InjectionKey, Ref } from 'vue'

import type { DashboardPhase, DashboardSection, WorkspaceSummary } from '../types'

/**
 * What the workspace shell owns and every surface needs: the loaded workspace,
 * engagement phase status, and the two shell actions surfaces trigger. Provided
 * by `WorkspaceView`, which only renders its surfaces once the workspace is
 * loaded — so `workspace` is never null below the shell.
 */
export interface WorkspaceContext {
  workspace: Ref<WorkspaceSummary>
  /** Phase status in backend order, for rails and progress summaries. */
  phases: Ref<DashboardPhase[]>
  /** The same phases keyed by id, for per-entry status markers. */
  phaseById: Ref<Partial<Record<DashboardPhase['id'], DashboardPhase>>>
  /** Per-rail-section state, for entries whose phase is broader than the tab
   *  they open — a rail badge should answer for the tab it sits on. */
  sectionById: Ref<Record<string, DashboardSection>>
  /** Reload the workspace summary and phase status together. */
  reload: () => Promise<void>
  /** Reload phase status only, after an edit that cannot change counts. */
  reloadStatus: () => Promise<void>
  /** Open the folder-import dialog owned by the shell. */
  requestImport: () => void
}

export const workspaceContextKey: InjectionKey<WorkspaceContext> = Symbol('workspace-context')
