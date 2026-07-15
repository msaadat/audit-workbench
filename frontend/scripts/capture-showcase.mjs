// Capture the guided workspace at its two supported QA viewports.
// Run while Chromium is listening on http://127.0.0.1:9222 with a representative
// workspace open. Set CAPTURE_DIALOGS=1 to include dialogs whose entry points are
// available in that workspace.
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const debuggingPort = process.env.CHROME_DEBUG_PORT ?? '9222'
const outputDir = resolve(process.argv[2] ?? 'public/showcase')
const viewports = (process.env.CAPTURE_VIEWPORTS ?? '1366x768,1600x1050')
  .split(',')
  .map((value) => {
    const [width, height] = value.trim().split('x').map(Number)
    if (!width || !height) throw new Error(`Invalid viewport: ${value}`)
    return { width, height }
  })
const tabs = (process.env.CAPTURE_TABS
  ? process.env.CAPTURE_TABS.split(',').map((value) => value.trim()).filter(Boolean)
  : ['Dashboard', 'Planning', 'Documents', 'Document tests', 'Data', 'Query', 'Validation', 'Analysis', 'Findings', 'Report'])
const pages = await fetch(`http://127.0.0.1:${debuggingPort}/json/list`).then((response) => response.json())
const target = pages.find((page) => page.type === 'page')
if (!target) throw new Error('No Chromium page target found')

const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolveOpen, reject) => {
  socket.addEventListener('open', resolveOpen, { once: true })
  socket.addEventListener('error', reject, { once: true })
})

let nextId = 0
const pending = new Map()
socket.addEventListener('message', ({ data }) => {
  const message = JSON.parse(data)
  if (!message.id || !pending.has(message.id)) return
  const { resolveCall, rejectCall } = pending.get(message.id)
  pending.delete(message.id)
  if (message.error) rejectCall(new Error(message.error.message))
  else resolveCall(message.result)
})

function call(method, params = {}) {
  const id = ++nextId
  socket.send(JSON.stringify({ id, method, params }))
  return new Promise((resolveCall, rejectCall) => pending.set(id, { resolveCall, rejectCall }))
}

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds))

async function evaluate(expression) {
  const result = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
  return result.result.value
}

async function clickByText(selector, label) {
  const clicked = await evaluate(`(() => {
    const node = [...document.querySelectorAll(${JSON.stringify(selector)})]
      .find((candidate) => candidate.textContent.trim().includes(${JSON.stringify(label)}));
    if (!node) return false;
    const options = { bubbles: true, cancelable: true, view: window };
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      const EventType = type.startsWith('pointer') ? PointerEvent : MouseEvent;
      node.dispatchEvent(new EventType(type, options));
    }
    return true;
  })()`)
  if (!clicked) throw new Error(`Could not find ${selector} containing ${label}`)
}

async function capture(name, clip) {
  await wait(900)
  const { data } = await call('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: false,
    fromSurface: true,
    ...(clip ? { clip: { ...clip, scale: 1 } } : {}),
  })
  await writeFile(resolve(outputDir, name), Buffer.from(data, 'base64'))
}

async function setAssistant(open) {
  const changed = await evaluate(`(() => {
    const expanded = document.querySelector('[aria-label="Collapse audit assistant panel"]');
    const collapsed = document.querySelector('[aria-label="Expand audit assistant panel"]');
    const node = ${open ? 'collapsed' : 'expanded'};
    if (!node) return false;
    const options = { bubbles: true, cancelable: true, view: window };
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      const EventType = type.startsWith('pointer') ? PointerEvent : MouseEvent;
      node.dispatchEvent(new EventType(type, options));
    }
    return true;
  })()`)
  if (changed) await wait(450)
}

async function closeDialog() {
  await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' })
  await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' })
  await wait(300)
}

async function captureDialog(label, fileName, selector = 'button') {
  try {
    await clickByText(selector, label)
    await wait(500)
    const visible = await evaluate(`Boolean(document.querySelector('.p-dialog'))`)
    if (!visible) return false
    await capture(fileName)
    await closeDialog()
    return true
  } catch {
    return false
  }
}

await mkdir(outputDir, { recursive: true })
await call('Page.enable')
await call('Runtime.enable')
await call('Page.reload', { ignoreCache: true })
await wait(1200)

for (const viewport of viewports) {
  const viewportName = `${viewport.width}x${viewport.height}`
  await mkdir(resolve(outputDir, viewportName), { recursive: true })
  await call('Emulation.setDeviceMetricsOverride', {
    ...viewport,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await wait(900)

  for (const tab of tabs) {
    await clickByText('[role="tab"]', tab)
    await wait(450)
    const name = tab.toLowerCase().replaceAll(' ', '-')
    await setAssistant(false)
    await capture(`${viewportName}/${name}-assistant-compact.png`)
    await setAssistant(true)
    await capture(`${viewportName}/${name}-assistant-open.png`)
  }

  await setAssistant(false)
  if (process.env.CAPTURE_DIALOGS === '1') {
    await captureDialog('Import folder', `${viewportName}/dialog-folder-import.png`)
    await clickByText('[role="tab"]', 'Document tests')
    await captureDialog('Create manually', `${viewportName}/dialog-document-test.png`)
    await clickByText('[role="tab"]', 'Data')
    await captureDialog('Add join', `${viewportName}/dialog-join.png`)
    await clickByText('[role="tab"]', 'Validation')
    await captureDialog('Add', `${viewportName}/dialog-validation-check.png`)

    await setAssistant(true)
    const approvalVisible = await evaluate(`Boolean(document.querySelector('.interaction'))`)
    if (approvalVisible) await capture(`${viewportName}/dialog-assistant-decision.png`)
    await setAssistant(false)
  }
}

socket.close()
