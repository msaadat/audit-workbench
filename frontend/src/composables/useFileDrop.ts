/** Desktop drag-and-drop collection shared by the import dialog and the
 * workspace-wide drop target. Directories are walked recursively through the
 * FileSystemEntry API so dropped folders keep their relative paths. */

export interface StagedFile {
  file: File
  relativePath: string
}

async function walk(entry: FileSystemEntry, prefix: string, out: StagedFile[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject))
    out.push({ file, relativePath: `${prefix}${entry.name}` })
    return
  }
  if (!entry.isDirectory) return
  const reader = (entry as FileSystemDirectoryEntry).createReader()
  // readEntries returns results in batches (Chromium caps at 100 per call).
  for (;;) {
    const children = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
    if (!children.length) break
    for (const child of children) await walk(child, `${prefix}${entry.name}/`, out)
  }
}

export function dragHasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files')
}

export async function collectDroppedFiles(event: DragEvent): Promise<StagedFile[]> {
  const transfer = event.dataTransfer
  if (!transfer) return []
  // webkitGetAsEntry must be read synchronously for every item before the
  // first await invalidates the DataTransferItemList.
  const entries = Array.from(transfer.items ?? [])
    .filter((item) => item.kind === 'file')
    .map((item) => item.webkitGetAsEntry?.() ?? null)
  const out: StagedFile[] = []
  if (entries.some(Boolean)) {
    for (const entry of entries) if (entry) await walk(entry, '', out)
    return out
  }
  return Array.from(transfer.files ?? []).map((file) => ({ file, relativePath: file.name }))
}
