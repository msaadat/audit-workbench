import { createRouter, createWebHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import LoginView from './views/LoginView.vue'
import WorkspaceView from './views/WorkspaceView.vue'
import ConsoleView from './views/ConsoleView.vue'
import EngagementRecordView from './views/EngagementRecordView.vue'
import AuditFileView from './views/AuditFileView.vue'
import WorkbenchView from './views/WorkbenchView.vue'
import DebugView from './views/DebugView.vue'
import {
  destinationForLegacyTab,
  workspaceRouteFromQuery,
} from './composables/useWorkspaceNavigation'
import { loadIdentity, useSession } from './composables/useSession'

const WORKSPACE_ROUTES = [
  'workspace', 'workspace-console', 'workspace-file', 'workspace-bench',
]

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/login', name: 'login', component: LoginView, meta: { anonymous: true } },
    {
      path: '/invite/:token',
      name: 'invite',
      component: LoginView,
      props: true,
      meta: { anonymous: true },
    },
    { path: '/workspace/:id/debug', name: 'debug', component: DebugView, props: true },
    {
      path: '/workspace/:id',
      component: WorkspaceView,
      props: true,
      children: [
        // The engagement record is the landing surface; the chat has its own
        // path. Both keep the shell, so the assistant drawer rides along.
        { path: '', name: 'workspace', component: EngagementRecordView, props: true },
        { path: 'console', name: 'workspace-console', component: ConsoleView, props: true },
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
 * Establish the session before anything renders.
 *
 * In single-user mode this resolves to the local account and never redirects,
 * so the local-first product keeps behaving exactly as it did. In multi-user
 * mode an anonymous visitor is sent to the login screen with the route they
 * wanted, so a bookmarked deep link survives signing in.
 */
router.beforeEach(async to => {
  const session = useSession()
  if (!session.state.ready) {
    try {
      await loadIdentity()
    } catch {
      // Unreachable API: fall through to the login screen rather than hanging.
    }
  }
  // Already signed in, or nothing to sign in to: there is no login to show.
  if (to.meta.anonymous) {
    return session.state.user || session.state.singleUser ? '/' : true
  }
  if (!session.state.user) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  return true
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
