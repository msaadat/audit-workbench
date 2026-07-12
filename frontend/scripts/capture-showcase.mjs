// Capture reproducible product screenshots from the synthetic Agent Demo workspace.
// Run while Chromium is listening on http://127.0.0.1:9222 with that workspace open.
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const debuggingPort = process.env.CHROME_DEBUG_PORT ?? '9222'
const outputDir = resolve(process.argv[2] ?? 'public/showcase')
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

await mkdir(outputDir, { recursive: true })
await call('Page.enable')
await call('Runtime.enable')
await call('Emulation.setDeviceMetricsOverride', {
  width: 1600,
  height: 1050,
  deviceScaleFactor: 1,
  mobile: false,
})
await wait(1800)

await clickByText('[role="tab"]', 'Query')
await capture('query-builder.png')

await clickByText('[role="tab"]', 'Validation')
await wait(900)
await clickByText('.rail-item', 'sales data quality')
await capture('validation-rules.png', { x: 215, y: 165, width: 970, height: 850 })

await clickByText('[role="tab"]', 'Analysis')
await wait(900)
await clickByText('.rail-item', 'Duplicate')
await capture('analysis-procedure.png')

socket.close()
