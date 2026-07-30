import { createRouter, createWebHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import WorkspaceView from './views/WorkspaceView.vue'
import ConsoleView from './views/ConsoleView.vue'
import DecisionsView from './views/DecisionsView.vue'
import AuditFileView from './views/AuditFileView.vue'
import WorkbenchView from './views/WorkbenchView.vue'
import DebugView from './views/DebugView.vue'
import {
  destinationForLegacyTab,
  workspaceRouteFromQuery,
} from './composables/useWorkspaceNavigation'

const WORKSPACE_ROUTES = [
  'workspace', 'workspace-decisions', 'workspace-file', 'workspace-bench',
]

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/workspace/:id/debug', name: 'debug', component: DebugView, props: true },
    {
      path: '/workspace/:id',
      component: WorkspaceView,
      props: true,
      children: [
        { path: '', name: 'workspace', component: ConsoleView, props: true },
        { path: 'decisions', name: 'workspace-decisions', component: DecisionsView, props: true },
        { path: 'file/:section', name: 'workspace-file', component: AuditFileView, props: true },
        { path: 'bench/:section', name: 'workspace-bench', component: WorkbenchView, props: true },
        // Bare surface paths land on their first section.
        { path: 'file', redirect: to => `/workspace/${to.params.id}/file/dashboard` },
        { path: 'bench', redirect: to => `/workspace/${to.params.id}/bench/documents` },
      ],
    },
  ],
})

/**
 * `?tab=` URLs predate the surface routes and are still in bookmarks, saved
 * report links, and the docs. Redirect them onto the destination they named,
 * carrying only the query keys that destination owns.
 *
 * This is a global guard rather than a per-route `beforeEnter`: a legacy link
 * followed from inside the workspace only changes the query, and `beforeEnter`
 * does not re-run while the matched route record stays the same.
 */
router.beforeEach(to => {
  if (!to.query.tab) return true
  if (!WORKSPACE_ROUTES.includes(String(to.name ?? ''))) return true
  const destination = destinationForLegacyTab(String(to.query.tab), to.query)
  // The rewritten location drops `tab`, so the next pass falls straight through.
  return workspaceRouteFromQuery(String(to.params.id ?? ''), destination, to.query)
})

export default router
