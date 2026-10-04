"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "code/devtools/operations.html"), "utf8");
const source = fs.readFileSync(path.join(root, "code/devtools/operations.js"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);

function response(data, status=200) {
  return {status, ok:status >= 200 && status < 300, json:async () => data};
}

function harness(role="test_operator", audioOptions={}) {
  const elements = new Map(ids.map(id => [id, {
    id, value:"", textContent:"", disabled:false, dataset:{}, handlers:{},
    addEventListener(event, handler) {this.handlers[event] = handler;},
    setAttribute(name, value) {this[name] = value;},
  }]));
  for (const [id, value] of Object.entries({fixture:"s1a-foundation-v1",seed:"1", "agent-mode":"mock",
    "reaction-mode":"manual", "response-delay":"0", "movement-delay":"0", "s2-delay":"0",
    "s2-mode":"brake_on_alarm", "fault-channel":"visual", "fault-failed":"true",
    "step-vehicle":"", "step-observation":"", "scenario":"", "agent-command-id":"", "command-id":"", "sound-kind":"alarm", "sound-broadcast-id":"broadcast-1"}))
    elements.get(id).value = value;
  const group = selector => [...elements.values()].filter(item => {
    const match = html.match(new RegExp(`<[^>]+id="${item.id}"[^>]*>`));
    return match && match[0].includes(selector.slice(1,-1));
  });
  const calls = [];
  const audioNodes = [];
  const audioContexts = [];
  class AudioContext {
    constructor() {this.state="suspended"; this.currentTime=0; this.destination={}; audioContexts.push(this);}
    async resume() {if (audioOptions.resumePending) await audioOptions.resumePending; if (!audioOptions.blocked) this.state="running";}
    async close() {this.state="closed";}
    createGain() {return {gain:{value:0}, connect() {}, disconnect() {this.disconnected=true;}};}
    createOscillator() {
      const node = {frequency:{value:0}, connect(gain) {this.gainNode=gain;}, disconnect() {this.disconnected=true;},
        start() {if (audioOptions.failStart) throw new Error("play failed");},
        stop(at) {if (at === undefined && audioOptions.failStop) throw new Error("not started");
          if (at === undefined) this.onended?.();}};
      audioNodes.push(node); return node;
    }
  }
  let currentRole = role;
  let stateDeferred = null;
  let failNext = null;
  let timer = null;
  const fetch = async (url, options={}) => {
    calls.push({url, options});
    if (failNext && url.endsWith(failNext)) {failNext = null; return response({error:{message:"temporary failure"}}, 503);}
    if (url === "/health/ready") return response({current_run_id:"run-1"});
    if (url === "/api/v1/me") return currentRole
      ? response({user_id:"user-1", alias:"Tester", csrf_token:"csrf-1",
        facility_roles:[{facility_id:"fac-demo-01", roles:[currentRole]}]})
      : response({error:{message:"unauthorized"}}, 401);
    if (url.includes("/devices?")) return response({run_id:"run-1", alarms:[{desired_active:true}],
      broadcasts:[{operation_id:"broadcast-1", receipt:"accepted", simulated_playback:"played", browser_playback:"not_requested", message_id:"closing_notice"}]});
    if (url.includes("/state?")) {
      if (stateDeferred) return stateDeferred.promise;
      return response({applied_state_version:2, run_id:"run-1"});
    }
    if (url.includes("/test/runs/") && url.endsWith("/control")) return response({run_id:"run-1"});
    if (url.endsWith("/test/runs")) return response({run_id:"run-2"});
    if (url.includes("/commands/") && url.endsWith("/plan")) return response({steps:[]});
    if (url.includes("/commands/") && options.method !== "POST") return response({resource_version:1});
    return response({ok:true});
  };
  vm.runInNewContext(source, {document:{getElementById:id => elements.get(id), querySelectorAll:group},
    fetch, crypto:{randomUUID:()=>`key-${calls.length}`}, setInterval:callback => {timer = callback;},
    console, URL, Promise, AudioContext});
  const flush = () => new Promise(resolve => setImmediate(resolve));
  return {elements, calls, flush, audioNodes, audioContexts, endSound:() => audioNodes.at(-1)?.onended?.(), tick:async () => {timer(); await flush();},
    click:async id => {elements.get(id).handlers.click(); await flush(); await flush();},
    revoke:() => {currentRole = null;}, setRole:next => {currentRole = next;},
    failOnce:suffix => {failNext = suffix;},
    deferState:() => {
      let resolve;
      const promise = new Promise(done => {resolve = done;});
      stateDeferred = {promise, resolve};
      return () => {stateDeferred = null; resolve(response({applied_state_version:99, secret:"stale"}));};
    },
  };
}

test("temporary console declares synthetic scope and separated main console", () => {
  assert.match(html, /임시 개발 콘솔 · 합성 SIM-0 시험/);
  assert.match(html, /href="\/devtools"/);
  assert.match(html, /서버의 browser_playback 값을 변경하지 않습니다/);
});

test("owner cannot create or control a run while mock remains default", async () => {
  const ui = harness("owner");
  await ui.flush(); await ui.flush();
  assert.equal(ui.elements.get("create-run").disabled, true);
  assert.equal(ui.elements.get("step").disabled, true);
  assert.equal(ui.elements.get("agent-start").disabled, false);
  assert.equal(ui.elements.get("agent-mode").value, "mock");
});

test("create run sends required fixture contract and step sends selected synthetic controls", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  await ui.click("create-run");
  const create = ui.calls.find(call => call.url.endsWith("/test/runs") && call.options.method === "POST");
  assert.deepEqual(JSON.parse(create.options.body), {facility_id:"fac-demo-01", fixture_ref:"s1a-foundation-v1",
    config_ref:"foundation-v1", seed:1});
  ui.elements.get("step-vehicle").value = "request_vehicle_move";
  ui.elements.get("step-observation").value = "occluded_vehicle";
  await ui.click("step");
  const step = ui.calls.find(call => call.url.endsWith("/control") && call.options.method === "POST");
  assert.deepEqual(JSON.parse(step.options.body), {action:"step", action_params:{request_vehicle_move:"obj-car-02",
    observation_mode:"occluded_vehicle"}});
});

test("role revocation erases data and discards a stale state response", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  const release = ui.deferState();
  await ui.click("refresh");
  ui.revoke(); await ui.tick();
  release(); await ui.flush(); await ui.flush();
  assert.equal(ui.elements.get("state").textContent, "자료 없음");
  assert.equal(ui.elements.get("create-run").disabled, true);
  assert.match(ui.elements.get("status").textContent, /권한 변경/);
});

test("role change clears old run and disables operator controls", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  ui.setRole("owner"); await ui.tick();
  assert.equal(ui.elements.get("state").textContent, "자료 없음");
  assert.equal(ui.elements.get("step").disabled, true);
  assert.match(ui.elements.get("status").textContent, /역할/);
});

test("uncertain mutation retries the identical idempotency key", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  ui.failOnce("/control");
  await ui.click("step");
  await ui.click("step");
  const attempts = ui.calls.filter(call => call.url.endsWith("/control") && call.options.method === "POST");
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].options.headers["Idempotency-Key"], attempts[1].options.headers["Idempotency-Key"]);
});

test("device fault and S2 reaction are versioned synthetic inputs", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  await ui.click("save-fault");
  await ui.click("save-s2");
  const fault = ui.calls.find(call => call.url.endsWith("/device-faults") && call.options.method === "PUT");
  const s2 = ui.calls.find(call => call.url.endsWith("/s2-reaction") && call.options.method === "PUT");
  assert.deepEqual(JSON.parse(fault.options.body), {expected_state_version:2,channel:"visual",failed:true});
  assert.deepEqual(JSON.parse(s2.options.body), {expected_state_version:2,mode:"brake_on_alarm",delay_ms:0});
});

test("command and S3 passage controls use the actual server input contract", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  ui.elements.get("command-text").value = "영업 종료, 입차 제한하고 출차는 유지";
  await ui.click("command-create");
  const command = ui.calls.find(call => call.url.endsWith("/commands") && call.options.method === "POST");
  assert.deepEqual(JSON.parse(command.options.body), {run_id:"run-1",based_on_state_version:2,
    text:"영업 종료, 입차 제한하고 출차는 유지",purpose:"operational_goal"});
  ui.elements.get("step-vehicle").value = "portal_entry";
  ui.elements.get("step-observation").value = "";
  await ui.click("step");
  const attempt = ui.calls.find(call => call.url.endsWith("/control") && call.options.method === "POST");
  assert.deepEqual(JSON.parse(attempt.options.body), {action:"step",action_params:{request_portal_attempt:"obj-car-s3-u"}});
});


test("browser signal requires an explicit gesture and never reports device playback", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  assert.equal(ui.audioContexts.length, 0);
  assert.equal(ui.audioNodes.length, 0);
  await ui.click("sound-enable"); await ui.click("sound-test");
  assert.equal(ui.audioNodes.length, 1);
  assert.equal(ui.elements.get("sound-status").dataset.phase, "playing");
  ui.endSound();
  assert.equal(ui.elements.get("sound-status").dataset.phase, "ended_unconfirmed");
  assert.match(ui.elements.get("sound-status").textContent, /들림은 미확인/);
  assert.equal(ui.calls.filter(call => call.options.method === "POST").length, 0);
});

test("pending browser permission displays a blocked boundary before resume resolves", async () => {
  let allow;
  const ui = harness("test_operator", {resumePending:new Promise(resolve => {allow = resolve;})});
  await ui.flush(); await ui.flush(); await ui.click("sound-enable");
  assert.equal(ui.elements.get("sound-status").dataset.phase, "blocked");
  assert.match(ui.elements.get("sound-status").textContent, /허용 대기.*차단/);
  assert.equal(ui.elements.get("sound-test").disabled, true);
  assert.equal(ui.audioNodes.length, 0);
  allow(); await ui.flush(); await ui.flush();
  assert.equal(ui.elements.get("sound-status").dataset.phase, "enabled");
});

test("autoplay denial and failed signal remain explicit failures", async () => {
  const blocked = harness("test_operator", {blocked:true}); await blocked.flush(); await blocked.flush();
  await blocked.click("sound-enable");
  assert.equal(blocked.elements.get("sound-status").dataset.phase, "blocked");
  assert.equal(blocked.elements.get("sound-test").disabled, true);
  const failed = harness("test_operator", {failStart:true}); await failed.flush(); await failed.flush();
  await failed.click("sound-enable"); await failed.click("sound-test");
  assert.equal(failed.elements.get("sound-status").dataset.phase, "failed");
});

test("mute stop and role revocation suppress late playback completion", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  await ui.click("sound-enable"); await ui.click("sound-test");
  const late = ui.audioNodes.at(-1).onended;
  await ui.click("sound-stop"); late();
  assert.equal(ui.audioNodes[0].disconnected, true);
  assert.equal(ui.audioNodes[0].gainNode.disconnected, true);
  assert.equal(ui.elements.get("sound-status").dataset.phase, "stopped");
  await ui.click("sound-mute");
  assert.equal(ui.elements.get("sound-mute")["aria-pressed"], "true");
  assert.equal(ui.elements.get("sound-test").disabled, true);
  await ui.click("sound-mute"); await ui.click("sound-test");
  const oldEnd = ui.audioNodes.at(-1).onended;
  ui.revoke(); await ui.tick(); oldEnd();
  assert.equal(ui.audioContexts[0].state, "closed");
  assert.equal(ui.elements.get("sound-status").dataset.phase, "stopped");
  assert.equal(ui.elements.get("sound-broadcast-id").value, "");
});

test("current alarm and selected broadcast cues recheck state without creating commands", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  await ui.click("sound-enable"); await ui.click("sound-current");
  assert.equal(ui.elements.get("sound-status").dataset.phase, "playing");
  ui.elements.get("sound-kind").value = "broadcast";
  await ui.click("sound-current");
  assert.match(ui.elements.get("sound-status").textContent, /영업 종료 안내/);
  assert.equal(ui.calls.filter(call => call.options.method === "POST").length, 0);
});

test("failed state lookup and invalid broadcast cannot produce a cue", async () => {
  const ui = harness(); await ui.flush(); await ui.flush();
  await ui.click("sound-enable");
  ui.failOnce("/devices?run_id=run-1"); await ui.click("sound-current");
  assert.equal(ui.audioNodes.length, 0);
  assert.equal(ui.elements.get("sound-status").dataset.phase, "failed");
  ui.elements.get("sound-kind").value = "broadcast";
  ui.elements.get("sound-broadcast-id").value = "old-run-broadcast";
  await ui.click("sound-current");
  assert.equal(ui.audioNodes.length, 0);
  assert.equal(ui.elements.get("sound-status").dataset.phase, "failed");
});


test("failed oscillator start and stop still disconnect both audio nodes", async () => {
  const ui = harness("test_operator", {failStart:true, failStop:true});
  await ui.flush(); await ui.flush(); await ui.click("sound-enable"); await ui.click("sound-test");
  assert.equal(ui.elements.get("sound-status").dataset.phase, "failed");
  assert.equal(ui.audioNodes[0].disconnected, true);
  assert.equal(ui.audioNodes[0].gainNode.disconnected, true);
});
