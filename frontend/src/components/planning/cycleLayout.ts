/**
 * Where every node, field row and arrow segment sits on the cycle strip.
 *
 * A pure module because the arithmetic is the interesting part and a component
 * is a bad place to test it. The strip is a sequence — a handful of steps, a
 * couple of roles each — so the layout is a fixed lane assignment rather than a
 * force simulation: one column per step in flow order, documents in the top
 * lane, populations below. Deterministic positions, nothing to jitter between
 * loads, and no graph library.
 *
 * Arrows are orthogonal. One leaves the right edge of its source field row and
 * enters the left edge of its target field row. Between neighbouring columns it
 * takes a vertical track in the gutter; one that skips a column rides a
 * horizontal bus above the documents so it never passes through a node. Tracks
 * and lanes are assigned to minimise crossings, starting from list order and
 * swapping pairs while a swap reduces the count.
 */

import type {
  CycleEdge,
  CycleEdgeKind,
  CycleGraph,
  CycleGraphStep,
} from '../../types'

export const METRICS = {
  columnWidth: 236,
  gutter: 64,
  nodeHeaderHeight: 46,
  fieldHeight: 24,
  nodePadding: 8,
  documentTop: 96,
  populationGap: 40,
  busLane: 22,
  busTop: 30,
  trackInset: 12,
  /** A note, a pending line or a hidden-field count, wrapped to two lines. */
  trailingLineHeight: 34,
} as const

export interface LaidOutField {
  name: string
  role: string
  /** A one-operand assertion requires this field to be stated at all. */
  stated: boolean
  y: number
}

export interface LaidOutNode {
  id: string
  kind: 'document' | 'population'
  title: string
  subtitle: string
  /** Documents held of this type, or rows in this population. */
  count: number | null
  countLabel: string
  fields: LaidOutField[]
  hiddenFieldCount: number
  /** Whether the type has an induced schema at all, as against one no rule uses. */
  hasSchema: boolean
  x: number
  y: number
  width: number
  height: number
  anchor: boolean
  /** False for a role the rules could not bind; null before any rules exist. */
  bound: boolean | null
  note: string
}

export interface LaidOutColumn {
  step: string
  index: number
  x: number
  width: number
  nodes: LaidOutNode[]
}

export interface LaidOutEdge {
  kind: CycleEdgeKind
  ruleId: string
  label: string
  from: { node: string; field: string }
  to: { node: string; field: string }
  /** The polyline, already orthogonal, in strip coordinates. */
  points: Array<{ x: number; y: number }>
  /** True where the arrow skips a column and rides the bus above the nodes. */
  rides: boolean
}

export interface CycleLayout {
  columns: LaidOutColumn[]
  edges: LaidOutEdge[]
  width: number
  height: number
  crossings: number
}

/** Per node, the fields an edge leaves or enters, in the order the edges do. */
export function relationshipFields(graph: CycleGraph): Map<string, string[]> {
  const order = new Map<string, string[]>()
  const add = (node: string, field: string) => {
    if (!node || !field) return
    const fields = order.get(node) ?? []
    if (!fields.includes(field)) fields.push(field)
    order.set(node, fields)
  }
  for (const edge of graph.edges) {
    add(edge.from.node, edge.from.field)
    add(edge.to.node, edge.to.field)
  }
  for (const step of graph.steps) {
    for (const document of step.documents) {
      for (const field of step.stated) {
        if (document.fields.some(item => item.name === field)) add(document.node, field)
      }
    }
  }
  return order
}

/**
 * Header, field rows, and the lines that sit *below* them.
 *
 * The trailing lines are counted because the population node is placed under
 * this one and would otherwise be laid over it. They are counted rather than
 * measured, which is why every one of them is rendered after the field list:
 * a note above the fields would shift each row off the arrow endpoint computed
 * for it, and the arrows are the thing this arithmetic exists to place.
 */
function nodeHeight(fieldCount: number, trailingLines: number): number {
  return (
    METRICS.nodeHeaderHeight +
    METRICS.nodePadding * 2 +
    fieldCount * METRICS.fieldHeight +
    trailingLines * METRICS.trailingLineHeight
  )
}

function documentNodes(
  step: CycleGraphStep,
  shown: Map<string, string[]>,
  showAllFields: boolean,
): LaidOutNode[] {
  return step.documents.map(document => {
    const names = showAllFields
      ? document.fields.map(field => field.name)
      : shown.get(document.node) ?? []
    const byName = new Map(document.fields.map(field => [field.name, field]))
    const fields: LaidOutField[] = names.map((name, index) => ({
      name,
      role: byName.get(name)?.role ?? '',
      stated: step.stated.includes(name),
      y:
        METRICS.nodeHeaderHeight +
        METRICS.nodePadding +
        index * METRICS.fieldHeight +
        METRICS.fieldHeight / 2,
    }))
    const hiddenFieldCount = Math.max(0, document.fields.length - fields.length)
    const note =
      document.bound === false ? 'No identifier field · nothing links here' : ''
    return {
      id: document.node,
      kind: 'document',
      title: document.label || document.document_type,
      subtitle: document.document_type,
      count: document.count,
      countLabel: `${document.count} ${document.count === 1 ? 'document' : 'documents'}`,
      fields,
      hiddenFieldCount,
      hasSchema: document.fields.length > 0,
      x: 0,
      y: METRICS.documentTop,
      width: METRICS.columnWidth,
      height: nodeHeight(
        fields.length,
        (fields.length ? 0 : 1) + (note ? 1 : 0) + (hiddenFieldCount ? 1 : 0),
      ),
      anchor: false,
      bound: document.bound,
      note,
    }
  })
}

function populationNodes(step: CycleGraphStep, shown: Map<string, string[]>): LaidOutNode[] {
  return step.populations.map(population => {
    const names = shown.get(population.table) ?? population.columns.slice(0, 4)
    const fields: LaidOutField[] = names.map((name, index) => ({
      name,
      role: 'column',
      stated: false,
      y:
        METRICS.nodeHeaderHeight +
        METRICS.nodePadding +
        index * METRICS.fieldHeight +
        METRICS.fieldHeight / 2,
    }))
    const rows = population.rows
    const hiddenFieldCount = Math.max(0, population.columns.length - fields.length)
    const note = population.borrowed
      ? `No population of its own: recorded on ${population.table} as ${population.columns.join(', ')}.`
      : ''
    return {
      id: population.table,
      kind: 'population',
      title: population.table,
      subtitle: population.borrowed ? 'columns on another step’s table' : 'population',
      count: rows,
      countLabel: rows === null ? 'rows unavailable' : `${rows} rows`,
      fields,
      hiddenFieldCount,
      hasSchema: true,
      x: 0,
      y: 0,
      width: METRICS.columnWidth,
      height: nodeHeight(
        fields.length,
        (note ? 1 : 0) + (hiddenFieldCount ? 1 : 0),
      ),
      anchor: population.anchor,
      bound: null,
      note,
    }
  })
}

function segmentsCross(
  a: { x1: number; y1: number; x2: number; y2: number },
  b: { x1: number; y1: number; x2: number; y2: number },
): boolean {
  // Only the vertical runs can cross each other on this strip: horizontal runs
  // sit on their own lanes, and a vertical and a horizontal meeting is a corner
  // rather than a crossing.
  if (a.x1 !== a.x2 || b.x1 !== b.x2) return false
  if (a.x1 !== b.x1) return false
  const [aLow, aHigh] = a.y1 < a.y2 ? [a.y1, a.y2] : [a.y2, a.y1]
  const [bLow, bHigh] = b.y1 < b.y2 ? [b.y1, b.y2] : [b.y2, b.y1]
  return aLow < bHigh && bLow < aHigh
}

function countCrossings(edges: LaidOutEdge[]): number {
  const runs: Array<{ x1: number; y1: number; x2: number; y2: number }> = []
  for (const edge of edges) {
    for (let index = 0; index < edge.points.length - 1; index += 1) {
      runs.push({
        x1: edge.points[index].x,
        y1: edge.points[index].y,
        x2: edge.points[index + 1].x,
        y2: edge.points[index + 1].y,
      })
    }
  }
  let crossings = 0
  for (let i = 0; i < runs.length; i += 1) {
    for (let j = i + 1; j < runs.length; j += 1) {
      if (segmentsCross(runs[i], runs[j])) crossings += 1
    }
  }
  return crossings
}

interface Placed {
  node: LaidOutNode
  column: number
}

/**
 * A node is addressed by its step as well as its name.
 *
 * A table can be the population of one step and hold a few of another step's
 * columns, so `po_data` is two nodes on the strip. The backend names the step on
 * every edge end for exactly this reason; keying on the node alone put both
 * occurrences under one entry and routed the anchor out of the wrong column.
 */
function placementKey(step: string, node: string): string {
  return `${step}\u0000${node}`
}

function route(
  edge: CycleEdge,
  placed: Map<string, Placed>,
  track: number,
  lane: number,
): LaidOutEdge | null {
  const from = placed.get(placementKey(edge.from.step, edge.from.node))
  const to = placed.get(placementKey(edge.to.step, edge.to.node))
  if (!from || !to) return null
  const fromField = from.node.fields.find(field => field.name === edge.from.field)
  const toField = to.node.fields.find(field => field.name === edge.to.field)
  if (!fromField || !toField) return null

  // An arrow leaves the side of its source facing the target and enters the
  // side of its target facing the source. For a left-to-right edge that is the
  // right edge and the left edge, as the design states it; a rule pointing back
  // up the flow — an invoice citing the order it bills against — is the same
  // rule read the other way round, and entering a node from the far side would
  // draw a line straight through it.
  const forward = to.column >= from.column
  const startX = forward ? from.node.x + from.node.width : from.node.x
  const startY = from.node.y + fromField.y
  const endX = forward ? to.node.x : to.node.x + to.node.width
  const endY = to.node.y + toField.y
  const rides = Math.abs(from.column - to.column) > 1
  const common = {
    kind: edge.kind,
    ruleId: edge.rule_id,
    label: edge.label,
    from: { node: edge.from.node, field: edge.from.field },
    to: { node: edge.to.node, field: edge.to.field },
  }

  if (from.column === to.column) {
    // Within one column — the anchor, a population row up to its document. Out
    // the right edge, up the gutter and back in, so it never crosses the node.
    const x = from.node.x + from.node.width + METRICS.trackInset + track * 10
    return {
      ...common,
      points: [
        { x: from.node.x + from.node.width, y: startY },
        { x, y: startY },
        { x, y: endY },
        { x: to.node.x + to.node.width, y: endY },
      ],
      rides: false,
    }
  }

  if (!rides) {
    // One gutter, and the track is a lane within it rather than an offset from
    // whichever end happens to be on the left.
    const left = Math.min(startX, endX)
    const right = Math.max(startX, endX)
    const x = Math.min(left + METRICS.trackInset + track * 10, right - 2)
    return {
      ...common,
      points: [
        { x: startX, y: startY },
        { x, y: startY },
        { x, y: endY },
        { x: endX, y: endY },
      ],
      rides: false,
    }
  }

  // Skips a column: up onto the bus, across above every node, back down. The
  // lane spreads the vertical runs as well as the horizontal one — riders
  // routinely leave and enter at the same node edges, and separating only the
  // bus left three of them sharing one vertical line down into the same column.
  const busY = METRICS.busTop - lane * METRICS.busLane
  const inset =
    METRICS.trackInset + ((lane * 10) % (METRICS.gutter - METRICS.trackInset * 2))
  const exit = startX + (forward ? inset : -inset)
  const entry = endX + (forward ? -inset : inset)
  return {
    ...common,
    points: [
      { x: startX, y: startY },
      { x: exit, y: startY },
      { x: exit, y: busY },
      { x: entry, y: busY },
      { x: entry, y: endY },
      { x: endX, y: endY },
    ],
    rides: true,
  }
}


function routeAll(
  edges: CycleEdge[],
  placed: Map<string, Placed>,
  tracks: number[],
  lanes: number[],
): LaidOutEdge[] {
  const out: LaidOutEdge[] = []
  edges.forEach((edge, index) => {
    const laid = route(edge, placed, tracks[index], lanes[index])
    if (laid) out.push(laid)
  })
  return out
}

/**
 * Assign each arrow its gutter track and bus lane, then swap pairs while a swap
 * reduces the crossing count.
 *
 * Started from list order, which is the order the rules were written in, so a
 * strip whose rows already follow the arrows routes with no crossing and this
 * finds nothing to do. Bounded: it is a handful of arrows, and the loop stops
 * as soon as a full pass improves nothing.
 */
function minimiseCrossings(
  edges: CycleEdge[],
  placed: Map<string, Placed>,
): { edges: LaidOutEdge[]; crossings: number } {
  const tracks = edges.map((_, index) => index)
  const lanes = edges.map((_, index) => index)
  let best = routeAll(edges, placed, tracks, lanes)
  let bestCount = countCrossings(best)

  let improved = true
  while (improved && bestCount > 0) {
    improved = false
    for (let i = 0; i < edges.length && !improved; i += 1) {
      for (let j = i + 1; j < edges.length && !improved; j += 1) {
        // A rider owns both a track and a lane, so the two are swapped
        // together as well as singly: swapping one alone moves half an arrow.
        for (const swap of [
          () => {
            ;[tracks[i], tracks[j]] = [tracks[j], tracks[i]]
          },
          () => {
            ;[lanes[i], lanes[j]] = [lanes[j], lanes[i]]
          },
          () => {
            ;[tracks[i], tracks[j]] = [tracks[j], tracks[i]]
            ;[lanes[i], lanes[j]] = [lanes[j], lanes[i]]
          },
        ]) {
          swap()
          const candidate = routeAll(edges, placed, tracks, lanes)
          const count = countCrossings(candidate)
          if (count < bestCount) {
            best = candidate
            bestCount = count
            improved = true
            break
          }
          swap()
        }
      }
    }
  }
  return { edges: best, crossings: bestCount }
}

export function layoutCycle(
  graph: CycleGraph,
  options: { showAllFields?: boolean } = {},
): CycleLayout {
  const shown = relationshipFields(graph)
  const columns: LaidOutColumn[] = []
  const placed = new Map<string, Placed>()

  graph.steps.forEach((step, index) => {
    const x = index * (METRICS.columnWidth + METRICS.gutter)
    const documents = documentNodes(step, shown, options.showAllFields ?? false)
    const populations = populationNodes(step, shown)
    const documentBottom = documents.reduce<number>(
      (lowest, node) => Math.max(lowest, node.y + node.height),
      METRICS.documentTop,
    )
    // A step with two roles spans two columns; the second sits beside the first
    // rather than under it, because the roles are peers in the flow.
    documents.forEach((node, offset) => {
      node.x = x + offset * (METRICS.columnWidth + METRICS.gutter)
      placed.set(placementKey(step.name, node.id), { node, column: index + offset })
    })
    let populationTop = documentBottom + METRICS.populationGap
    populations.forEach(node => {
      node.x = x
      node.y = populationTop
      populationTop += node.height + 16
      placed.set(placementKey(step.name, node.id), { node, column: index })
    })
    columns.push({
      step: step.name,
      index,
      x,
      width:
        METRICS.columnWidth +
        Math.max(0, documents.length - 1) * (METRICS.columnWidth + METRICS.gutter),
      nodes: [...documents, ...populations],
    })
  })

  const { edges, crossings } = minimiseCrossings(graph.edges, placed)
  const width = columns.reduce<number>(
    (widest, column) => Math.max(widest, column.x + column.width),
    0,
  )
  const height = columns.reduce<number>(
    (tallest, column) =>
      Math.max(tallest, ...column.nodes.map(node => node.y + node.height)),
    METRICS.documentTop,
  )
  return { columns, edges, width, height, crossings }
}
