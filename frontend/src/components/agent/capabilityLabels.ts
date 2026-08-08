/**
 * Auditor-facing names for capability IDs.
 *
 * Materialized stages carry their own `title`, so this map exists for the IDs
 * that appear without a stage: `readiness_before.blocking_on`, `next_outcomes`,
 * and `reused_capabilities` all name capabilities the run did not schedule.
 *
 * The strings are the registered `Capability.title` values verbatim, so the
 * spine reads the same whether a capability arrived as a stage or as a
 * reference. `test_plan_spine_capability_labels.py` fails if the two drift.
 */
export const CAPABILITY_LABELS: Record<string, string> = {
  // audit_workflow_v3
  'documents.text_ready': 'Document content',
  'documents.analysis_chunks_ready': 'Document chunk analysis',
  'documents.analysis_generated': 'Document analysis',
  'data.relationships_inferred': 'Table relationships',
  'data.join_utility_ready': 'Join utility selection',
  'data.joins_ready': 'Materialized joins',
  'analysis.definitions_ready': 'Analysis definitions',
  'analysis.executed': 'Analysis results',
  'analysis.summarized': 'Analysis summary',
  'planning.context_ready': 'Planning context',
  'planning.apm_ready': 'Audit planning memorandum',
  'planning.rcm_ready': 'Risk and control matrix',
  'tests.specified': 'Executable test specifications',
  'fieldwork.executed': 'Fieldwork execution',
  'results.rolled_up': 'Results and observations',
  'findings.drafted': 'Eligible finding drafts',
  'working_papers.generated': 'RCM working papers',
  'dashboard.curated': 'Dashboard curation',
  'report.working_draft': 'Report working draft',
  'audit.verified': 'Audit verification',
  // documents_workflow_v1
  'documents.analysis_reviewed': 'Document analysis review',
  // doc_tests_workflow_v2
  'doc_tests.definitions_ready': 'Document test definitions',
  'doc_tests.executed': 'Document test execution',
  'doc_tests.dispositioned': 'Auditor disposition',
}

/** An unregistered ID still has to read as something; never show a raw key. */
export function capabilityLabel(capabilityId: string): string {
  const known = CAPABILITY_LABELS[capabilityId]
  if (known) return known
  const tail = capabilityId.split('.').pop() ?? capabilityId
  return tail.replaceAll('_', ' ').replace(/^./, character => character.toUpperCase())
}

/**
 * Drop a label into mid-sentence case without flattening acronyms —
 * "RCM working papers" has to survive, "Dashboard curation" should not shout.
 */
function midSentence(label: string): string {
  const [first = ''] = label.split(' ')
  if (first.length > 1 && first === first.toUpperCase()) return label
  return label.charAt(0).toLowerCase() + label.slice(1)
}

/** Join capability references into one clause: "the RCM and fieldwork". */
export function capabilityClause(capabilityIds: readonly string[]): string {
  const labels = capabilityIds.map(id => midSentence(capabilityLabel(id)))
  if (labels.length <= 1) return labels[0] ?? ''
  return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`
}
