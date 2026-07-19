import type { LocationQuery, LocationQueryRaw } from 'vue-router'

type QueryValue = string | number | null | undefined

const ownedKeys: Record<string, readonly string[]> = {
  planning: ['view', 'rcm', 'planned_test', 'observation'],
  documents: ['doc', 'page'],
  'doc-tests': ['test', 'item', 'create', 'rcm', 'planned_test'],
  'data-tests': ['test', 'create', 'rcm', 'planned_test'],
  findings: ['finding'],
}

/** Build workspace deep links from destination-owned state only. */
export function workspaceQuery(tab: string, state: Record<string, QueryValue> = {}): LocationQueryRaw {
  const query: LocationQueryRaw = { tab }
  for (const [key, value] of Object.entries(state)) {
    if (value !== undefined && value !== null && value !== '') query[key] = value
  }
  return query
}

/** Remove query state owned by other tabs from an incoming/deep-linked URL. */
export function cleanWorkspaceQuery(tab: string, current: LocationQuery): LocationQueryRaw {
  const state: Record<string, QueryValue> = {}
  for (const key of ownedKeys[tab] ?? []) {
    const value = current[key]
    state[key] = Array.isArray(value) ? value[0] : value
  }
  return workspaceQuery(tab, state)
}
