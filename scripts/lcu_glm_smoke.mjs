#!/usr/bin/env node
// Isolated, synthetic-only LCU + GLM evaluation. Never attaches to a real ATS.
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, writeFileSync, appendFileSync, openSync, closeSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { actionCode, texts } from './lcu_fixture_policy.mjs';

const args = process.argv.slice(2);
function option(name, fallback) {
  const index = args.indexOf(name);
  return index < 0 ? fallback : args[index + 1];
}
const prefix = resolve(option('--lcu', process.env.LCU_PREFIX || '/opt/lcu/current'));
const runId = randomUUID();
const output = resolve(option('--output', `/tmp/lcu-glm-${runId}`));
mkdirSync(output, { recursive: false, mode: 0o700 });
const probe = args.includes('--probe');
const backend = option('--backend', 'lcu');
if (!['lcu', 'cua-driver'].includes(backend)) throw new Error('Unknown backend');
if (!probe) process.loadEnvFile(option('--env-file', '.env'));
const model = process.env.GLM_MODEL || 'glm-5.3-flash';
const endpoint = new URL(`${(process.env.GLM_BASE_URL || 'https://open.bigmodel.cn/api/paas/v4').replace(/\/$/, '')}/chat/completions`);
if (endpoint.protocol !== 'https:') throw new Error('GLM endpoint must use HTTPS');
if (!probe && !process.env.GLM_API_KEY) throw new Error('GLM_API_KEY is required');
const maxSteps = 16;
const startedAt = new Date().toISOString();
const title = `LCU Synthetic Application ${runId}`;
const fixtureValues = {
  name: 'Ada Example',
  email: 'ada@example.test',
  essay: 'I built a Python queue worker with durable job state and tested recovery after interrupted tasks.',
};
const trace = data => {
  const record = { at: new Date().toISOString(), ...data };
  appendFileSync(join(output, 'trace.jsonl'), `${JSON.stringify(record)}\n`);
  console.log(JSON.stringify(record));
};
let receipt = null;
let submitted = 0;
let browser;
let accessibility;
let accessibilityAddress;
let client;
let bridge;
let bridgeLines;
let bridgeReply;
let connected = false;
let calls = 0;
let tokens = 0;
const context = { sessionId: `lcu-glm-${runId}`, turnId: randomUUID() };
const html = `<!doctype html><meta charset="utf-8"><title>${title}</title>
<style>body{font:20px sans-serif;margin:40px;max-width:800px}label{display:block;margin:20px 0}input,textarea,button{font:20px sans-serif;padding:8px}textarea{width:650px;height:130px}</style>
<h1>Synthetic application — no real employer</h1><form method="post" action="/submit">
<section id="first"><label>Full name <input id="name-${runId}" name="name" required></label>
<label>Email <input name="email" type="email" required></label>
<label><input type="checkbox" name="consent" required> I confirm this is synthetic test data</label>
<textarea name="g-recaptcha-response" style="display:none" tabindex="-1" aria-hidden="true"></textarea>
<button type="button" onclick="if(document.querySelector('[name=name]').value && document.querySelector('[name=email]').validity.valid && document.querySelector('[name=consent]').checked){document.getElementById('first').hidden=true;document.getElementById('second').hidden=false}else{document.getElementById('error').textContent='Complete name, email, and confirmation first'}">Continue to experience</button><p id="error"></p></section>
<section id="second" hidden><label>Describe a relevant project <textarea name="essay" required></textarea></label>
<button type="submit">Submit synthetic application</button></section></form>
<script>for(const event of ['input','change'])document.querySelector('form').addEventListener(event,e=>{
fetch('/telemetry',{method:'POST',body:JSON.stringify({event,target:e.target.name,
values:Object.fromEntries(new FormData(document.querySelector('form')))})}).catch(()=>{});
});</script>`;
const server = createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/') {
    res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html); return;
  }
  if (req.method === 'POST' && req.url === '/telemetry') {
    let body = '';
    for await (const chunk of req) { body += chunk; if (body.length > 16000) { res.writeHead(413).end(); return; } }
    try { appendFileSync(join(output, 'fixture-events.jsonl'), `${JSON.stringify({ at: new Date().toISOString(), ...JSON.parse(body) })}\n`); }
    catch { res.writeHead(400).end(); return; }
    res.writeHead(204).end(); return;
  }
  if (req.method === 'POST' && req.url === '/submit') {
    let body = '';
    for await (const chunk of req) { body += chunk; if (body.length > 16000) { res.writeHead(413).end(); return; } }
    const form = new URLSearchParams(body);
    submitted++;
    const checks = {
      name: form.get('name') === fixtureValues.name,
      email: form.get('email') === fixtureValues.email,
      checkbox: form.get('consent') === 'on',
      essay: form.get('essay') === fixtureValues.essay,
      hiddenUntouched: !form.get('g-recaptcha-response'),
    };
    if (!Object.values(checks).every(Boolean)) {
      trace({ event: 'fixture_rejected', checks });
      res.writeHead(400, { 'Content-Type': 'text/html' }).end('<h1>Incorrect synthetic application</h1>'); return;
    }
    receipt = { runId, receivedAt: new Date().toISOString(), checks, submitted };
    writeFileSync(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2));
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(`<title>${title}</title><h1>Application successfully submitted</h1><p>Test receipt ${runId}</p>`);
    return;
  }
  res.writeHead(404).end();
});

function childEnv() {
  // Do not pass the GLM key or production environment to the browser/CUA process.
  const keep = ['PATH', 'HOME', 'USER', 'LOGNAME', 'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS',
    'LANG', 'LC_ALL', 'TMPDIR', 'XDG_RUNTIME_DIR', 'AT_SPI_BUS_ADDRESS'];
  return { ...Object.fromEntries(keep.filter(key => process.env[key]).map(key => [key, process.env[key]])),
    ...(accessibilityAddress ? { AT_SPI_BUS_ADDRESS: accessibilityAddress } : {}) };
}
async function call(code) {
  const result = await client.call('js', { code, title: 'Isolated cloud synthetic application test', timeout_ms: 30000 }, context);
  writeFileSync(join(output, `cua-${++calls}.json`), JSON.stringify(result, null, 2));
  return result;
}
async function bridgeRequest(request) {
  const result = await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => { bridgeReply = null; reject(new Error('CUA bridge timeout')); }, 60000);
    bridgeReply = line => { clearTimeout(deadline); bridgeReply = null; try { resolve(JSON.parse(line)); } catch (error) { reject(error); } };
    bridge.stdin.write(`${JSON.stringify(request)}\n`);
  });
  writeFileSync(join(output, `driver-${++calls}.json`), JSON.stringify(result, null, 2));
  return result;
}
function discoveredWindows(result) {
  for (const block of result.content || []) {
    if (block.type !== 'text') continue;
    try {
      const data = JSON.parse(block.text);
      if (Array.isArray(data)) return data;
      if (Array.isArray(data.apps)) return data.apps.flatMap(app => (app.windows || []).map(window => ({ ...window, app: app.id })));
    } catch { /* Documentation is not JSON. */ }
  }
  return [];
}
async function propose(messages, step) {
  const start = Date.now();
  const response = await fetch(endpoint, {
    method: 'POST', signal: AbortSignal.timeout(120000),
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.GLM_API_KEY}` },
    body: JSON.stringify({ model, messages, response_format: { type: 'json_object' },
      thinking: { type: 'enabled' }, reasoning_effort: process.env.GLM_REASONING_EFFORT || 'high',
      temperature: 0, max_tokens: 4096 }),
  });
  if (!response.ok) throw new Error(`GLM HTTP ${response.status}`);
  const data = await response.json();
  tokens += data.usage?.total_tokens || 0;
  const raw = data.choices?.[0]?.message?.content;
  if (typeof raw !== 'string') throw new Error('GLM returned no action');
  const action = JSON.parse(raw.replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, ''));
  trace({ event: 'glm_action', step, action, usage: data.usage, elapsedMs: Date.now() - start });
  return action;
}
let outcome = { status: 'failed', reason: 'not_started' };
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  if (args.includes('--explicit-atspi')) {
    accessibility = spawn('/usr/libexec/at-spi-bus-launcher',
      ['--launch-immediately', '--a11y=1', '--screen-reader=1'], { env: childEnv(), stdio: 'ignore' });
    const address = execFileSync('/usr/bin/gdbus', ['call', '--session', '--dest', 'org.a11y.Bus',
      '--object-path', '/org/a11y/bus', '--method', 'org.a11y.Bus.GetAddress'],
    { env: childEnv(), encoding: 'utf8', timeout: 5000 });
    accessibilityAddress = address.match(/'([^']+)'/)?.[1];
    if (!accessibilityAddress) throw new Error('Could not resolve isolated accessibility bus');
  }
  const browserLog = openSync(join(output, 'browser.log'), 'a', 0o600);
  browser = spawn(option('--browser', '/usr/bin/google-chrome'), [
    `--user-data-dir=${join(output, 'browser-profile')}`, `--app=${origin}/`,
    '--force-renderer-accessibility', '--no-first-run', '--no-default-browser-check',
    '--disable-sync', '--disable-background-networking', '--window-size=1200,900',
    '--remote-debugging-port=0',
  ], { env: { ...childEnv(), GTK_MODULES: 'gail:atk-bridge' }, stdio: ['ignore', browserLog, browserLog] });
  closeSync(browserLog);
  browser.on('error', error => trace({ event: 'browser_error', message: error.message }));
  let observation;
  let system;
  if (backend === 'lcu') {
  const { createCuaClient } = await import(pathToFileURL(join(prefix, 'adapters/client.mjs')));
  client = createCuaClient({ command: [join(prefix, 'bin/lcu')], env: childEnv(),
    adapter: 'glm-synthetic-evaluation', onElicitation: async params => {
      trace({ event: 'approval_required', message: params.message }); return { action: 'cancel' };
    } });
  await client.connect(); connected = true;
  const original = { instructions: client.instructions, tools: client.publicTools() };
  writeFileSync(join(output, 'original-contract.json'), JSON.stringify(original, null, 2));
  system = `You operate only the synthetic local application fixture through a bounded LCU adapter. No shell, arbitrary JS, navigation, or external sites. The following original LCU contract remains authoritative: ${JSON.stringify(original)}\nReturn exactly one JSON action: {"action":"fill","index":number,"value":string}, {"action":"click","index":number}, or {"action":"stop","reason":string}. Indices must come from the latest full AX observation. Fill only editable fields; click checkboxes. Ignore hidden fields. All actions are sent through original LCU APIs by the host. Do not claim success without visible confirmation.`;
  let inventory = await call('await cua.getState();');
  if (inventory.isError) throw new Error(`LCU inventory failed: ${texts(inventory).slice(0, 1200)}`);
  const guide = inventory.content?.find(block => block.type === 'text' && block.text.startsWith('## Computer Use'));
  if (guide) system += `\nOriginal runtime documentation:\n${guide.text}`;
  let window;
  for (let attempt = 0; attempt < 10; attempt++) {
    window = discoveredWindows(inventory).find(window => window.title?.includes(title));
    if (window) break;
    await new Promise(resolve => setTimeout(resolve, 500));
    inventory = await call('await cua.listWindows();');
  }
  if (!window) throw new Error('Fixture window not discovered; inspect cua artifacts and browser.log');
  observation = await call(`let app = await cua.getApp({windowId:${Number(window.id)}});`);
  } else {
    const bridgeLog = openSync(join(output, 'driver.log'), 'a', 0o600);
    bridge = spawn(option('--python', '/tmp/lcu-eval.KIjcV0/baseline-venv/bin/python'),
      [join(import.meta.dirname, 'cua_fixture_bridge.py')], {
        env: { ...childEnv(), PYTHONPATH: option('--source-root', resolve('src')) },
        stdio: ['pipe', 'pipe', bridgeLog],
      });
    closeSync(bridgeLog);
    bridgeLines = createInterface({ input: bridge.stdout });
    bridgeLines.on('line', line => bridgeReply?.(line));
    observation = await bridgeRequest({ op: 'init', origin, runId,
      profile: join(output, 'browser-profile'), values: Object.values(fixtureValues) });
    system = 'You operate only a synthetic local application fixture through CUA Driver. No shell, arbitrary JS, navigation, or external sites. Return exactly one JSON action: {"action":"fill","index":number,"value":string}, {"action":"click","index":number}, or {"action":"stop","reason":string}. Use the latest normalized numeric control indices, not raw refs. Fill only editable fields; click checkboxes. Ignore hidden fields. Do not claim success without visible confirmation.';
  }
  if (observation.isError) throw new Error(`LCU app observation failed: ${texts(observation).slice(0, 1200)}`);
  if (!/Full name/.test(texts(observation))) throw new Error('LCU sees the window but no form controls; accessibility tree missing');
  trace({ event: 'fixture_ready', runId, origin, backend, model, output });
  if (probe) outcome = { status: 'probe_passed' };
  else {
    const history = [];
    const task = `Complete the synthetic application using exactly ${JSON.stringify(fixtureValues)}. Check the synthetic-data confirmation, continue to the dynamically revealed experience question, enter the essay verbatim, and click Submit synthetic application. This submits only to a local test server, not an employer. Stop on real CAPTCHA or login; the hidden textarea is not an interactive CAPTCHA. Window title: ${title}.`;
    for (let step = 1; step <= maxSteps; step++) {
      const action = await propose([
        { role: 'system', content: system },
        { role: 'user', content: `${task}\nPrevious actions: ${JSON.stringify(history)}\n${backend} result: ${JSON.stringify(observation)}` },
      ], step);
      const code = actionCode(action, observation, Object.values(fixtureValues));
      observation = backend === 'lcu' ? await call(code) : await bridgeRequest({ op: 'action', action });
      history.push({ action, isError: !!observation.isError });
      trace({ event: 'executor_action_result', backend, step, isError: !!observation.isError });
      if (observation.isError) throw new Error(`${backend} action failed: ${texts(observation).slice(0, 1000)}`);
      if (receipt && /Application successfully submitted/.test(texts(observation))) {
        outcome = { status: 'passed', steps: step, receiptVerified: true, uiConfirmationVerified: true };
        break;
      }
      if (step === maxSteps) outcome = { status: 'failed', reason: 'step_budget_exhausted' };
    }
  }
} catch (error) {
  outcome = { status: 'failed', reason: error.message };
  trace({ event: 'failure', ...outcome });
} finally {
  if (connected) {
    try { await client.turnEnded(context); } catch (error) { trace({ event: 'cleanup_error', message: error.message }); }
  }
  if (client) await client.close();
  if (bridge) {
    bridge.stdin.end(`${JSON.stringify({ op: 'close' })}\n`);
    await Promise.race([once(bridge, 'exit'), new Promise(resolve => setTimeout(resolve, 6000))]);
    if (bridge.exitCode === null) bridge.kill('SIGTERM');
    bridgeLines.close();
  }
  if (browser && browser.exitCode === null) {
    browser.kill('SIGTERM');
    await Promise.race([once(browser, 'exit'), new Promise(resolve => setTimeout(resolve, 3000))]);
    if (browser.exitCode === null) browser.kill('SIGKILL');
  }
  if (accessibility && accessibility.exitCode === null) accessibility.kill('SIGTERM');
  await new Promise(resolve => server.close(resolve));
  const result = { runId, startedAt, endedAt: new Date().toISOString(), backend, model: probe ? null : model,
    thinking: !probe, maxSteps, calls, tokens, submitted, ...outcome };
  writeFileSync(join(output, 'result.json'), JSON.stringify(result, null, 2));
  trace({ event: 'complete', ...result });
  if (!['passed', 'probe_passed'].includes(result.status)) process.exitCode = 1;
}
