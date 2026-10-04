/** Browser evidence, never a replacement for the frozen 194 acceptance oracle.
 * No browser is imported/launched unless --run --frontend-ready --synthetic-db.
 * A per-ID plan supplies real setup and role/label actions; no UI response mocks,
 * product injection, paid provider, security-disable flags, or expected rewriting.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REGISTRY_HASH = '0b956741990f9d94dc0cd334df764f6b75dd0886e635ec26501dc321d1a20ffa';
const PROFILES = { owner: 'demo-owner', driver: 'demo-driver', driver2: 'demo-driver-2', operator: 'demo-operator' };
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const fail = (condition, message) => { if (!condition) throw new Error(message); };
const canonical = value => JSON.stringify(value, (_key, entry) => entry && !Array.isArray(entry)
  && typeof entry === 'object' ? Object.fromEntries(Object.entries(entry).sort(([a], [b]) => a.localeCompare(b))) : entry);

export async function readSpecs(root = ROOT) {
  const inputsRaw = await fs.readFile(path.join(root, 'tests/scenarios/acceptance-inputs.json'));
  const expectedRaw = await fs.readFile(path.join(root, 'tests/expected/acceptance-cases.json'));
  const inputs = JSON.parse(inputsRaw.toString('utf8').replace(/^\uFEFF/, ''));
  const expected = JSON.parse(expectedRaw.toString('utf8').replace(/^\uFEFF/, ''));
  return { inputs, expected, sources: { inputs_sha256: sha(inputsRaw), expected_sha256: sha(expectedRaw) } };
}

export function buildManifest({ inputs, expected, sources }) {
  fail(inputs.schema_version === 'acceptance-inputs-v1' && expected.schema_version === 'acceptance-cases-v1', 'Unsupported source schema');
  fail(inputs.suite_version === expected.suite_version && inputs.mode === 'mock'
    && inputs.actual_provider_call_budget === 0, 'Suite/mock/provider budget mismatch');
  const registry = inputs.groups.flatMap(group => group.variants.map(variant => ({
    id: `${group.test}-${variant}`, group: group.test, fixture: group.fixture,
  })));
  const ids = registry.map(row => row.id).sort();
  fail(new Set(ids).size === 194 && ids.length === 194 && sha(ids.join('\n')) === REGISTRY_HASH, 'Frozen 194 IDs changed');
  fail(canonical(ids) === canonical(Object.keys(expected.variant_expected).sort()), 'Expected IDs differ');
  const cases = registry.filter(row => expected.criteria[row.group].evidence.includes('E-UI')
    || row.id === 'R07-screen-execution-text').map(row => ({ ...row,
    required_evidence: structuredClone(expected.criteria[row.group].evidence),
    e_ui_required: expected.criteria[row.group].evidence.includes('E-UI'),
    supplemental_screen_escape: row.id === 'R07-screen-execution-text',
    criteria: structuredClone(expected.criteria[row.group]),
    independent_expected: structuredClone(expected.variant_expected[row.id]),
    adopted_conditions: [...(inputs.direct ?? []), ...(inputs.l3_cases ?? [])]
      .filter(condition => condition.id === row.id).map(condition => structuredClone(condition)),
    manual_requirements: row.id === 'V05b-screen-reader' ? ['actual_screen_reader_navigation']
      : row.id === 'V05b-mobile-zoom' ? ['actual_mobile_or_browser_zoom_review']
      : /audio|broadcast|allowed-zone/.test(row.id) ? ['actual_listening_if_claimed'] : [],
    required_plan_inputs: ['frontend_version', 'profile', 'route', 'condition_source',
      'fixture_seed_tick_injection_or_explicit_supplemental_condition', 'setup', 'role_label_steps',
      'visible_positive_and_negative_assertions', 'upstream_non_ui_evidence'],
  }));
  fail(cases.filter(row => row.e_ui_required).length === 54, 'Frozen E-UI count changed');
  return { schema_version: 'browser-acceptance-manifest-v1', suite_version: inputs.suite_version,
    sources, whole_suite_registered: 194, e_ui_required: 54, supplemental_screen_escape: 1,
    original_denominators: structuredClone(expected.denominators), cases };
}

export function localOrigin(value) {
  const url = new URL(value);
  fail(url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
    && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash
    && Number(url.port) >= 1024 && ![4178, 8018].includes(Number(url.port)),
  'Use a dedicated loopback HTTP proxy port, excluding the other frontend task ports 4178/8018');
  return url.origin;
}

export function allowedRequest(method, route, body = undefined) {
  const url = new URL(route, 'http://127.0.0.1:18080');
  const p = url.pathname;
  if (/live-queries/.test(p)) return false;
  if (method === 'GET') return /^\/(health\/(ready|live)|api\/v1\/(me(?:\/(vehicles|vehicle-locations|parking-map))?|facilities\/[^/]+\/(map|state|events|incidents|devices|relationships|commands|executions)|notifications|commands\/[^/]+(?:\/(plan|progress))?|executions\/[^/]+|incidents\/[^/]+(?:\/timeline)?|test\/runs\/[^/]+\/synthetic-users|test\/agent\/(config|operations(?:\/jobs)?)))$/.test(p);
  if (p === '/api/v1/auth/session') return ['POST', 'DELETE'].includes(method);
  if (p === '/api/v1/test/agent/operations') return method === 'POST' && body?.mode === 'mock';
  if (p === '/api/v1/test/agent/queries') return method === 'POST';
  if (/^\/api\/v1\/test\/runs(?:\/[^/]+\/(control|synthetic-users|device-faults|s2-reaction))?$/.test(p)) return ['POST', 'PUT'].includes(method);
  if (/^\/api\/v1\/facilities\/[^/]+\/relationships\/(customers|vehicles|vehicle-objects|person-objects)(?:\/[^/]+(?:\/customer)?)?$/.test(p)) return ['POST', 'PATCH', 'PUT'].includes(method);
  if (/^\/api\/v1\/facilities\/[^/]+\/commands$/.test(p)) return method === 'POST';
  return method === 'POST' && /^\/api\/v1\/(commands\/[^/]+\/(clarify|confirm|cancel)|executions\/[^/]+\/cancel|notifications\/[^/]+\/(receipts|responses))$/.test(p);
}

// APIRequestContext is independent of browser route interception. Never follow
// even same-origin redirects: a redirect hop can leave the dedicated proxy.
export async function localApiFetch(request, origin, route, options = {}) {
  const base = localOrigin(origin);
  const url = new URL(route, base);
  fail(url.origin === base && allowedRequest(options.method ?? 'GET', url.href, options.data), 'Unsafe setup API origin/route');
  const response = await request.fetch(url.href, { ...options, maxRedirects: 0 });
  fail(response.status() < 300 || response.status() >= 400, `Setup API redirect denied: HTTP ${response.status()}`);
  return response;
}

const OPS = new Set(['goto', 'click', 'fill', 'select', 'press', 'visible', 'absent', 'text',
  'capture', 'offline', 'keyboard', 'keyboard_zoom', 'viewport', 'escape_text', 'permission_end', 'api']);
function validateSelector(selector) {
  fail(selector && ['role', 'label', 'text'].includes(selector.by) && typeof selector.name === 'string'
    && selector.name.length > 0 && selector.name.length <= 500, 'A stable role/label/text selector is required');
  if (selector.by === 'role') fail(typeof selector.role === 'string' && /^[a-z]+$/.test(selector.role), 'Invalid role');
  if (selector.scope) {
    const scope = selector.scope;
    fail(['role', 'heading_section'].includes(scope.by) && typeof scope.name === 'string'
      && scope.name.length > 0 && scope.name.length <= 500 && !scope.scope, 'Invalid semantic selector scope');
    if (scope.by === 'role') fail(typeof scope.role === 'string' && /^[a-z]+$/.test(scope.role), 'Invalid scope role');
  }
}

export function validatePlan(plan, manifest) {
  fail(plan?.schema_version === 'browser-acceptance-plan-v1' && typeof plan.frontend_version === 'string'
    && plan.frontend_version.length > 0, 'Plan/frontend version required');
  const origin = localOrigin(plan.origin);
  const known = new Map(manifest.cases.map(row => [row.id, row]));
  const seen = new Set();
  fail(Array.isArray(plan.cases) && plan.cases.length > 0, 'Cases required');
  for (const entry of plan.cases) {
    fail(known.has(entry.id) && !seen.has(entry.id), 'Unknown or duplicate browser case'); seen.add(entry.id);
    fail(PROFILES[entry.profile] && typeof entry.route === 'string'
      && new URL(entry.route, origin).origin === origin, 'Local route/demo profile required');
    fail(entry.condition && ['adopted', 'supplemental'].includes(entry.condition.source)
      && typeof entry.condition.fixture === 'string' && entry.condition.fixture.length > 0
      && Number.isInteger(entry.condition.seed) && entry.condition.seed >= 0 && entry.condition.seed <= 2147483647
      && Number.isInteger(entry.condition.tick) && entry.condition.tick >= 0 && entry.condition.tick <= 10000
      && entry.condition.injection && typeof entry.condition.injection === 'object'
      && !Array.isArray(entry.condition.injection), 'Explicit fixture/seed/tick/injection and condition source required');
    if (entry.condition.source === 'adopted') {
      const match = known.get(entry.id).adopted_conditions.some(c =>
        c.seed === entry.condition.seed && c.tick === entry.condition.tick
        && (c.fixture ?? known.get(entry.id).fixture) === entry.condition.fixture
        && canonical(c.injection ?? {}) === canonical(entry.condition.injection ?? {}));
      fail(match, 'Claimed adopted seed/tick/injection does not match frozen input');
    }
    fail(Array.isArray(entry.setup) && Array.isArray(entry.steps) && entry.steps.length > 0, 'Setup and real UI steps required');
    fail(entry.setup.length <= 200 && entry.steps.length <= 200, 'Bounded plan required');
    for (const step of [...entry.setup, ...entry.steps]) {
      fail(OPS.has(step.op), `Unsupported step: ${step.op}`);
      if (['click', 'fill', 'select', 'visible', 'absent', 'text', 'escape_text'].includes(step.op)) validateSelector(step.selector);
      if (step.op === 'api') {
        fail(typeof step.path === 'string' && step.path.startsWith('/api/v1/')
          && new URL(step.path, origin).origin === origin && step.path !== '/api/v1/auth/session'
          && allowedRequest(step.method, step.path, step.body), 'Unsafe/paid API action');
        if (step.path === '/api/v1/test/runs') fail(step.body?.fixture_ref === entry.condition.fixture
          && step.body?.seed === entry.condition.seed, 'Run setup differs from declared fixture/seed');
      }
      if (step.op === 'goto') fail(new URL(step.path, origin).origin === origin, 'Cross-origin navigation denied');
      if (step.op === 'fill') fail(typeof step.value === 'string' && step.value.length <= 2000, 'Bounded fill required');
      if (step.op === 'text' || step.op === 'escape_text') fail(typeof step.literal === 'string' && step.literal.length > 0, 'Literal rendered text required');
      if (step.op === 'offline') fail(typeof step.value === 'boolean', 'Offline boolean required');
      if (step.op === 'press') fail(['Tab', 'Shift+Tab', 'Enter', 'Space', 'Escape', 'ArrowUp', 'ArrowDown'].includes(step.key), 'Unsupported keyboard key');
      if (step.op === 'viewport') fail(Number.isInteger(step.width) && step.width >= 320 && step.width <= 2560
        && Number.isInteger(step.height) && step.height >= 400 && step.height <= 1600, 'Invalid viewport');
      if (step.save_as) fail(/^[a-z][a-z0-9_]{0,40}$/.test(step.save_as), 'Invalid result variable');
    }
  }
  return { ...plan, origin };
}

export function summarize(rows) {
  return { browser_cases_registered: 55, e_ui_required: 54, whole_suite_registered: 194,
    attempted: rows.filter(row => row.status !== 'not_run').length,
    checks_observed: rows.filter(row => row.status === 'observed').length,
    failed: rows.filter(row => row.status === 'failed').length,
    partial: rows.filter(row => row.status === 'partial').length,
    not_run: rows.filter(row => row.status === 'not_run').length,
    acceptance_passed: 0, full_acceptance: false,
    boundary: 'Rendered evidence only; upstream decisions/side effects and independent review remain required' };
}

function locator(page, selector) {
  let root = page;
  if (selector.scope?.by === 'role') root = page.getByRole(selector.scope.role, { name: selector.scope.name, exact: true });
  // Card is a section with an h2 in the current frontend source. Plans specify
  // only the visible heading, never arbitrary CSS or user-supplied evaluation.
  if (selector.scope?.by === 'heading_section') root = page.locator('section').filter({
    has: page.getByRole('heading', { name: selector.scope.name, exact: true }) });
  return selector.by === 'role' ? root.getByRole(selector.role, { name: selector.name, exact: true })
    : selector.by === 'label' ? root.getByLabel(selector.name, { exact: true })
    : root.getByText(selector.name, { exact: true });
}
const clean = (text, limit = 4000) => String(text).replace(/(csrf_token|X-CSRF-Token|parking_session|api[_-]?key)[\s"':=]+[^\s,}<]+/gi, '$1=[redacted]').slice(0, limit);
export function lookupVariable(variables, expression) {
  const parts = expression.split('.');
  fail(parts.length <= 8 && /^[a-z][a-z0-9_]*$/.test(parts[0])
    && parts.every((key, i) => (i === 0 || /^(?:[a-z][a-z0-9_]*|\d+)$/.test(key))
      && !['__proto__', 'constructor', 'prototype'].includes(key)), 'Invalid setup variable path');
  let result = variables;
  for (const key of parts) {
    fail(result !== null && typeof result === 'object' && Object.hasOwn(result, key), `Missing setup variable ${expression}`);
    result = result[key];
  }
  fail(['string', 'number', 'boolean'].includes(typeof result) && (typeof result !== 'number' || Number.isFinite(result)),
    `Non-scalar setup variable ${expression}`);
  return result;
}
export const substitute = (value, variables) => {
  if (typeof value === 'string') {
    const whole = value.match(/^\$\{([^{}]+)\}$/);
    if (whole) return lookupVariable(variables, whole[1]);
    const expanded = value.replace(/\$\{([^{}]+)\}/g, (_all, expression) => String(lookupVariable(variables, expression)));
    fail(!expanded.includes('${'), 'Malformed setup variable expression');
    return expanded;
  }
  if (Array.isArray(value)) return value.map(v => substitute(v, variables));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, v]) => [key, substitute(v, variables)]));
  return value;
};

async function capture(page, directory, index, label) {
  const prefix = `${String(index).padStart(3, '0')}-${String(label).replace(/[^a-z0-9_-]/gi, '-').slice(0, 60)}`;
  const dom = await page.evaluate(() => {
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll('input,textarea').forEach(el => {
      el.removeAttribute('value'); if (el.tagName === 'TEXTAREA') el.textContent = '[input redacted]';
    });
    clone.querySelectorAll('script').forEach(el => el.remove());
    return { html: clone.outerHTML, text: document.body.innerText,
      title: document.title, url: location.pathname + location.hash,
      viewport: { width: innerWidth, height: innerHeight, scale: visualViewport?.scale, dpr: devicePixelRatio },
      scroll: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      active: { tag: document.activeElement?.tagName, label: document.activeElement?.getAttribute('aria-label') } };
  });
  await fs.writeFile(path.join(directory, `${prefix}.dom.html`), clean(dom.html, 2000000));
  await fs.writeFile(path.join(directory, `${prefix}.dom.json`), JSON.stringify({ ...dom, html: undefined,
    text: clean(dom.text, 1000000), text_truncated: dom.text.length > 1000000, html_truncated: dom.html.length > 2000000 }, null, 2));
  await page.screenshot({ path: path.join(directory, `${prefix}.png`), fullPage: true });
  return { prefix, rendered_text_nonempty: dom.text.trim().length > 0, viewport: dom.viewport };
}

async function stepAction(step, env) {
  const { page, context, operator, origin, variables, observations } = env;
  step = substitute(step, variables);
  const timeout = Math.min(step.timeout_ms ?? 15000, 60000);
  if (step.op === 'goto') await page.goto(new URL(step.path, origin).href, { waitUntil: 'domcontentloaded' });
  else if (step.op === 'click') await locator(page, step.selector).click({ timeout });
  else if (step.op === 'fill') await locator(page, step.selector).fill(step.value, { timeout });
  else if (step.op === 'select') await locator(page, step.selector).selectOption(step.value, { timeout });
  else if (step.op === 'press') await page.keyboard.press(step.key);
  else if (step.op === 'visible') await locator(page, step.selector).waitFor({ state: 'visible', timeout });
  else if (step.op === 'absent') await locator(page, step.selector).waitFor({ state: 'hidden', timeout });
  else if (step.op === 'text' || step.op === 'escape_text') {
    const element = locator(page, step.selector);
    await element.waitFor({ state: 'visible', timeout });
    fail((await element.innerText()).includes(step.literal), 'Expected literal is absent from rendered UI');
    if (step.op === 'escape_text') fail(await element.evaluate(el => ![el, ...el.querySelectorAll('*')].some(node =>
      node.tagName === 'SCRIPT' || [...node.attributes].some(a => /^on/i.test(a.name)
        || /^(href|src)$/i.test(a.name) && /^javascript:/i.test(a.value)))), 'Executable markup in displayed source');
    observations.push({ kind: step.op, literal: step.literal, source: step.source ?? 'unspecified' });
  } else if (step.op === 'offline') {
    await context.setOffline(step.value); observations.push({ kind: 'real_browser_offline', value: step.value });
  } else if (step.op === 'viewport') {
    await page.setViewportSize({ width: step.width, height: step.height });
    observations.push({ kind: 'emulated_viewport_only', width: step.width, height: step.height, actual_mobile: false });
  } else if (step.op === 'keyboard') {
    const trace = [];
    for (let i = 0; i < Math.min(step.count ?? 20, 100); i++) {
      await page.keyboard.press('Tab');
      trace.push(await page.evaluate(() => { const el = document.activeElement; const box = el.getBoundingClientRect();
        return { tag: el.tagName, label: el.getAttribute('aria-label'), text: (el.innerText ?? '').slice(0, 100),
          visible: box.width > 0 && box.height > 0, x: box.x, y: box.y }; }));
    }
    observations.push({ kind: 'real_keyboard_focus_trace', trace });
    fail(trace.some(entry => entry.visible && !['BODY', 'HTML'].includes(entry.tag)), 'No visible keyboard focus observed');
  } else if (step.op === 'keyboard_zoom') {
    const measure = () => page.evaluate(() => ({ width: innerWidth, scale: visualViewport?.scale, dpr: devicePixelRatio }));
    const before = await measure();
    for (let i = 0; i < Math.min(step.count ?? 3, 8); i++) await page.keyboard.press('Control+Equal');
    const after = await measure();
    const changed = canonical(before) !== canonical(after);
    observations.push({ kind: 'browser_keyboard_zoom', before, after, changed, actual_mobile: false });
    fail(changed, 'Browser zoom did not change measured scale; requires manual zoom evidence');
  } else if (step.op === 'permission_end') {
    await page.getByRole('button', { name: '나가기', exact: true }).click({ timeout });
    await page.getByLabel('비밀번호', { exact: true }).waitFor({ state: 'visible', timeout });
    if (step.private_text) fail(!(await page.locator('body').innerText()).includes(step.private_text), 'Private display retained after logout');
    observations.push({ kind: 'actual_ui_logout', server_expiry_or_revocation: false });
  } else if (step.op === 'api') {
    fail(allowedRequest(step.method, step.path, step.body), 'Unsafe substituted API action');
    const response = await localApiFetch(operator.request, origin, step.path, { method: step.method,
      data: step.body, headers: step.method === 'GET' ? {} : {
        Origin: origin, 'X-CSRF-Token': env.operatorCsrf, 'Idempotency-Key': crypto.randomUUID() }, timeout });
    fail(response.status() === (step.expected_status ?? 200), `Setup API HTTP ${response.status()}`);
    const value = response.status() === 204 ? {} : await response.json();
    if (step.save_as) variables[step.save_as] = value;
    observations.push({ kind: 'api_setup_only', method: step.method, path: new URL(step.path, origin).pathname,
      status: response.status(), inputs: step.body, result_reference: Object.fromEntries(
        Object.entries(value).filter(([key]) => ['run_id', 'command_id', 'execution_id', 'resource_version'].includes(key))) });
  }
}

export async function runBrowser(plan, manifest, output, options = {}) {
  const outputRoot = path.join(ROOT, 'Work_tree/artifacts/contest-browser-acceptance');
  fail(path.relative(outputRoot, output) !== '' && !path.relative(outputRoot, output).startsWith('..')
    && !path.isAbsolute(path.relative(outputRoot, output)), 'Output must be a new run directory inside the assigned artifacts folder');
  fail(path.dirname(output) === outputRoot, 'Use a direct new run directory under the assigned artifacts folder');
  await fs.mkdir(outputRoot, { recursive: true });
  await fs.mkdir(output, { recursive: false });
  const report = { schema_version: 'browser-acceptance-evidence-v1', frontend_version: plan.frontend_version,
    sources: manifest.sources, suite_version: manifest.suite_version, origin: plan.origin,
    started_at_utc: new Date().toISOString(), actual_provider_calls: 0, whole_acceptance: false,
    actual_screen_reader: 'not_run', actual_listening: 'not_run', actual_mobile_device: 'not_run',
    cases: manifest.cases.map(row => ({ ...row, status: 'not_run', artifacts: [], observations: [],
      missing: ['matching real browser plan/run', 'independent UI review', 'upstream non-UI evidence'] })),
    cleanup: { browser_started: false, browser_closed: false, contexts_closed: false, backend_started: false, errors: [] } };
  let browser, operator;
  const contexts = new Map();
  try {
    const moduleDir = options.playwrightModuleDir ?? process.env.BROWSER_ACCEPTANCE_PLAYWRIGHT_MODULE_DIR;
    const request = moduleDir ? createRequire(path.join(path.resolve(moduleDir), 'playwright/package.json'))
      : createRequire(import.meta.url);
    const { chromium } = request('playwright');
    fail(options.browserChannel === undefined || ['chrome', 'msedge'].includes(options.browserChannel), 'Unsupported installed browser channel');
    browser = await chromium.launch({ headless: options.headless === true, channel: options.browserChannel });
    report.cleanup.browser_started = true;
    operator = await browser.newContext();
    const response = await localApiFetch(operator.request, plan.origin, '/health/ready');
    const ready = await response.json();
    fail(response.status() === 200 && ready.test_control_enabled === true && ready.llm === 'not_configured'
      && ready.mode === 'local_foundation', 'Dedicated mock backend readiness required');
    const login = await localApiFetch(operator.request, plan.origin, '/api/v1/auth/session', { method: 'POST',
      headers: { Origin: plan.origin }, data: { username: PROFILES.operator, password: 'parking-demo-only' } });
    fail(login.status() === 200, 'Synthetic setup login failed');
    const meResponse = await localApiFetch(operator.request, plan.origin, '/api/v1/me');
    fail(meResponse.status() === 200, 'Synthetic setup identity failed');
    const identity = await meResponse.json();
    const configResponse = await localApiFetch(operator.request, plan.origin, '/api/v1/test/agent/config');
    fail(configResponse.status() === 200, 'Mock configuration check failed');
    const config = await configResponse.json();
    fail(config.mode === 'mock' && (config.providers ?? []).length === 0, 'Live model configuration forbidden');
    if (ready.current_run_id) {
      const workerResponse = await localApiFetch(operator.request, plan.origin,
        `/api/v1/test/agent/operations?run_id=${encodeURIComponent(ready.current_run_id)}`);
      fail(workerResponse.status() === 200, 'Worker isolation check failed');
      const worker = await workerResponse.json();
      fail(worker.mode !== 'live' && worker.active_jobs === 0, 'Active/live worker must be stopped by the environment owner');
    }
    report.environment = { mode: ready.mode, test_control_enabled: ready.test_control_enabled, live_models: false };
    for (const entry of plan.cases) {
      const row = report.cases.find(item => item.id === entry.id);
      const directory = path.join(output, entry.id); await fs.mkdir(directory);
      row.status = 'partial'; row.declared_condition = entry.condition;
      row.condition_observation = 'Setup requests recorded; frozen-condition equivalence requires independent corroboration';
      row.condition_adoption = entry.condition.source; row.profile = entry.profile;
      let context = contexts.get(entry.profile);
      if (!context) {
        context = await browser.newContext(); contexts.set(entry.profile, context);
        await context.route('**/*', async route => {
          const req = route.request(); const url = new URL(req.url());
          let body; try { body = req.postDataJSON(); } catch { /* Non-JSON assets. */ }
          if (url.origin !== plan.origin || url.pathname.startsWith('/api/') && !allowedRequest(req.method(), req.url(), body)) {
            await route.abort('blockedbyclient'); return;
          }
          await route.continue();
        });
      }
      const page = await context.newPage(); const logs = { console: [], page_errors: [], dialogs: [], network: [], sse: [] };
      const env = { page, context, operator, origin: plan.origin, operatorCsrf: identity.csrf_token,
        variables: {}, observations: row.observations };
      page.on('console', event => { if (['error', 'warning'].includes(event.type())) logs.console.push({ type: event.type(), text: clean(event.text()) }); });
      page.on('pageerror', error => logs.page_errors.push(clean(error.message)));
      page.on('dialog', async dialog => { logs.dialogs.push({ type: dialog.type(), text: clean(dialog.message()) }); await dialog.dismiss(); });
      page.on('response', response => { const url = new URL(response.url()); if (url.origin === plan.origin)
        logs.network.push({ method: response.request().method(), path: url.pathname, status: response.status() }); });
      page.on('requestfailed', req => logs.network.push({ method: req.method(), path: new URL(req.url()).pathname,
        failure: clean(req.failure()?.errorText ?? 'request_failed') }));
      page.on('request', async req => {
        const url = new URL(req.url());
        if (url.pathname.endsWith('/events')) logs.network.push({ path: url.pathname,
          run_id: url.searchParams.get('run_id'), cursor: url.searchParams.get('cursor'),
          sse_last_event_id: await req.headerValue('last-event-id') });
      });
      try {
        const cdp = await context.newCDPSession(page); await cdp.send('Network.enable');
        cdp.on('Network.eventSourceMessageReceived', event => {
          let value; try { value = JSON.parse(event.data); } catch { value = {}; }
          const snapshot = value.payload?.snapshot;
          logs.sse.push({ name: event.eventName, id: event.eventId, type: value.type, run_id: value.run_id,
            state_version: value.state_version, observation_id: snapshot?.observation_id,
            sim_time_ms: snapshot?.sim_time_ms, coverage: snapshot?.coverage });
        });
        let index = 0;
        // Establish the case's run before subscribing a role-scoped UI. Creating
        // a new run after driver subscription changes its scope stamp and sends
        // a genuine access.revoked, invalidating unrelated case preparation.
        for (const step of entry.setup) {
          fail(step.op === 'api', 'Pre-login setup must use declared API preparation only');
          await stepAction(step, env);
          row.artifacts.push(await capture(page, directory, index++, 'setup-api'));
        }
        await page.goto(`${plan.origin}/?mode=live#/login`, { waitUntil: 'domcontentloaded' });
        const loginField = page.getByLabel('비밀번호', { exact: true });
        await loginField.or(page.getByRole('button', { name: '나가기', exact: true })).first()
          .waitFor({ state: 'visible', timeout: 15000 });
        if (await loginField.isVisible()) {
          await page.getByLabel('계정', { exact: true }).fill(PROFILES[entry.profile]);
          await loginField.fill('parking-demo-only');
          await page.getByRole('button', { name: '로그인', exact: true }).click();
          await page.getByRole('button', { name: '나가기', exact: true }).waitFor({ state: 'visible', timeout: 15000 });
        }
        await page.goto(new URL(entry.route, plan.origin).href, { waitUntil: 'domcontentloaded' });
        row.artifacts.push(await capture(page, directory, index++, 'initial'));
        for (const step of entry.steps) {
          await stepAction(step, env);
          row.artifacts.push(await capture(page, directory, index++, step.op));
        }
        fail(logs.page_errors.length === 0 && logs.dialogs.length === 0, 'Runtime error/dialog observed');
        fail(row.artifacts.some(item => item.rendered_text_nonempty)
          && row.observations.some(item => ['text', 'escape_text', 'real_keyboard_focus_trace', 'actual_ui_logout'].includes(item.kind)),
        'DOM text/real UI assertion is missing; API-only is not UI evidence');
        row.status = 'observed'; row.missing = ['independent UI review', 'upstream non-UI evidence',
          'runtime seed/tick/injection corroboration', ...row.manual_requirements];
        if (entry.condition.source !== 'adopted') row.missing.push('supplemental condition adoption');
      } catch (error) {
        row.status = 'failed'; row.error = clean(error.message);
        try { row.artifacts.push(await capture(page, directory, 999, 'failure')); }
        catch (captureError) { row.capture_error = clean(captureError.message); }
      }
      finally {
        await context.setOffline(false);
        await fs.writeFile(path.join(directory, 'events.json'), JSON.stringify(logs, null, 2));
        await page.close();
      }
    }
  } catch (error) { report.environment_error = clean(error.message); }
  finally {
    for (const context of [...contexts.values(), ...(operator ? [operator] : [])]) {
      try { await context.close(); } catch (error) { report.cleanup.errors.push(clean(error.message)); }
    }
    report.cleanup.contexts_closed = report.cleanup.errors.length === 0;
    if (browser) {
      try { await browser.close(); report.cleanup.browser_closed = true; }
      catch (error) { report.cleanup.errors.push(clean(error.message)); }
    }
    report.finished_at_utc = new Date().toISOString(); report.summary = summarize(report.cases);
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  }
  return report;
}

export async function main(argv = process.argv.slice(2)) {
  const manifest = buildManifest(await readSpecs());
  if (!argv.includes('--run')) { process.stdout.write(JSON.stringify(manifest, null, 2) + '\n'); return 0; }
  fail(argv.includes('--frontend-ready') && argv.includes('--synthetic-db'), 'Main frontend start signal and dedicated synthetic DB acknowledgement required');
  const flag = name => { const index = argv.indexOf(name); fail(index >= 0 && argv[index + 1], `Missing ${name}`); return argv[index + 1]; };
  const plan = validatePlan(JSON.parse(await fs.readFile(flag('--plan'), 'utf8')), manifest);
  const report = await runBrowser(plan, manifest, path.resolve(flag('--output')), {
    headless: argv.includes('--headless'),
    browserChannel: argv.includes('--browser-channel') ? flag('--browser-channel') : undefined,
    playwrightModuleDir: argv.includes('--playwright-module-dir') ? flag('--playwright-module-dir') : undefined });
  process.stdout.write(JSON.stringify({ output: flag('--output'), summary: report.summary }) + '\n');
  return report.environment_error || report.summary.failed || report.cleanup.errors.length ? 3 : 0;
}

if (!process.execArgv.some(arg => arg === '-e' || arg === '--eval' || arg.startsWith('--eval='))
  && process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().then(code => { process.exitCode = code; }).catch(error => { process.stderr.write(clean(error.message) + '\n'); process.exitCode = 3; });
}
