import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw, RouteLocationRaw } from 'vue-router'

type QueryValue = string | number | null | undefined

/**
 * Workspace navigation is destination-keyed, not path-keyed. Callers name where
 * they want to go ("rcm", "doc-tests") and this module owns which surface that
 * destination currently lives on. Destinations are the only vocabulary: the
 * `?tab=` queries they replaced are gone from the router, from the backend's
 * navigation targets, and from this module.
 */

/**
 * `home` is the engagement record at the workspace root. The console keeps its
 * own path rather than the root: the landing question is "what was done here",
 * and the chat answers a different one — it is also always a click away in the
 * sidecar drawer on every surface but its own.
 *
 * `file` is a host rather than a place: it names which component answers for a
 * work product, and its sections sit directly under the workspace. The audit
 * file stopped being a surface when the record became the index, and the
 * `/file/` segment outlived the thing it named.
 */
export type WorkspaceSurface = 'home' | 'console' | 'file' | 'bench'

export type WorkspaceDestination =
  | 'console'
  | 'record'
  | 'apm'
  | 'rcm'
  | 'rcm-row'
  | 'chain'
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
  /** Path segment under the surface; the console has none. */
  section: string
  /** Query keys this destination owns, so deep links can be normalized. */
  keys: readonly string[]
  /**
   * The state key that becomes a path segment rather than a query parameter.
   *
   * One work product per route is the rule everywhere else here; a single RCM
   * row is the first thing that is *inside* a work product and still deserves
   * its own address, because it is what a reviewer is sent. Without the key's
   * value the destination collapses to its section, which is the matrix — a
   * link to "some row" is a link to the matrix.
   */
  param?: string
}

const DESTINATIONS: Record<WorkspaceDestination, DestinationSpec> = {
  console: { surface: 'console', section: '', keys: [] },
  // What the engagement holds, keyed by work product rather than by the chat
  // that asked for it. Drawn from the audit graph, with run history layered on.
  record: { surface: 'home', section: '', keys: [] },
  apm: { surface: 'file', section: 'apm', keys: [] },
  // `paper` names the row whose working paper is open, so a rendered paper is
  // a link someone can send rather than a dialog only they can see.
  rcm: { surface: 'file', section: 'coverage', keys: ['rcm', 'observation', 'paper'] },
  // One row as a page: `?rcm=` opens the drawer over the matrix, this opens
  // the row itself. `tab` picks which of its tabs — definition, attributes,
  // tests, the working paper, or where the row came from.
  'rcm-row': { surface: 'file', section: 'coverage', keys: ['rcm', 'tab'], param: 'rcm' },
  // The derivation spine for one risk, source through to finding.
  chain: { surface: 'file', section: 'chain', keys: ['rcm'] },
  'doc-tests': { surface: 'file', section: 'doc-tests', keys: ['test', 'item', 'create', 'rcm'] },
  'data-tests': { surface: 'file', section: 'data-tests', keys: ['test', 'create', 'rcm'] },
  findings: { surface: 'file', section: 'findings', keys: ['finding'] },
  report: { surface: 'file', section: 'report', keys: [] },
  documents: { surface: 'bench', section: 'documents', keys: ['doc', 'page'] },
  data: { surface: 'bench', section: 'tables', keys: [] },
  query: { surface: 'bench', section: 'query', keys: [] },
  // `view` picks Summary vs. Procedures; `filter` and `analysis` are
  // Procedures-only (a triage bucket and the open procedure).
  analysis: { surface: 'bench', section: 'analysis', keys: ['filter', 'analysis', 'view'] },
}

/** Sections in rail order, per surface. */
export const FILE_SECTIONS = [
  'apm', 'coverage', 'data-tests', 'doc-tests', 'findings', 'chain', 'report',
] as const
export const BENCH_SECTIONS = ['documents', 'tables', 'query', 'analysis'] as const

function isDestination(value: string): value is WorkspaceDestination {
  return value in DESTINATIONS
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
  if (surface === 'home') return base
  if (surface === 'console') return `${base}/console`
  // Work products are named directly: `/workspace/x/apm`, not `/x/file/apm`.
  if (surface === 'file') return `${base}/${section}`
  return section ? `${base}/${surface}/${section}` : `${base}/${surface}`
}

/** Keep only the state a destination owns; drop keys left over from other tabs. */
function ownedQuery(
  destination: WorkspaceDestination,
  state: Record<string, QueryValue>,
  skip?: string,
): LocationQueryRaw {
  const query: LocationQueryRaw = {}
  for (const [key, value] of Object.entries(state)) {
    if (value === undefined || value === null || value === '') continue
    if (key === skip) continue
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
  const param = spec.param ? String(state[spec.param] ?? '') : ''
  const base = surfacePath(workspaceId, spec.surface, spec.section)
  return {
    path: param ? `${base}/${encodeURIComponent(param)}` : base,
    query: ownedQuery(destination, state, param ? spec.param : undefined),
  }
}

/**
 * Resolve a server-supplied navigation target. The backend names destinations,
 * so this is a validation step rather than a translation: a target this build
 * does not know falls back to the console instead of routing nowhere.
 */
export function routeForTarget(
  workspaceId: string,
  target: { tab: string; query?: Record<string, string> },
): RouteLocationRaw {
  const query = target.query ?? {}
  return workspaceRoute(workspaceId, isDestination(target.tab) ? target.tab : 'console', query)
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
