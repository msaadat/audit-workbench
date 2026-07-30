import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQuery, LocationQueryRaw, RouteLocationRaw } from 'vue-router'

type QueryValue = string | number | null | undefined

/**
 * Workspace navigation is destination-keyed, not path-keyed. Callers name where
 * they want to go ("rcm", "doc-tests") and this module owns which surface that
 * destination currently lives on. Phase 1 of docs/agentic-ux-plan.md moved every
 * destination from a `?tab=` query onto a real route; keeping the destination
 * names stable meant the ~25 call sites only changed shape, not intent.
 */

export type WorkspaceSurface = 'console' | 'decisions' | 'file' | 'bench'

export type WorkspaceDestination =
  | 'console'
  | 'decisions'
  | 'dashboard'
  | 'apm'
  | 'rcm'
  | 'doc-tests'
  | 'data-tests'
  | 'findings'
  | 'report'
  | 'documents'
  | 'data'
  | 'query'
  | 'analysis'

interface DestinationSpec {
  surface: WorkspaceSurface
  /** Path segment under the surface; console and decisions have none. */
  section: string
  /** Query keys this destination owns, so deep links can be normalized. */
  keys: readonly string[]
}

const DESTINATIONS: Record<WorkspaceDestination, DestinationSpec> = {
  console: { surface: 'console', section: '', keys: [] },
  decisions: { surface: 'decisions', section: '', keys: [] },
  // The curated dashboard is an audit outcome (`dashboard.curated`), so it sits
  // in the audit file rather than acting as the workspace landing page.
  dashboard: { surface: 'file', section: 'dashboard', keys: [] },
  apm: { surface: 'file', section: 'apm', keys: [] },
  rcm: { surface: 'file', section: 'coverage', keys: ['rcm', 'observation'] },
  'doc-tests': { surface: 'file', section: 'doc-tests', keys: ['test', 'item', 'create', 'rcm'] },
  'data-tests': { surface: 'file', section: 'data-tests', keys: ['test', 'create', 'rcm'] },
  findings: { surface: 'file', section: 'findings', keys: ['finding'] },
  report: { surface: 'file', section: 'report', keys: [] },
  documents: { surface: 'bench', section: 'documents', keys: ['doc', 'page'] },
  data: { surface: 'bench', section: 'tables', keys: [] },
  query: { surface: 'bench', section: 'query', keys: [] },
  analysis: { surface: 'bench', section: 'analysis', keys: ['view', 'analysis'] },
}

/** Sections in rail order, per surface. */
export const FILE_SECTIONS = [
  'dashboard', 'apm', 'coverage', 'data-tests', 'doc-tests', 'findings', 'report',
] as const
export const BENCH_SECTIONS = ['documents', 'tables', 'query', 'analysis'] as const

/**
 * Destinations retired from the rail but still reachable from old URLs and
 * bookmarks. `planning` additionally depended on a `view=rcm` query.
 */
const LEGACY_TABS: Record<string, WorkspaceDestination> = {
  validation: 'data',
  planning: 'apm',
}

function isDestination(value: string): value is WorkspaceDestination {
  return value in DESTINATIONS
}

/** Resolve a legacy `?tab=` value (plus its query) to a destination. */
export function destinationForLegacyTab(
  tab: string,
  query: LocationQuery = {},
): WorkspaceDestination {
  if (tab === 'planning' && query.view === 'rcm') return 'rcm'
  if (tab in LEGACY_TABS) return LEGACY_TABS[tab]
  return isDestination(tab) ? tab : 'console'
}

/** Reverse lookup used by the surface rails to mark the active entry. */
export function destinationForSection(
  surface: WorkspaceSurface,
  section: string,
): WorkspaceDestination | null {
  const found = (Object.keys(DESTINATIONS) as WorkspaceDestination[]).find(
    key => DESTINATIONS[key].surface === surface && DESTINATIONS[key].section === section,
  )
  return found ?? null
}

export function surfacePath(workspaceId: string, surface: WorkspaceSurface, section = ''): string {
  const base = `/workspace/${workspaceId}`
  if (surface === 'console') return base
  if (surface === 'decisions') return `${base}/decisions`
  return section ? `${base}/${surface}/${section}` : `${base}/${surface}`
}

/** Keep only the state a destination owns; drop keys left over from other tabs. */
function ownedQuery(
  destination: WorkspaceDestination,
  state: Record<string, QueryValue>,
): LocationQueryRaw {
  const query: LocationQueryRaw = {}
  for (const [key, value] of Object.entries(state)) {
    if (value === undefined || value === null || value === '') continue
    if (!DESTINATIONS[destination].keys.includes(key)) continue
    query[key] = value
  }
  return query
}

/** Build a workspace deep link from destination-owned state only. */
export function workspaceRoute(
  workspaceId: string,
  destination: WorkspaceDestination,
  state: Record<string, QueryValue> = {},
): RouteLocationRaw {
  const spec = DESTINATIONS[destination]
  return { path: surfacePath(workspaceId, spec.surface, spec.section), query: ownedQuery(destination, state) }
}

/** Carry a deep-linked URL's destination-owned query onto the new route. */
export function workspaceRouteFromQuery(
  workspaceId: string,
  destination: WorkspaceDestination,
  current: LocationQuery,
): RouteLocationRaw {
  const state: Record<string, QueryValue> = {}
  for (const key of DESTINATIONS[destination].keys) {
    const value = current[key]
    state[key] = Array.isArray(value) ? value[0] : value
  }
  return workspaceRoute(workspaceId, destination, state)
}

/**
 * Resolve a server-supplied navigation target. The backend still names
 * destinations with the pre-surface vocabulary (`planning`, `validation`), so
 * targets go through the same legacy mapping as bookmarked URLs.
 */
export function routeForTarget(
  workspaceId: string,
  target: { tab: string; query?: Record<string, string> },
): RouteLocationRaw {
  const query = target.query ?? {}
  return workspaceRoute(workspaceId, destinationForLegacyTab(target.tab, query), query)
}

/**
 * Navigation bound to the workspace in the current route. Components inside a
 * workspace never need to thread the id through to reach another destination.
 */
export function useWorkspaceNav() {
  const route = useRoute()
  const router = useRouter()
  const workspaceId = computed(() => String(route.params.id ?? ''))

  function to(destination: WorkspaceDestination, state: Record<string, QueryValue> = {}) {
    return workspaceRoute(workspaceId.value, destination, state)
  }
  return {
    workspaceId,
    to,
    push: (destination: WorkspaceDestination, state: Record<string, QueryValue> = {}) =>
      router.push(to(destination, state)),
    replace: (destination: WorkspaceDestination, state: Record<string, QueryValue> = {}) =>
      router.replace(to(destination, state)),
    target: (value: { tab: string; query?: Record<string, string> }) =>
      routeForTarget(workspaceId.value, value),
    replaceTarget: (value: { tab: string; query?: Record<string, string> }) =>
      router.replace(routeForTarget(workspaceId.value, value)),
  }
}
