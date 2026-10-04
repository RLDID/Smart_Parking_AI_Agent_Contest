"use strict";
const $ = (id) => document.getElementById(id);
const facility = "fac-demo-01";
let csrf = "", runId = null, stream = null, map = null, lastMessage = 0, role = null;
let lastState = null, requestPending = false;
let uncertainRequest = null;
let lastEventId = "";
let analysisRequest = 0;

function clearAnalysis(message = "관측 연결 대기") {
  analysisRequest++;
  $("analysis-status").textContent = message;
  $("analysis-values").textContent = "";
  $("analysis-json").textContent = "분석 없음";
}
async function refreshAnalysis() {
  if (role === "driver") {clearAnalysis("차주 화면 · 시설 전체 분석은 제공하지 않습니다."); return;}
  const request = ++analysisRequest, requestedRun = runId;
  try {
    const result = await api(`/facilities/${facility}/spatial-analysis?run_id=${encodeURIComponent(requestedRun)}`);
    if (request !== analysisRequest || requestedRun !== runId) return;
    const m = result.metrics;
    const label = {clear:"통과 공간 있음", blocked:"지원 직선 경로 차단", unknown:"확인 필요"}[m.passage];
    $("analysis-status").textContent = `${label} · ${result.support_status}`;
    $("analysis-json").textContent = JSON.stringify(result, null, 2);
    $("analysis-values").replaceChildren();
    const values = result.support_status === "supported" ? [
      `연속 통과 폭 ${m.available_clearance_m.toFixed(2)} m / 필요한 폭 ${m.required_clearance_m.toFixed(2)} m`,
      `차단 유지 ${m.blocked_duration_ms} ms · 통과 공간 유지 ${m.clear_duration_ms} ms`,
      `3초 공간 유지: ${m.clearance_sustained ? "확인" : "대기"} · 사건 해결 여부는 별도`,
      ...m.objects.filter(o => m.occupied_object_ids.includes(o.object_id)).map(o =>
        `${o.object_id}: 정지 관측 ${o.stop_duration_ms} ms${o.stationary_candidate ? " · 5초 정지 후보" : ""}`),
    ] : [`근거 부족/지원 한계: ${result.quality.reasons.join(", ")}`];
    for (const value of values) {const line=document.createElement("li"); line.textContent=value; $("analysis-values").append(line);}
  } catch (error) {
    if (request === analysisRequest) clearAnalysis(`분석 조회 실패: ${error.message}`);
  }
}

function connection(message, error = false) {
  $("connection").textContent = message;
  $("connection").dataset.status = error ? "error" : "ok";
}
async function api(path, method = "GET", body, key) {
  const generation = sessionGeneration;
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(`/api/v1${path}`, {method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body)});
  if (response.status === 204) return;
  const result = await response.json();
  if (generation !== sessionGeneration) throw new Error("요청 중 세션이 바뀌어 이전 응답을 표시하지 않습니다.");
  if (!response.ok) {
    const error = new Error(`${result.error?.code || response.status}: ${result.error?.message || "요청 실패"}`);
    error.status = response.status; throw error;
  }
  return result;
}
async function mutate(path, body) {
  const signature = JSON.stringify({path, body});
  if (uncertainRequest && uncertainRequest.signature !== signature)
    throw new Error("이전 요청의 결과가 미확인입니다. 같은 버튼으로 먼저 재확인하세요.");
  uncertainRequest ??= {signature, key: crypto.randomUUID()};
  try {
    const result = await api(path, "POST", body, uncertainRequest.key);
    uncertainRequest = null; return result;
  } catch (error) {
    if (error.status && error.status < 500 && error.status !== 408) uncertainRequest = null;
    throw error;
  }
}
let sessionGeneration = 0, businessRequest = 0, selectedIncident = null, manualResult = null, businessSyncNeeded = true;
const receipts = new Map(), responseRequests = new Map();

function clearBusiness() {
  businessRequest++; selectedIncident=null; manualResult=null; receipts.clear(); responseRequests.clear(); businessSyncNeeded=true;
  $("incident-list").replaceChildren(); $("notification-list").textContent="인증 후 조회";
  $("business-status").textContent="인증 후 업무 연결"; $("manual-result").textContent="실행 없음";
  $("agent-result").textContent="실행 없음";
  $("model-status").textContent="인증 후 제공자 · 비용 확인";
}
async function refreshBusiness() {
  if (!role) return;
  const request = ++businessRequest, requestedRole = role, requestedRun = runId;
  const current = () => request === businessRequest && requestedRole === role && requestedRun === runId;
  try {
    const inbox = await api("/notifications?limit=100");
    const incidents = role === "driver" || !runId ? {items:[]} : await api(`/facilities/${facility}/incidents?run_id=${encodeURIComponent(runId)}&limit=100`);
    if (!current()) return;
    $("incident-list").replaceChildren();
    selectedIncident = incidents.items.find(i=>!["resolved","closed_no_issue","closed_false_positive"].includes(i.status))?.incident_id || incidents.items.at(-1)?.incident_id || null;
    for (const incident of incidents.items) {
      const item=document.createElement("li"); item.textContent=`${incident.primary_object_id}: ${incident.status} · 버전 ${incident.resource_version}`;
      $("incident-list").append(item);
    }
    $("notification-list").replaceChildren();
    if (!inbox.items.length) $("notification-list").textContent="현재 권한으로 수신한 메시지 없음";
    for (const notice of inbox.items) {
      const card=document.createElement("article"), title=document.createElement("p"), state=document.createElement("small"), replies=document.createElement("p"), buttons=document.createElement("div");
      title.textContent=notice.message.text || notice.message.summary || "가상 메시지";
      state.textContent=`${notice.delivery_status} · ${notice.mode} · 응답 기한 ${notice.response_due_at || "—"}`;
      replies.textContent=notice.responses.map(r=>`응답 ${r.response}`).join(" · ") || "아직 응답 기록 없음";
      buttons.className="buttons";
      for (const [value,label] of [["acknowledged","확인"],["will_move","이동하겠습니다"],["cannot_move","이동 어려움"],["question","문의"]]) {
        const button=document.createElement("button"); button.textContent=label; button.dataset.reply="";
        button.addEventListener("click",()=>perform(async()=>{
          const requestKey=`${notice.notification_id}:${value}`;
          if (!responseRequests.has(requestKey)) responseRequests.set(requestKey,crypto.randomUUID());
          await mutate(`/notifications/${notice.notification_id}/responses`,{client_request_id:responseRequests.get(requestKey),response:value});
          responseRequests.delete(requestKey); await refreshBusiness();
        })); buttons.append(button);
      }
      card.append(title,state,replies,buttons); $("notification-list").append(card);
      // Only a message actually appended to this authenticated screen gets a
      // receipt. Stable IDs survive transport retry within the current session.
      if (notice.delivery_status === "channel_accepted") {
        if (!receipts.has(notice.notification_id)) receipts.set(notice.notification_id,{key:crypto.randomUUID(),client_request_id:crypto.randomUUID(),received_at:new Date().toISOString(),done:false});
        const receipt=receipts.get(notice.notification_id);
        if (!receipt.done && !receipt.pending) {
          receipt.pending=true;
          api(`/notifications/${notice.notification_id}/receipts`,"POST",{client_request_id:receipt.client_request_id,received_at:receipt.received_at},receipt.key)
            .then(()=>{receipt.done=true; if (current()) state.textContent=`화면 수신 기록 완료 · ${notice.mode}`;})
            .catch(error=>{if (current()) state.textContent=`수신 기록 미확인: ${error.message}`;})
            .finally(()=>{receipt.pending=false;});
        }
      }
    }
    if (manualResult?.execution) {
      manualResult.execution=await api(`/executions/${manualResult.execution.execution_id}`);
      if (!current()) return;
      $("manual-result").textContent=JSON.stringify(manualResult,null,2);
    }
    $("business-status").textContent=`사건 ${incidents.items.length} · 내 메시지 ${inbox.items.length} · 현재 권한으로 갱신됨`;
    controls();
  } catch (error) {
    if (!current()) return;
    $("business-status").textContent=`업무 조회 실패: ${error.message}`;
    if ([401,403].includes(error.status)) {clearDisplayedState(); connection("업무 권한 만료 · 다시 로그인하세요.",true);}
  }
}

function controls() {
  document.querySelector('#login-form button[type="submit"]').disabled = requestPending;
  $("logout").disabled = !role || requestPending;
  for (const button of document.querySelectorAll("[data-control]"))
    button.disabled = role !== "test_operator" || requestPending || (button.id !== "new-run" && !runId);
  if (lastState?.run_status === "running") {
    $("new-run").disabled = true; $("step").disabled = true; $("move").disabled = true; $("apply-observation").disabled = true;
  }
  for (const button of document.querySelectorAll("[data-manual]"))
    button.disabled = role !== "test_operator" || requestPending || !runId || lastState?.run_status !== "paused" || (button.id !== "manual-notify" && !selectedIncident);
  for (const button of document.querySelectorAll("[data-reply]")) button.disabled=!role || requestPending;
  $("refresh-business").disabled=!role || requestPending;
  $("agent-submit").disabled=!role || !runId || requestPending ||
    ($("agent-provider").value && $("agent-provider").value !== "mock" && lastState?.run_status !== "paused");
  $("refresh-models").disabled=!role || requestPending;
}
function clearDisplayedState() {
  sessionGeneration++;
  stream?.close(); stream=null; csrf=""; role=null; runId=null; lastState=null; map=null;
  uncertainRequest=null;
  clearBusiness();
  $("geometry").replaceChildren(); $("snapshot").textContent="관측 없음";
  $("vehicles").textContent=""; $("view-scope").textContent="인증 후 표시 범위를 확인합니다.";
  $("identity").textContent="인증되지 않음";
  for (const id of ["run-status","run-id","sim-time","obs-time","received","version","coverage"]) $(id).textContent="—";
  clearAnalysis(); controls();
}
function svgElement(tag, attributes) {
  const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) item.setAttribute(name, value);
  return item;
}
function draw(snapshot) {
  const group = $("geometry"); group.replaceChildren();
  if (map) for (const zone of map.zones) {
    if (zone.type === "announcement") continue;
    const fill = zone.type === "parking_bay" ? "#e9f1f4" : zone.type === "pedestrian" ? "#fff0d7" : "#e1e6eb";
    group.append(svgElement("polygon", {points: zone.polygon.map(p => `${p.x},${p.y}`).join(" "), fill, stroke: "#9caeb8", "stroke-width": .07}));
  }
  for (const object of snapshot.objects) {
    if (!object.position || !object.size) continue;
    const {x,y} = object.position, {length_m: l,width_m: w} = object.size;
    const item = svgElement("rect", {x: -l/2, y: -w/2, width:l, height:w, rx:.14,
      transform:`translate(${x},${y}) rotate(${object.heading_deg})`,
      fill: object.object_type === "vehicle" ? "#1e6c9a" : "#d97328"});
    const title = svgElement("title", {}); title.textContent = object.object_id;
    item.append(title); group.append(item);
    const label = svgElement("text", {x:x+.4, y:-y-.5, transform:"scale(1,-1)", "font-size":.65, fill:"#142633"});
    label.textContent = object.object_id.replace("obj-", ""); group.append(label);
  }
}
function render(state, allowRunChange = false) {
  if (lastState && state.snapshot.run_id !== lastState.snapshot.run_id && !allowRunChange) return;
  if (lastState && state.snapshot.run_id === lastState.snapshot.run_id
      && state.applied_state_version < lastState.applied_state_version) return;
  lastState = state;
  const snapshot = state.snapshot; runId = snapshot.run_id;
  $("run-status").textContent = `${state.run_status}${state.recovery_required ? " · 복구 확인 필요" : ""}`;
  $("run-id").textContent = runId;
  $("sim-time").textContent = `${state.applied_sim_time_ms} ms`;
  $("obs-time").textContent = `${snapshot.sim_time_ms} ms`;
  $("received").textContent = snapshot.received_at;
  $("version").textContent = `${snapshot.state_version} (적용 ${state.applied_state_version})`;
  $("snapshot").textContent = JSON.stringify(snapshot, null, 2);
  $("coverage").textContent = snapshot.coverage;
  $("view-scope").textContent = state.view_scope === "own_vehicles"
    ? "본인 등록 차량만 표시 · 보이지 않는 차량의 출차/부재를 뜻하지 않습니다."
    : "시설 관측 조회";
  draw(snapshot); controls();
  refreshAnalysis();
}
function connect() {
  stream?.close();
  clearBusiness();
  lastEventId = "";
  stream = new EventSource(`/api/v1/facilities/${facility}/events?run_id=${encodeURIComponent(runId)}`);
  const activeStream = stream;
  stream.onopen = () => {
    if (stream !== activeStream) return;
    // Opening the transport does not refresh the observation or its timestamp.
    connection(lastState ? "서버 연결됨 · 마지막 관측 표시 중 · 새 관측 수신 대기" : "서버 연결됨 · 새 스냅샷 대기");
    clearAnalysis("서버 연결됨 · 새 관측 수신 후 분석 갱신");
  };
  for (const type of ["state.snapshot", "run.updated"]) stream.addEventListener(type, event => {
    if (stream !== activeStream) return;
    const data = JSON.parse(event.data);
    if (event.lastEventId === lastEventId) return;
    lastEventId = event.lastEventId;
    lastMessage = Date.now(); render(data.payload);
    if (businessSyncNeeded) {businessSyncNeeded=false; refreshBusiness();}
    connection(data.payload.run_status === "paused" ? "서버 연결됨 · 일시정지 관측" : "서버 연결됨 · 가상 관측 갱신 중");
  });
  stream.addEventListener("reset_required", () => {
    if (stream !== activeStream) return;
    lastEventId = "";
    lastState=null; $("geometry").replaceChildren(); $("snapshot").textContent="새 스냅샷 대기";
    for (const id of ["run-status","run-id","sim-time","obs-time","received","version","coverage"]) $(id).textContent="—";
    controls();
    clearAnalysis("새 관측 동기화 대기");
    clearBusiness();
    connection("실행/커서 변경 · 새 스냅샷 동기화 중");
  });
  for (const type of ["incident.updated","notification.updated","execution.updated","command.updated","plan.updated","followup.updated"]) stream.addEventListener(type,()=>{
    if (stream === activeStream) refreshBusiness();
  });
  stream.addEventListener("access.revoked", () => {
    if (stream !== activeStream) return;
    clearDisplayedState();
    connection("권한 변경/만료 · 표시 데이터를 지웠습니다. 다시 로그인하세요.", true);
  });
  stream.onerror = async () => {
    if (stream !== activeStream) return;
    connection("연결 끊김 · 마지막 관측 표시 중 · 재연결 대기", true);
    clearAnalysis("연결 끊김 · 현재 분석 확인 필요");
    try {await api("/me");} catch (error) {
      if (stream === activeStream && [401,403].includes(error.status)) {
        clearDisplayedState(); connection("세션/권한 만료 · 다시 로그인하세요.", true);
      }
    }
  };
}
async function perform(action) {
  if (requestPending) return;
  $("message").textContent = ""; requestPending=true; controls();
  try {await action();} catch (error) {$("message").textContent=error.message; if (!role) connection("인증을 확인하세요.", true);}
  finally {requestPending=false; controls();}
}
$("login-form").addEventListener("submit", event => {event.preventDefault(); perform(async () => {
  clearDisplayedState();
  connection("로그인 확인 중");
  await api("/auth/session", "POST", {username:$("username").value, password:$("password").value});
  const identity=await api("/me"); csrf=identity.csrf_token; role=identity.facility_roles[0].roles[0];
  $("identity").textContent=`${identity.alias} · ${role}`; $("logout").disabled=false;
  map=role === "driver" ? null : await api(`/facilities/${facility}/map`);
  const ownVehicles=await api(`/me/vehicles?facility_id=${facility}`);
  $("vehicles").textContent=role === "driver"
    ? `본인 등록 차량: ${ownVehicles.vehicles.map(v=>v.display_alias).join(", ") || "연결된 차량 없음"}` : "";
  const readiness=await (await fetch("/health/ready")).json(); runId=readiness.current_run_id;
  if (runId) {render(await api(`/facilities/${facility}/state?run_id=${runId}`)); connect();}
  else connection("인증 완료 · 시험 운영자가 새 실행을 생성하세요.");
});});
$("logout").addEventListener("click", () => perform(async () => {
  try {await api("/auth/session", "DELETE");}
  catch {throw new Error("화면 데이터는 지웠으나 서버 로그아웃은 확인하지 못했습니다. 연결 후 다시 로그인하여 세션을 교체하세요.");}
  finally {clearDisplayedState();}
  connection("로그아웃됨", true);
}));
$("new-run").addEventListener("click", () => perform(async () => {
  render(await mutate("/test/runs", {facility_id:facility, fixture_ref:"s1a-foundation-v1", seed:1, config_ref:"foundation-v1"}), true);
  connect();
}));
for (const action of ["start","pause","step","move"]) $(action).addEventListener("click", () => perform(async () => {
  const body = {action: action === "move" ? "step" : action};
  if (action === "move") body.action_params={request_vehicle_move:"obj-car-02"};
  render(await mutate(`/test/runs/${runId}/control`, body));
}));
$("apply-observation").addEventListener("click", () => perform(async () => {
  render(await mutate(`/test/runs/${runId}/control`, {action:"step", action_params:{observation_mode:$("observation-mode").value}}));
}));
for (const [id,action] of [["manual-notify","notify"],["manual-recheck","recheck"],["manual-timeout","review_timeout"]]) $(id).addEventListener("click",()=>perform(async()=>{
  const generation = sessionGeneration, requestedRun = runId;
  const body={run_id:runId,action}; if (action !== "notify") body.incident_id=selectedIncident;
  const result=await mutate("/test/s1a/manual",body);
  if (generation !== sessionGeneration || requestedRun !== runId) return;
  manualResult=result;
  $("manual-result").textContent=JSON.stringify(manualResult,null,2);
  await refreshBusiness();
}));
$("refresh-business").addEventListener("click",()=>perform(refreshBusiness));
$("agent-provider").addEventListener("change",()=>{ $("agent-result").textContent="실행 없음"; controls(); });
$("refresh-models").addEventListener("click",()=>perform(async()=>{
  const generation=sessionGeneration, requestedRun=runId;
  const result=await api("/test/agent/config");
  if (generation !== sessionGeneration || requestedRun !== runId) return;
  $("model-status").textContent=JSON.stringify(result,null,2);
}));
$("agent-form").addEventListener("submit",event=>{
  event.preventDefault();
  return perform(async()=>{
    const generation=sessionGeneration, requestedRun=runId;
    const provider=$("agent-provider").value || "mock";
    const body={run_id:runId,goal:$("agent-goal").value,query:$("agent-query").value};
    if (provider !== "mock") body.provider=provider;
    const result=await mutate(provider === "mock" ? "/test/agent/queries" : "/test/agent/live-queries",body);
    if (generation !== sessionGeneration || requestedRun !== runId) return;
    $("agent-result").textContent=JSON.stringify(result,null,2);
  });
});
setInterval(() => {
  if (stream && lastState?.run_status === "running" && Date.now()-lastMessage>2500) {
    connection("관측 갱신 지연 · 마지막 데이터 표시 중", true);
    clearAnalysis("관측 연결 지연 · 현재 분석 확인 필요");
  }
}, 500);
