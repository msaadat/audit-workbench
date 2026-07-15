import { computed, reactive } from 'vue'

import type { AuditDocument } from '../types'

interface AssistantContextState {
  documents: AuditDocument[]
}

const stores = new Map<string, AssistantContextState>()

function state(workspaceId: string): AssistantContextState {
  let existing = stores.get(workspaceId)
  if (!existing) {
    existing = reactive({ documents: [] })
    stores.set(workspaceId, existing)
  }
  return existing
}

export function useAssistantContext(workspaceId: string) {
  const store = state(workspaceId)

  function add(document: AuditDocument) {
    if (!store.documents.some((item) => item.id === document.id)) {
      store.documents.push(document)
    }
  }

  function replace(documents: AuditDocument[]) {
    store.documents.splice(0, store.documents.length, ...documents)
  }

  function remove(documentId: string) {
    const index = store.documents.findIndex((item) => item.id === documentId)
    if (index >= 0) store.documents.splice(index, 1)
  }

  function clear() {
    store.documents.splice(0)
  }

  return {
    documents: computed(() => store.documents),
    documentIds: computed(() => store.documents.map((item) => item.id)),
    add,
    replace,
    remove,
    clear,
  }
}
