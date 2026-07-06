export class ApiError extends Error {}

async function handle<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      detail = body.detail ?? detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  get<T>(url: string): Promise<T> {
    return fetch(url).then((r) => handle<T>(r))
  },

  post<T>(url: string, body?: unknown): Promise<T> {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    }).then((r) => handle<T>(r))
  },

  del<T>(url: string): Promise<T> {
    return fetch(url, { method: 'DELETE' }).then((r) => handle<T>(r))
  },

  upload<T>(url: string, files: File[]): Promise<T> {
    const form = new FormData()
    for (const file of files) form.append('files', file)
    return fetch(url, { method: 'POST', body: form }).then((r) => handle<T>(r))
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
