export class ApiError extends Error {
  status?: number
  code?: string
  currentRevision?: number
}

const workspaceRevisions = new Map<string, number>()

function workspaceId(url: string): string | null {
  const match = url.match(/^\/api\/workspaces\/([^/]+)(?:\/|$)/)
  return match ? decodeURIComponent(match[1]) : null
}

function rememberRevision(url: string, response: Response): void {
  const id = workspaceId(url)
  const raw = response.headers.get('X-Workspace-Revision')
  if (id && raw && Number.isInteger(Number(raw))) workspaceRevisions.set(id, Number(raw))
}

function mutationHeaders(url: string, json = true): Record<string, string> {
  const headers: Record<string, string> = json ? { 'Content-Type': 'application/json' } : {}
  const id = workspaceId(url)
  const revision = id ? workspaceRevisions.get(id) : undefined
  if (revision !== undefined) headers['If-Match'] = `"rev-${revision}"`
  return headers
}

async function handle<T>(url: string, response: Response): Promise<T> {
  rememberRevision(url, response)
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    let code: string | undefined
    let currentRevision: number | undefined
    try {
      const body = await response.json()
      detail = body.detail ?? detail
      code = body.code
      currentRevision = body.current_revision
    } catch {
      /* non-JSON error body */
    }
    const error = new ApiError(detail)
    error.status = response.status
    error.code = code
    error.currentRevision = currentRevision
    throw error
  }
  if (response.status === 204 || response.status === 205) return undefined as T
  const text = await response.text()
  if (!text.trim()) return undefined as T
  return JSON.parse(text) as T
}

export const api = {
  get<T>(url: string): Promise<T> {
    return fetch(url).then((r) => handle<T>(url, r))
  },

  post<T>(url: string, body?: unknown): Promise<T> {
    return fetch(url, {
      method: 'POST',
      headers: mutationHeaders(url),
      body: JSON.stringify(body ?? {}),
    }).then((r) => handle<T>(url, r))
  },

  put<T>(url: string, body?: unknown): Promise<T> {
    return fetch(url, {
      method: 'PUT',
      headers: mutationHeaders(url),
      body: JSON.stringify(body ?? {}),
    }).then((r) => handle<T>(url, r))
  },

  patch<T>(url: string, body?: unknown): Promise<T> {
    return fetch(url, {
      method: 'PATCH',
      headers: mutationHeaders(url),
      body: JSON.stringify(body ?? {}),
    }).then((r) => handle<T>(url, r))
  },

  del<T>(url: string): Promise<T> {
    return fetch(url, { method: 'DELETE', headers: mutationHeaders(url, false) }).then((r) => handle<T>(url, r))
  },

  upload<T>(url: string, files: File[], fields: Record<string, string> = {}): Promise<T> {
    const form = new FormData()
    for (const [key, value] of Object.entries(fields)) form.append(key, value)
    for (const file of files) form.append('files', file)
    return fetch(url, { method: 'POST', headers: mutationHeaders(url, false), body: form }).then((r) => handle<T>(url, r))
  },

  uploadOne<T>(url: string, file: File, fields: Record<string, string> = {}): Promise<T> {
    const form = new FormData()
    for (const [key, value] of Object.entries(fields)) form.append(key, value)
    form.append('file', file)
    return fetch(url, { method: 'POST', headers: mutationHeaders(url, false), body: form }).then((r) => handle<T>(url, r))
  },

  /** PUT a single file to replace an existing table's data in place. */
  replace<T>(url: string, file: File): Promise<T> {
    const form = new FormData()
    form.append('file', file)
    return fetch(url, { method: 'PUT', headers: mutationHeaders(url, false), body: form }).then((r) => handle<T>(url, r))
  },

  /** POST that streams back an Excel file and triggers a browser download. */
  async download(url: string, body: unknown, filename: string): Promise<void> {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    })
    if (!response.ok) {
      let detail = `Export failed (${response.status})`
      try {
        detail = (await response.json()).detail ?? detail
      } catch {
        /* ignore */
      }
      throw new ApiError(detail)
    }
    const blob = await response.blob()
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = filename
    link.click()
    URL.revokeObjectURL(link.href)
  },
}
