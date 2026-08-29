import { createRouter, createWebHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import LoginView from './views/LoginView.vue'
import WorkspaceView from './views/WorkspaceView.vue'
import ConsoleView from './views/ConsoleView.vue'
import EngagementRecordView from './views/EngagementRecordView.vue'
import AuditFileView from './views/AuditFileView.vue'
import WorkbenchView from './views/WorkbenchView.vue'
import DebugView from './views/DebugView.vue'
import { loadIdentity, useSession } from './composables/useSession'

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
        { path: 'bench/:section', name: 'workspace-bench', component: WorkbenchView, props: true },
        { path: 'bench', redirect: to => `/workspace/${to.params.id}/bench/documents` },
        // Work products sit directly under the workspace. They were grouped
        // under `file/` while the audit file was a surface with its own rail;
        // the record is the index now, so the grouping named nothing a reader
        // could see. This wildcard is matched last: every static sibling above
        // outranks it, and `AuditFileView` sends a section it does not answer
        // for back to the first one.
        { path: ':section', name: 'workspace-file', component: AuditFileView, props: true },
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

export default router
