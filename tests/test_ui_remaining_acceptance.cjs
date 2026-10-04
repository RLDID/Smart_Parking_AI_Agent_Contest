"use strict";
// Additional temporary-console boundaries. No browser, server or external fetch.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const BASE = "/api/v1/facilities/fac-demo-01/relationships";
let fixtureNumber = 0;
const reply = (body, status = 200) => ({status, ok:status >= 200 && status < 300, json:async () => body});
const flush = () => new Promise(resolve => setImmediate(resolve));

function attributes(source) {
  return Object.fromEntries([...source.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1], m[2] ?? ""]));
}
function element(attrs = {}) {
  let ownText = "";
  const node = {attrs, id:attrs.id, value:attrs.value ?? "", disabled:"disabled" in attrs,
    dataset:{}, children:[], handlers:{}, classList:{add(){}, remove(){}},
    addEventListener(kind, fn) {this.handlers[kind] = fn;},
    setAttribute(key, value) {this.attrs[key] = String(value);},
    append(...nodes) {this.children.push(...nodes);},
    replaceChildren(...nodes) {ownText = ""; this.children = nodes;}};
  for (const [key, value] of Object.entries(attrs))
    if (key.startsWith("data-")) node.dataset[key.slice(5)] = value;
  Object.defineProperty(node, "textContent", {get() {return ownText + this.children.map(n => n.textContent ?? n).join("");},
    set(value) {ownText = String(value); this.children = [];}});
  Object.defineProperty(node, "innerHTML", {set() {throw new Error("HTML insertion is forbidden in this text-rendering fixture");}});
  return node;
}
function state(run = "run-1", version = 2, marker = "current") {
  return {run_id:run, run_status:"paused", recovery_required:false, applied_state_version:version,
    applied_sim_time_ms:version * 100, view_scope:"facility",
    snapshot:{run_id:run, state_version:version, sim_time_ms:version * 100, received_at:"synthetic-time",
      coverage:"full", objects:[], marker}};
}

function harness(t, page, {role = "test_operator", route = () => undefined} = {}) {
  const html = fs.readFileSync(path.join(ROOT, "code/devtools", page === "app" ? "index.html" : page + ".html"), "utf8");
  const source = fs.readFileSync(path.join(ROOT, "code/devtools", page + ".js"), "utf8");
  const prefix = `vm-${++fixtureNumber}`;
  const nodes = new Map(), forms = new Map(), all = [], timers = new Set(), streams = [], held = [];
  const calls = [], trace = [];
  for (const match of html.matchAll(/<([a-z]+)\b([^>]*)>/g)) {
    const attrs = attributes(match[2]);
    if (!attrs.id) continue;
    const node = element(attrs); nodes.set(attrs.id, node); all.push(node);
  }
  for (const match of html.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
    const attrs = attributes(match[1]);
    if (attrs.id) {
      const first = /<option\b([^>]*)>([^<]*)/.exec(match[2]);
      if (first) nodes.get(attrs.id).value = attributes(first[1]).value ?? first[2];
    }
  }
  for (const match of html.matchAll(/<form\b([^>]*)>([\s\S]*?)<\/form>/g)) {
    const attrs = attributes(match[1]);
    if (!("data-action" in attrs)) continue;
    const form = element(attrs), fields = new Map();
    for (const input of match[2].matchAll(/<(input|select)\b([^>]*)>/g)) {
      const a = attributes(input[2]);
      if (a.name) fields.set(a.name, element(a));
    }
    form.fields = fields; form.elements = {namedItem:name => fields.get(name)};
    const button = /<button\b([^>]*)>/.exec(match[2]);
    assert.ok(button, "real relationship submit button absent");
    form.submitButton = element(attributes(button[1])); forms.set(attrs["data-action"], form); all.push(form);
  }
  const loginMarkup = /<form\b[^>]*id="login-form"[^>]*>([\s\S]*?)<\/form>/.exec(html);
  const loginSubmit = loginMarkup && /<button\b([^>]*type="submit"[^>]*)>/.exec(loginMarkup[1]);
  const loginButton = element(loginSubmit ? attributes(loginSubmit[1]) : {});
  const get = id => {assert.ok(nodes.has(id), `fixture missing real HTML id: ${id}`); return nodes.get(id);};
  const identity = {user_id:"manager-a", alias:"Synthetic manager", csrf_token:"csrf-a", role};
  let currentRun = "run-1", key = 0, closed = false;
  const hooks = [];
  const document = {getElementById:get, createElement:() => element(), createElementNS:() => element(),
    querySelector:selector => {assert.equal(selector, '#login-form button[type="submit"]'); return loginButton;},
    querySelectorAll:selector => {
      if (selector === "form[data-action]") return [...forms.values()];
      const m = /^\[(data-[\w-]+)\]$/.exec(selector);
      assert.ok(m, `unmodelled selector: ${selector}`);
      return all.filter(n => m[1] in n.attrs);
    }};
  class EventSource {
    constructor(url) {this.url = url; this.handlers = {}; this.closed = false; streams.push(this); trace.push({event:"stream", url});}
    addEventListener(type, fn) {this.handlers[type] = fn;}
    close() {this.closed = true;}
    emit(type, data, id = "") {this.handlers[type]?.({data:JSON.stringify(data), lastEventId:id});}
  }
  const fallback = call => {
    const {url, method} = call;
    if (url === "/health/ready") return reply({current_run_id:currentRun});
    if (url === "/api/v1/me") return identity.role ? reply({...identity,
      facility_roles:[{facility_id:"fac-demo-01", roles:[identity.role]}]}) : reply({error:{message:"session ended"}}, 401);
    if (url === "/api/v1/auth/session" && method === "POST") return reply({});
    if (url === "/api/v1/auth/session" && method === "DELETE") return reply({}, 204);
    if (url === BASE && method === "GET") return reply({updated_at:"synthetic-time", customers:[], versions:[]});
    if (url.endsWith("/map")) return reply({zones:[]});
    if (url.includes("/me/vehicles?")) return reply({vehicles:[]});
    if (url.includes("/state?")) return reply(state(new URL(url, "http://fixture.invalid").searchParams.get("run_id")));
    if (url.includes("/spatial-analysis?")) return reply({metrics:{passage:"unknown"}, support_status:"insufficient_data", quality:{reasons:[]}});
    if (url.includes("/notifications?") || url.includes("/incidents?")) return reply({items:[]});
    if (url.includes("/devices?")) return reply({run_id:currentRun, alarms:[], broadcasts:[]});
    if (url.endsWith("/synthetic-users") && method === "GET") return reply({run_id:currentRun, mode:"manual"});
    if (url.includes("/agent/operations/jobs?") && method === "GET") return reply({items:[]});
    if (url.includes("/agent/operations?") && method === "GET") return reply({run_id:currentRun, mode:"mock", status:"idle"});
    if (url === "/api/v1/test/agent/config") return reply({mode:"mock", providers:[], budget:null});
    if (url.endsWith("/test/runs") && method === "POST") {currentRun = "run-2"; return reply(state(currentRun));}
    if (/\/commands\/[^/]+\/plan$/.test(url) && method === "GET") return reply({steps:[], marker:url});
    if (/\/commands\/[^/]+$/.test(url) && method === "GET") return reply({resource_version:1, marker:url});
    throw new Error(`unplanned fixture request: ${method} ${url}`);
  };
  const fetch = (url, options = {}) => {
    assert.equal(typeof url, "string"); assert.ok(url.startsWith("/"), "external requests forbidden");
    const call = {url, method:options.method || "GET", body:options.body ? JSON.parse(options.body) : null, headers:options.headers || {}};
    calls.push(call); trace.push({event:"fetch", ...call});
    const at = hooks.findIndex(h => h.match(call));
    if (at >= 0) {
      const hook = hooks.splice(at, 1)[0]; hook.started = true; trace.push({event:"held", label:hook.label});
      return hook.promise;
    }
    return Promise.resolve(route(call) ?? fallback(call));
  };
  vm.runInNewContext(source, {document, fetch, EventSource, URL, Date, JSON, Map, Error, Number, Promise, console,
    crypto:{randomUUID:() => `${prefix}-key-${++key}`},
    setInterval:fn => {timers.add(fn); return fn;}, clearInterval:fn => timers.delete(fn)});
  const ui = {page, identity, calls, trace, streams, get, forms,
    evidence:() => JSON.stringify(trace),
    text:() => [...nodes.values()].map(n => n.textContent).join("\n"),
    async ready() {await flush(); await flush();},
    async click(id) {const node = get(id); assert.equal(node.disabled, false, `user cannot click disabled ${id}`);
      assert.equal(typeof node.handlers.click, "function"); node.handlers.click(); await flush();},
    async input(id, value) {get(id).value = value; get(id).handlers.input?.(); await flush();},
    async poll() {for (const fn of timers) fn(); await flush();},
    async login() {assert.equal(loginButton.disabled, false); get("login-form").handlers.submit({preventDefault(){}}); await ui.ready();},
    async submit(kind, values) {const form = forms.get(kind); assert.ok(form); assert.equal(form.submitButton.disabled, false);
      for (const [name, value] of Object.entries(values)) {assert.ok(form.fields.has(name)); form.fields.get(name).value = String(value);}
      for (const field of form.fields.values()) if ("required" in field.attrs) assert.notEqual(field.value, "", "required input absent");
      form.handlers.submit({preventDefault(){}}); await flush();},
    defer(label, match) {let resolve, reject; const promise = new Promise((done, fail) => {resolve = done; reject = fail;});
      const hook = {label, match, promise, started:false, resolved:false};
      hook.release = (body, status = 200) => {assert.ok(!hook.resolved); hook.resolved = true; trace.push({event:"release", label, status}); resolve(reply(body, status));};
      hook.reject = error => {assert.ok(!hook.resolved); hook.resolved = true; trace.push({event:"reject", label}); reject(error);};
      hooks.push(hook); held.push(hook); return hook;},
  };
  t.after(async () => {
    if (closed) return; closed = true;
    for (const hook of held) if (hook.started && !hook.resolved) hook.release({error:{message:"fixture teardown"}}, 401);
    await flush(); for (const stream of streams) stream.close(); timers.clear(); hooks.length = 0;
    assert.equal(timers.size, 0); assert.ok(streams.every(s => s.closed));
  });
  return ui;
}
const link = {vehicle_id:"veh-demo-02", expected_version:"0", user_id:"demo-driver-2", reason:"synthetic transfer"};
const isPaidWork = c => c.method !== "GET" && /\/agent\/(live-queries|operations)$/.test(c.url);

for (const page of ["relationships", "operations"]) for (const role of ["owner", "test_operator", "driver"]) {
  test(`N4 ${page}: ${role} bootstrap and passive refresh never dispatch model work`, async t => {
    const ui = harness(t, page, {role}); await ui.ready(); await ui.poll();
    if (role !== "driver") await ui.click("refresh");
    assert.ok(ui.calls.every(c => c.method === "GET"), ui.evidence());
    assert.equal(ui.calls.filter(isPaidWork).length, 0);
    if (role === "driver") {
      assert.match(ui.get("status").textContent, /관리 권한/);
      if (page === "relationships") assert.equal(ui.calls.filter(c => c.url === BASE).length, 0);
      else {assert.equal(ui.get("agent-start").disabled, true); assert.equal(ui.get("command-create").disabled, true);}
    }
  });
}

test("N4 relationships: late snapshot after account replacement stays cleared", async t => {
  const ui = harness(t, "relationships"); await ui.ready();
  const old = ui.defer("old snapshot", c => c.url === BASE && c.method === "GET");
  await ui.click("refresh"); assert.ok(old.started);
  ui.identity.user_id = "manager-b"; await ui.poll();
  old.release({updated_at:"old", customers:[{display_alias:"OLD_ACCOUNT_CANARY"}]}); await ui.ready();
  assert.equal(ui.get("snapshot").textContent, ""); assert.ok(!ui.text().includes("OLD_ACCOUNT_CANARY"), ui.evidence());
});

test("N4 relationships: account change during post-mutation refresh hides old result", async t => {
  const ui = harness(t, "relationships"); await ui.ready();
  const old = ui.defer("old mutation", c => c.url.endsWith("/customer") && c.method === "PUT");
  await ui.submit("vehicle-user", link); assert.ok(old.started);
  ui.identity.user_id = "manager-b";
  ui.trace.push({event:"identity-change", user_id:ui.identity.user_id});
  old.release({resource_version:1, previous_alias:"OLD_MUTATION_CANARY"}); await ui.ready();
  assert.equal(ui.get("snapshot").textContent, "");
  assert.ok(!ui.text().includes("OLD_MUTATION_CANARY"), JSON.stringify({actual:ui.get("status").textContent, trace:ui.trace}));
  assert.match(ui.get("status").textContent, /계정|권한/);
});

test("N4 relationships: two VM clients surface version conflict without automatic retry", async t => {
  let version = 0;
  const route = c => {
    if (c.url === BASE) return reply({updated_at:`version-${version}`, versions:[{resource_version:version}]});
    if (c.url.endsWith("/customer") && c.method === "PUT") {
      if (c.body.expected_version !== version) return reply({error:{code:"RESOURCE_CHANGED", message:"관계 버전 충돌: 다시 조회하세요"}}, 409);
      return reply({resource_version:++version});
    }
  };
  const a = harness(t, "relationships", {route}), b = harness(t, "relationships", {route});
  await a.ready(); await b.ready(); await a.submit("vehicle-user", link); await b.submit("vehicle-user", link);
  const mutations = b.calls.filter(c => c.method === "PUT");
  assert.equal(version, 1); assert.equal(mutations.length, 1, b.evidence());
  assert.equal(mutations[0].body.expected_version, 0);
  assert.notEqual(a.calls.find(c => c.method === "PUT").headers["Idempotency-Key"], mutations[0].headers["Idempotency-Key"]);
  assert.match(b.get("status").textContent, /버전 충돌/); assert.doesNotMatch(b.get("status").textContent, /변경 완료/);
  await b.click("refresh"); assert.match(b.get("snapshot").textContent, /"resource_version": 1/);
});

test("N4 relationships: aliases and conflict messages are written as text", async t => {
  const marker = '<img src=x onerror="SYNTHETIC_CANARY()">';
  const ui = harness(t, "relationships", {route:c => c.url === BASE
    ? reply({updated_at:"now", customers:[{display_alias:marker}]})
    : c.method === "PUT" ? reply({error:{code:"RESOURCE_CHANGED", message:marker}}, 409) : undefined});
  await ui.ready(); assert.ok(ui.get("snapshot").textContent.includes("SYNTHETIC_CANARY"));
  await ui.submit("vehicle-user", link); assert.equal(ui.get("status").textContent, marker);
  assert.equal(ui.get("snapshot").children.length, 0);
});

test("N4 operations: same-user session token rotation rejects late device data", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const old = ui.defer("old devices", c => c.url.includes("/devices?"));
  await ui.click("read-devices"); assert.ok(old.started);
  ui.identity.csrf_token = "csrf-replaced"; await ui.poll();
  old.release({run_id:"run-1", secret:"OLD_SESSION_CANARY"}); await ui.ready();
  assert.ok(!ui.text().includes("OLD_SESSION_CANARY"), ui.evidence());
  assert.equal(ui.get("devices").textContent, "자료 없음"); assert.equal(ui.get("command-confirm").disabled, true);
});

test("N4 operations: obsolete command preview cannot enable a different command", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  await ui.input("command-id", "command-A");
  const old = ui.defer("old preview", c => c.url.endsWith("/commands/command-A"));
  await ui.click("command-preview"); assert.ok(old.started);
  await ui.input("command-id", "command-B");
  assert.equal(ui.get("command-confirm").disabled, true);
  await ui.click("command-preview"); assert.equal(ui.get("command-confirm").disabled, false);
  old.release({resource_version:99, marker:"OLD_PREVIEW_CANARY"}); await ui.ready();
  assert.ok(!ui.text().includes("OLD_PREVIEW_CANARY"), ui.evidence());
  assert.match(ui.get("command").textContent, /command-B/);
  const confirm = ui.defer("B confirmation", c => c.url.endsWith("/commands/command-B/confirm"));
  await ui.click("command-confirm"); assert.ok(confirm.started);
  assert.deepEqual(ui.calls.at(-1).body, {expected_resource_version:1});
  confirm.release({command_id:"command-B", resource_version:2}); await ui.ready();
});

for (const kind of ["reaction", "agent"]) test(`N4 operations: old-run ${kind} mutation cannot refill new-run panels`, async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const old = ui.defer(`old ${kind}`, c => kind === "reaction"
    ? c.url.endsWith("/run-1/synthetic-users") && c.method === "PUT"
    : c.url.endsWith("/agent/operations") && c.method === "POST");
  await ui.click(kind === "reaction" ? "save-reaction" : "agent-start"); assert.ok(old.started);
  await ui.click("create-run"); assert.match(ui.get("identity").textContent, /run-2/);
  // Keep the subsequent run-2 GET pending so a transient stale write is observable.
  const next = kind === "agent" ? ui.defer("new agent lookup", c => c.method === "GET" && c.url.includes("/agent/operations?")) : null;
  old.release({run_id:"run-1", marker:"OLD_RUN_CANARY"}); await ui.ready();
  const actual = ui.get(kind === "reaction" ? "reaction" : "agent-state").textContent;
  assert.ok(!actual.includes("OLD_RUN_CANARY"), JSON.stringify({actual, trace:ui.trace}));
  if (next?.started) {next.release({run_id:"run-2", mode:"mock"}); await ui.ready();}
});

test("N4 stream: retired source callbacks cannot affect the active connection", async t => {
  const ui = harness(t, "app"); await ui.login(); const old = ui.streams.at(-1);
  await ui.click("new-run"); const current = ui.streams.at(-1); assert.notEqual(old, current); assert.ok(old.closed);
  current.emit("state.snapshot", {payload:state("run-2", 5, "ACTIVE")}, "new-5"); await ui.ready();
  const before = ui.get("snapshot").textContent;
  old.emit("state.snapshot", {payload:state("run-1", 99, "OLD_STREAM_CANARY")}, "old-99");
  old.emit("access.revoked", {}); old.onerror?.(); await ui.ready();
  assert.equal(ui.get("snapshot").textContent, before, ui.evidence());
  assert.equal(ui.get("run-id").textContent, "run-2"); assert.equal(current.closed, false);
});

test("N4 stream: transport reconnect does not claim a fresh observation before a snapshot", async t => {
  const ui = harness(t, "app"); await ui.login(); const stream = ui.streams.at(-1);
  stream.emit("state.snapshot", {payload:state("run-1", 5, "LAST_OBSERVATION")}, "event-5"); await ui.ready();
  const before = ui.get("snapshot").textContent;
  const received = ui.get("received").textContent;
  await stream.onerror();
  assert.match(ui.get("connection").textContent, /연결 끊김.*마지막 관측/);
  stream.onopen(); await ui.ready();
  assert.match(ui.get("connection").textContent, /서버 연결됨.*마지막 관측.*수신 대기/);
  assert.equal(ui.get("snapshot").textContent, before);
  assert.equal(ui.get("received").textContent, received);
  assert.match(ui.get("analysis-status").textContent, /새 관측 수신/);
  stream.emit("reset_required", {});
  for (const id of ["run-status","run-id","sim-time","obs-time","received","version","coverage"])
    assert.equal(ui.get(id).textContent, "—", id);
  assert.equal(ui.get("snapshot").textContent, "새 스냅샷 대기");
  stream.onopen();
  assert.match(ui.get("connection").textContent, /새 스냅샷 대기/);
  stream.emit("state.snapshot", {payload:state("run-2", 1, "FRESH_SNAPSHOT")}, "event-1"); await ui.ready();
  assert.equal(ui.get("run-id").textContent, "run-2");
  assert.match(ui.get("connection").textContent, /일시정지 관측/);
});

test("N4 stream: reset invalidates pending inbox and spatial analysis responses", async t => {
  const ui = harness(t, "app"); await ui.login(); const stream = ui.streams.at(-1);
  const inbox = ui.defer("old inbox", c => c.url.includes("/notifications?"));
  const analysis = ui.defer("old analysis", c => c.url.includes("/spatial-analysis?"));
  stream.emit("notification.updated", {});
  stream.emit("state.snapshot", {payload:state("run-1", 3)}, "event-3"); await ui.ready();
  assert.ok(inbox.started && analysis.started);
  stream.emit("reset_required", {});
  stream.emit("state.snapshot", {payload:state("run-1", 4, "AFTER_RESET")}, "event-4"); await ui.ready();
  inbox.release({items:[{notification_id:"old-message", message:{text:"OLD_INBOX_CANARY"},
    delivery_status:"channel_accepted", mode:"live", responses:[]}]});
  analysis.release({metrics:{passage:"unknown"}, support_status:"OLD_ANALYSIS_CANARY", quality:{reasons:[]}}); await ui.ready();
  assert.ok(!ui.text().includes("OLD_INBOX_CANARY") && !ui.text().includes("OLD_ANALYSIS_CANARY"), ui.evidence());
  assert.equal(ui.calls.filter(c => c.url.endsWith("/old-message/receipts")).length, 0);
});

test("N4 stream: duplicate cursor and older state cannot roll back the displayed snapshot", async t => {
  const ui = harness(t, "app"); await ui.login(); const stream = ui.streams.at(-1);
  stream.emit("state.snapshot", {payload:state("run-1", 5, "LATEST")}, "event-5"); await ui.ready();
  const before = ui.get("snapshot").textContent;
  stream.emit("state.snapshot", {payload:state("run-1", 6, "DUPLICATE_CANARY")}, "event-5");
  stream.emit("state.snapshot", {payload:state("run-1", 4, "OLDER_CANARY")}, "event-4"); await ui.ready();
  assert.equal(ui.get("snapshot").textContent, before);
  stream.emit("reset_required", {});
  stream.emit("state.snapshot", {payload:state("run-2", 1, "RESET_ACCEPTED")}, "event-4"); await ui.ready();
  assert.equal(ui.get("run-id").textContent, "run-2"); assert.match(ui.get("snapshot").textContent, /RESET_ACCEPTED/);
});

function operationView(ui) {
  return {identity:ui.get("identity").textContent, state:ui.get("state").textContent,
    status:ui.get("status").textContent, commandId:ui.get("command-id").value,
    command:ui.get("command").textContent, plan:ui.get("plan").textContent,
    confirmDisabled:ui.get("command-confirm").disabled, requests:ui.calls.length};
}

test("N4 operations: old-run step cannot restore its run or refresh the new view", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const old = ui.defer("run-A step", c => c.url.endsWith("/run-1/control") && c.method === "POST");
  await ui.click("step"); assert.ok(old.started);
  assert.equal(ui.calls.at(-1).body.action, "step");
  await ui.click("create-run"); assert.match(ui.get("identity").textContent, /run-2/);
  const expected = operationView(ui);
  old.release(state("run-1", 9, "OLD_STEP_CANARY")); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
});

test("N4 operations: old-run fault setting cannot replace the new-run notice or trigger reads", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const old = ui.defer("run-A device fault", c => c.url.endsWith("/run-1/device-faults") && c.method === "PUT");
  await ui.click("save-fault"); assert.ok(old.started);
  assert.deepEqual(ui.calls.at(-1).body, {expected_state_version:2, channel:"visual", failed:true});
  await ui.click("create-run"); assert.match(ui.get("identity").textContent, /run-2/);
  const expected = operationView(ui);
  old.release({run_id:"run-1", applied_state_version:9}); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
});

test("N4 operations: old-run command creation cannot replace the new command selection", async t => {
  const ui = harness(t, "operations", {route:c => c.url.endsWith("/commands/command-B") && c.method === "GET"
    ? reply({command_id:"command-B", run_id:"run-2", resource_version:7}) : undefined});
  await ui.ready(); await ui.input("command-text", "합성 운영 안내");
  const old = ui.defer("run-A command create", c => c.url.endsWith("/facilities/fac-demo-01/commands") && c.method === "POST");
  await ui.click("command-create"); assert.ok(old.started);
  assert.equal(ui.calls.at(-1).body.run_id, "run-1"); assert.equal(ui.calls.at(-1).body.based_on_state_version, 2);
  await ui.click("create-run"); assert.match(ui.get("identity").textContent, /run-2/);
  await ui.input("command-id", "command-B"); await ui.click("command-preview");
  assert.equal(ui.get("command-confirm").disabled, false);
  const expected = operationView(ui);
  old.release({command_id:"command-A", run_id:"run-1", resource_version:1}); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
  const confirm = ui.defer("retained B version", c => c.url.endsWith("/commands/command-B/confirm"));
  await ui.click("command-confirm"); assert.ok(confirm.started);
  assert.deepEqual(ui.calls.at(-1).body, {expected_resource_version:7});
  confirm.release({command_id:"command-B", resource_version:8}); await ui.ready();
});

test("N4 operations: late command-A confirmation cannot invalidate command-B preview", async t => {
  const ui = harness(t, "operations", {route:c => c.url.endsWith("/commands/command-B") && c.method === "GET"
    ? reply({command_id:"command-B", run_id:"run-1", resource_version:7}) : undefined});
  await ui.ready(); await ui.input("command-id", "command-A"); await ui.click("command-preview");
  const old = ui.defer("command-A confirmation", c => c.url.endsWith("/commands/command-A/confirm"));
  await ui.click("command-confirm"); assert.ok(old.started);
  assert.deepEqual(ui.calls.at(-1).body, {expected_resource_version:1});
  await ui.input("command-id", "command-B"); await ui.click("command-preview");
  assert.equal(ui.get("command-confirm").disabled, false);
  const expected = operationView(ui);
  // An unwanted repeat GET must not conceal the stale result before inspection.
  const reread = ui.defer("unwanted B reread", c => c.url.endsWith("/commands/command-B") && c.method === "GET");
  old.release({command_id:"command-A", resource_version:2, marker:"OLD_CONFIRM_CANARY"}); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
  assert.equal(reread.started, false);
  const confirm = ui.defer("current B confirmation", c => c.url.endsWith("/commands/command-B/confirm"));
  await ui.click("command-confirm"); assert.ok(confirm.started);
  assert.deepEqual(ui.calls.at(-1).body, {expected_resource_version:7});
  confirm.release({command_id:"command-B", resource_version:8}); await ui.ready();
  if (reread.started) {reread.release({command_id:"command-B", resource_version:8}); await ui.ready();}
});

test("3-03 operations: late failed run-A step cannot re-read or mark run-B as failed", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const old = ui.defer("run-A step fails after B", c => c.url.endsWith("/run-1/control") && c.method === "POST");
  await ui.click("step"); assert.ok(old.started);
  await ui.click("create-run"); assert.match(ui.get("identity").textContent, /run-2/);
  const expected = operationView(ui);
  old.release({error:{message:"old run failure", code:"TEMPORARY_UNAVAILABLE"}}, 503); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
});

test("3-03 operations: late command-A conflict cannot replace command-B preview status", async t => {
  const ui = harness(t, "operations", {route:c => c.url.endsWith("/commands/command-B") && c.method === "GET"
    ? reply({command_id:"command-B", run_id:"run-1", resource_version:7}) : undefined});
  await ui.ready(); await ui.input("command-id", "command-A"); await ui.click("command-preview");
  const old = ui.defer("command-A conflict after B", c => c.url.endsWith("/commands/command-A/confirm"));
  await ui.click("command-confirm"); assert.ok(old.started);
  await ui.input("command-id", "command-B"); await ui.click("command-preview");
  const expected = operationView(ui);
  old.release({error:{message:"old command conflict", code:"VERSION_CONFLICT"}}, 409); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
  const confirm = ui.defer("current B confirmation", c => c.url.endsWith("/commands/command-B/confirm"));
  await ui.click("command-confirm"); assert.ok(confirm.started);
  assert.deepEqual(ui.calls.at(-1).body, {expected_resource_version:7});
  confirm.release({command_id:"command-B", resource_version:8}); await ui.ready();
});

for (const [kind, finish] of [
  ["server failure", hook => hook.release({error:{message:"old command server failure"}}, 503)],
  ["network rejection", hook => hook.reject(new Error("old command connection lost"))],
]) test(`3-03 operations: late command-A ${kind} cannot re-read or mark command-B as failed`, async t => {
  const ui = harness(t, "operations", {route:c => c.url.endsWith("/commands/command-B") && c.method === "GET"
    ? reply({command_id:"command-B", run_id:"run-1", resource_version:7}) : undefined});
  await ui.ready(); await ui.input("command-id", "command-A"); await ui.click("command-preview");
  const old = ui.defer(`command-A ${kind} after B`, c => c.url.endsWith("/commands/command-A/confirm"));
  await ui.click("command-confirm"); assert.ok(old.started);
  await ui.input("command-id", "command-B"); await ui.click("command-preview");
  const expected = operationView(ui);
  finish(old); await ui.ready();
  assert.deepEqual(operationView(ui), expected, ui.evidence());
});

test("3-03 operations: current command conflict remains visible without an automatic retry", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  await ui.input("command-id", "command-A"); await ui.click("command-preview");
  const conflict = ui.defer("current command conflict", c => c.url.endsWith("/commands/command-A/confirm"));
  await ui.click("command-confirm"); assert.ok(conflict.started);
  const requests = ui.calls.length;
  conflict.release({error:{message:"version conflict", code:"VERSION_CONFLICT"}}, 409); await ui.ready();
  assert.equal(ui.calls.length, requests, ui.evidence());
  assert.match(ui.get("status").textContent, /version conflict/);
  assert.equal(ui.get("status").dataset.error, "true");
});

test("3-03 operations: overlapping identical action sends once and refreshes once", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const first = ui.defer("single step", c => c.url.endsWith("/run-1/control") && c.method === "POST");
  await ui.click("step"); await ui.click("step");
  assert.ok(first.started);
  assert.equal(ui.calls.filter(c => c.method === "POST").length, 1, ui.evidence());
  assert.match(ui.get("status").textContent, /진행 중/);
  const reads = ui.calls.filter(c => c.method === "GET").length;
  first.release(state("run-1", 3)); await ui.ready();
  assert.equal(ui.calls.filter(c => c.method === "GET").length - reads, 5, ui.evidence());
  assert.match(ui.get("status").textContent, /step 요청 결과 확인/);
});

for (const [kind, finish] of [
  ["server unknown", hook => hook.release({error:{message:"unknown"}}, 503)],
  ["reconciliation required", hook => hook.release({error:{message:"reconcile", code:"QUERY_RECONCILIATION_REQUIRED"}}, 409)],
  ["network failure", hook => hook.reject(new Error("connection lost"))],
]) test(`3-03 operations: ${kind} releases in-flight guard and retains same retry key`, async t => {
  const ui = harness(t, "operations"); await ui.ready();
  const match = c => c.url.endsWith("/run-1/control") && c.method === "POST";
  const first = ui.defer(kind, match);
  await ui.click("step"); await ui.click("step");
  const calls = () => ui.calls.filter(match);
  assert.equal(calls().length, 1, ui.evidence());
  const originalKey = calls()[0].headers["Idempotency-Key"];
  finish(first); await ui.ready();
  await ui.input("step-observation", "unavailable"); await ui.click("step");
  assert.equal(calls().length, 1); assert.match(ui.get("status").textContent, /이전 변경의 결과가 불명확/);
  await ui.input("step-observation", "");
  const retry = ui.defer("same-key retry", match);
  await ui.click("step"); assert.ok(retry.started);
  assert.equal(calls().length, 2); assert.equal(calls()[1].headers["Idempotency-Key"], originalKey);
  retry.release(state("run-1", 3)); await ui.ready();
  const next = ui.defer("next intent", match);
  await ui.click("step"); assert.ok(next.started);
  assert.notEqual(calls()[2].headers["Idempotency-Key"], originalKey);
  next.release(state("run-1", 4)); await ui.ready();
});

for (const [kind, finish] of [
  ["503", hook => hook.release({error:{message:"preview unavailable"}}, 503)],
  ["network", hook => hook.reject(new Error("preview connection lost"))],
]) for (const change of ["command", "serial", "run"]) {
  test(`3-03 operations: stale preview ${kind} after ${change} change preserves current view`, async t => {
    const ui = harness(t, "operations"); await ui.ready();
    await ui.input("command-id", "command-A");
    const old = ui.defer("old preview", c => c.url.endsWith("/commands/command-A"));
    await ui.click("command-preview"); assert.ok(old.started);
    if (change === "command") await ui.input("command-id", "command-B");
    if (change === "run") await ui.click("create-run");
    await ui.click("command-preview");
    const expected = operationView(ui);
    finish(old); await ui.ready();
    assert.deepEqual(operationView(ui), expected, ui.evidence());
  });
}

test("3-03 operations: current preview failure remains visible and confirmation disabled", async t => {
  const ui = harness(t, "operations"); await ui.ready();
  await ui.input("command-id", "command-A");
  const current = ui.defer("current plan failure", c => c.url.endsWith("/commands/command-A/plan"));
  await ui.click("command-preview"); assert.ok(current.started);
  current.release({error:{message:"current plan unavailable"}}, 503); await ui.ready();
  assert.match(ui.get("status").textContent, /current plan unavailable/);
  assert.equal(ui.get("status").dataset.error, "true");
  assert.equal(ui.get("command-confirm").disabled, true);
});
