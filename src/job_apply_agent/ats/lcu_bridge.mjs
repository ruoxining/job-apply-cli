// Private, typed IPC bridge. The model never supplies JavaScript.
import { createInterface } from 'node:readline';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';

const prefix = process.argv[2];
const { createCuaClient } = await import(pathToFileURL(join(prefix, 'adapters/client.mjs')));
const context = { sessionId: randomUUID(), turnId: randomUUID() };
const client = createCuaClient({ command: [join(prefix, 'bin/lcu')], env: process.env,
  adapter: 'job-apply-agent', onElicitation: async () => ({ action: 'cancel' }) });
let connected = false;
let windowId;
let browserPid;
function text(result) {
  return (result.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n');
}
async function call(code) {
  const result = await client.call('js', { code, title: 'Cloud job application', timeout_ms: 30000 }, context);
  if (result.isError) throw new Error(`LCU call failed: ${text(result).slice(0, 400)}`);
  return result;
}
function windows(result) {
  for (const block of result.content || []) {
    if (block.type !== 'text') continue;
    try {
      const value = JSON.parse(block.text);
      if (Array.isArray(value)) return value;
      if (Array.isArray(value.apps)) return value.apps.flatMap(a => (a.windows || []).map(w => ({...w, app:a.id})));
    } catch { /* Runtime documentation is not JSON. */ }
  }
  return [];
}
function ownsWindow(id, pid) {
  if (!Number.isSafeInteger(id) || id <= 0) return false;
  const value = execFileSync('xprop', ['-id', String(id), '_NET_WM_PID'], {encoding:'utf8', timeout:5000});
  return Number(value.match(/=\s*(\d+)/)?.[1]) === pid;
}
try {
  for await (const line of createInterface({input:process.stdin})) {
    try {
      const req = JSON.parse(line);
      let result;
      if (req.op === 'init') {
        if (connected || !Number.isSafeInteger(req.pid) || req.pid <= 0) throw new Error('Invalid init');
        browserPid = req.pid;
        await client.connect(); connected = true;
        await call('await cua.getState();');
        const candidates = windows(await call('await cua.listWindows();'))
          .filter(w => ownsWindow(Number(w.id), browserPid));
        if (candidates.length !== 1) throw new Error('Expected exactly one owned browser window');
        windowId = Number(candidates[0].id);
        result = await call(`let app = await cua.getApp({windowId:${windowId}});`);
      } else if (req.op === 'close') {
        process.stdout.write(JSON.stringify({ok:true})+'\n'); break;
      } else {
        if (!connected || !ownsWindow(windowId, browserPid)) throw new Error('Browser ownership changed');
        if (req.op === 'observe') result = await call('await app.getAXState({disableDiffing:true});');
        else if (req.op === 'action') {
          if (!Number.isSafeInteger(req.index) || req.index < 0) throw new Error('Invalid index');
          if (req.action === 'click') result = await call(`await app.click(${req.index}); await app.getAXState({disableDiffing:true});`);
          else if (req.action === 'scroll' && Number.isSafeInteger(req.value) && req.value !== 0 && Math.abs(req.value) <= 1200) {
            result = await call(`await app.scroll(${req.index}, ${JSON.stringify(req.value > 0 ? 'down' : 'up')}, {pixels:${Math.abs(req.value)}}); await app.getAXState({disableDiffing:true});`);
          }
          else if (req.action === 'fill' && typeof req.value === 'string') {
            await call(`await app.click(${req.index});`);
            // Native accessibility activation can return before Chromium moves
            // keyboard focus. Never type while the omnibox still owns focus.
            let focused = false;
            for (let attempt = 0; attempt < 8; attempt++) {
              const state = text(await call('await app.getAXState({disableDiffing:true});'));
              const rows = state.split('\n').filter(line => /\b(?:entry|combo box) \([^)]*focused/.test(line));
              focused = rows.length === 1 && new RegExp(`^\\s*${req.index} (?:entry|combo box) \\([^)]*focused`).test(rows[0]);
              if (focused) break;
              // Some Chromium builds focus only the renderer's AX node on
              // activation. Tab moves native keyboard focus without typing or
              // activating anything; verify again before sending any value.
              await call('await app.pressKey("Tab");');
            }
            if (!focused) throw new Error('LCU could not verify exclusive keyboard focus on the form field');
            result = await call(`await app.pressKey("ctrl+a"); await app.typeText(${JSON.stringify(req.value)});`);
          } else throw new Error('Unsupported action');
        } else throw new Error('Unsupported operation');
      }
      process.stdout.write(JSON.stringify({ok:true, text:text(result)})+'\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({ok:false, error:String(error.message).slice(0,600)})+'\n');
    }
  }
} finally {
  if (connected) await client.turnEnded(context).catch(()=>{});
  await client.close().catch(()=>{});
}
