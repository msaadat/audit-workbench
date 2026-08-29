import type { InjectionKey, Ref } from 'vue'

import type { EngagementPhase, EngagementSection, WorkspaceSummary } from '../types'

/**
 * What the workspace shell owns and every surface needs: the loaded workspace,
 * engagement phase status, and the two shell actions surfaces trigger. Provided
 * by `WorkspaceView`, which only renders its surfaces once the workspace is
 * loaded — so `workspace` is never null below the shell.
 */
export interface WorkspaceContext {
  workspace: Ref<WorkspaceSummary>
  /** Phase status in backend order, for the console rail. */
  phases: Ref<EngagementPhase[]>
  /** Per-section state, where a phase is broader than the one work product it
   *  is being read against — "Fieldwork" spans data tests and RCM coverage. */
  sectionById: Ref<Record<string, EngagementSection>>
  /** Reload the workspace summary and phase status together. */
  reload: () => Promise<void>
  /** Reload phase status only, after an edit that cannot change counts. */
  reloadStatus: () => Promise<void>
  /** Open the folder-import dialog owned by the shell. */
  requestImport: () => void
}

export const workspaceContextKey: InjectionKey<WorkspaceContext> = Symbol('workspace-context')
