// Screenshots the LawKitty web app (the visual reference for the desktop app) with headless
// Edge over the DevTools protocol. Needs node 22+ and a running `lawkitty-local` server.
//   node ref-shot.mjs <base-url> <out-dir> [theme]
// Writes home.png, tree.png (ITAA 1997 opened from the sidebar), section.png (s 8-1), search.png.
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const [base = 'http://127.0.0.1:8795', out = '.', theme = 'dark'] = process.argv.slice(2)
const edge = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(existsSync)
const port = 9377
const profile = mkdtempSync(join(tmpdir(), 'lk-ref-'))
const proc = spawn(edge, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--window-size=1440,900', '--force-device-scale-factor=1', 'about:blank'], { stdio: 'ignore' })
const sleep = ms => new Promise(r => setTimeout(r, ms))

let target
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200)
  try { target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === 'page') } catch {}
}
const ws = new WebSocket(target.webSocketDebuggerUrl)
await new Promise(r => ws.addEventListener('open', r))
let id = 0
const pending = new Map()
ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id) } })
const send = (method, params = {}) => new Promise(r => { pending.set(++id, r); ws.send(JSON.stringify({ id, method, params })) })
const js = expression => send('Runtime.evaluate', { expression, awaitPromise: true })
const shot = async name => {
  const { data } = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(out, name), Buffer.from(data, 'base64'))
  console.log('wrote', join(out, name))
}

mkdirSync(out, { recursive: true })
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
await send('Page.enable')
// The MCP banner is not part of the desktop app: dismiss it. Pick the theme the same way the app stores it.
await send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('mcp-token-banner-dismissed','1');` })
await send('Page.navigate', { url: `${base}/app/` })
await sleep(2500)
const key = (await js(`(() => { for (const s of document.scripts) {} return Object.keys(localStorage).join(',') })()`)).result.value
console.log('localStorage keys:', key)
if (theme !== 'dark') {
  await js(`fetch('/api/user/prefs',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:'${theme}'})}).then(r=>r.json()).then(d=>localStorage.setItem('legislation-user-prefs',JSON.stringify(d)))`)
  await send('Page.navigate', { url: `${base}/app/` })
  await sleep(2500)
}
await shot('home.png')
// Open ITAA 1997 from the sidebar, as a user does.
await js(`[...document.querySelectorAll('.lk-source-row a')].find(a => a.textContent.trim() === 'Income Tax Assessment Act 1997').click()`)
await sleep(2000)
await shot('tree.png')
await send('Page.navigate', { url: `${base}/app/itaa-1997/8-1` })
await sleep(3000)
await shot('section.png')
await send('Page.navigate', { url: `${base}/app/search` })
await sleep(2000)
await shot('search.png')
if (theme !== 'dark') await js(`fetch('/api/user/prefs',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:'dark'})})`)
ws.close()
proc.kill()
