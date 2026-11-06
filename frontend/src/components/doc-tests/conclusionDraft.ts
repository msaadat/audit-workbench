import { computed, reactive, ref, watch } from 'vue'

import type { ControlConclusion } from '../../types'

/**
 * The conclusion a Document Test carries, and the auditor's scope limitation
 * beside it — one draft, saved together.
 *
 * Each test kind used to keep these as two independent fields with a Save
 * each, and one of them edited the test prop in place. Saving the scope
 * limitation reloaded the test, which put the filed conclusion back over a
 * choice that had not been saved yet: two Save buttons side by side, each
 * discarding the other's work. Here a reload replaces only a field the
 * auditor has not changed, and Save sends every field that has.
 */

// Short labels: the field is already called "Conclusion", so repeating
// "Control" in every option only spent width the footer row does not have.
export const CONTROL_CONCLUSIONS: Array<{ label: string; value: ControlConclusion }> = [
  { label: 'Not concluded', value: 'no_conclusion' },
  { label: 'Effective', value: 'effective' },
  { label: 'Partially effective', value: 'partially_effective' },
  { label: 'Ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]

/** Only what changed: the PATCH a Save sends. */
export interface ConclusionChanges {
  control_conclusion?: ControlConclusion
  scope_limitations?: string
}

/** What is on file for one test, or null while it is loading. */
export interface FiledConclusion {
  /** The test this belongs to. A different key starts a fresh draft. */
  key: string
  conclusion: ControlConclusion
  scopeNote: string
}

export function useConclusionDraft(filed: () => FiledConclusion | null) {
  const conclusion = ref<ControlConclusion>('no_conclusion')
  const scopeNote = ref('')
  const savedConclusion = ref<ControlConclusion>('no_conclusion')
  const savedScopeNote = ref('')
  let key: string | null = null

  watch(
    () => {
      const value = filed()
      return value ? [value.key, value.conclusion, value.scopeNote] as const : null
    },
    value => {
      if (!value) return
      const [nextKey, nextConclusion, nextScope] = value
      const fresh = nextKey !== key
      key = nextKey
      // A field the auditor has moved keeps their value across a reload; one
      // they have not follows the file.
      if (fresh || conclusion.value === savedConclusion.value) conclusion.value = nextConclusion
      if (fresh || scopeNote.value === savedScopeNote.value) scopeNote.value = nextScope
      savedConclusion.value = nextConclusion
      savedScopeNote.value = nextScope
    },
    { immediate: true },
  )

  const conclusionChanged = computed(() => conclusion.value !== savedConclusion.value)
  const scopeNoteChanged = computed(() => scopeNote.value !== savedScopeNote.value)
  const dirty = computed(() => conclusionChanged.value || scopeNoteChanged.value)

  function changes(): ConclusionChanges {
    const result: ConclusionChanges = {}
    if (conclusionChanged.value) result.control_conclusion = conclusion.value
    if (scopeNoteChanged.value) result.scope_limitations = scopeNote.value
    return result
  }

  function discard() {
    conclusion.value = savedConclusion.value
    scopeNote.value = savedScopeNote.value
  }

  return reactive({ conclusion, scopeNote, conclusionChanged, scopeNoteChanged, dirty, changes, discard })
}
