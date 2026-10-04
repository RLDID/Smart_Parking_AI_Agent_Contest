"use strict";
(() => {
  const facility = "fac-demo-01";
  const api = "/api/v1";
  const el = id => document.getElementById(id);
  const value = id => el(id).value.trim();
  const text = (id, data) => { el(id).textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2); };
  let identity = null;
  let csrf = null;
  let runId = null;
  let stateVersion = null;
  let commandVersion = null;
  let previewedId = null;
  let generation = 0;
  let viewSerial = 0;
  let previewSerial = 0;
  const pending = new Map();
  let soundContext = null;
  let soundNode = null;
  let soundGain = null;
  let soundEnabled = false;
  let soundMuted = false;
  let soundEpoch = 0;

  function buttons() {
    const manager = identity && (identity.role === "owner" || identity.role === "test_operator");
    document.querySelectorAll("[data-manager]").forEach(node => {node.disabled = !manager;});
    document.querySelectorAll("[data-operator]").forEach(node => {node.disabled = !manager || identity.role !== "test_operator";});
    el("command-confirm").disabled = !manager || previewedId !== value("command-id");
    el("sound-test").disabled = !manager || !soundEnabled || soundMuted;
    el("sound-current").disabled = !manager || !runId || !soundEnabled || soundMuted;
    el("sound-mute").setAttribute("aria-pressed", String(soundMuted));
  }

  function clear(message) {
    generation += 1;
    viewSerial += 1;
    previewSerial += 1;
    stopSound("세션·권한 변경으로 신호를 중단했습니다.", "stopped", true);
    el("sound-broadcast-id").value = "";
    identity = null; csrf = null; runId = null; stateVersion = null;
    commandVersion = null; previewedId = null; pending.clear();
    for (const id of ["state","reaction","agent-state","jobs","devices","command","plan"]) text(id, "자료 없음");
    text("identity", "인증되지 않음");
    el("status").dataset.error = "true";
    text("status", message);
    buttons();
  }

  function notice(message, error=false) {
    el("status").dataset.error = String(error);
    text("status", message);
  }

  async function request(path, options={}) {
    const captured = generation;
    const response = await fetch(path, {credentials:"same-origin", cache:"no-store", ...options});
    if (response.status === 401 || response.status === 403) {
      clear("로그아웃 또는 권한 변경을 확인했습니다. 기존 시험 자료를 지웠습니다.");
      throw new Error("현재 접근 권한이 없습니다.");
    }
    const result = response.status === 204 ? {} : await response.json();
    if (captured !== generation) throw new Error("이전 세션 응답을 폐기했습니다.");
    if (!response.ok) {
      const error = new Error(result.error?.message || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = result.error?.code;
      throw error;
    }
    return result;
  }

  async function mutate(action, path, body, method="POST", isCurrent=() => identity !== null) {
    if (!csrf || !identity) throw new Error("관리 세션이 필요합니다.");
    const signature = JSON.stringify({path, body, method});
    let saved = pending.get(action);
    if (saved && saved.signature !== signature)
      throw new Error("이전 변경의 결과가 불명확합니다. 같은 입력·요청 키로 재확인하거나 목록을 조회하세요.");
    if (saved?.inFlight) throw new Error("같은 변경 요청이 진행 중입니다. 결과를 기다린 뒤 다시 확인하세요.");
    if (!saved) {
      saved = {key:crypto.randomUUID(), signature};
      pending.set(action, saved);
    }
    saved.inFlight = true;
    try {
      const result = await request(path, {method, headers:{"Content-Type":"application/json",
        "X-CSRF-Token":csrf, "Idempotency-Key":saved.key}, body:JSON.stringify(body)});
      if (pending.get(action) === saved) pending.delete(action);
      return result;
    } catch(error) {
      if (pending.get(action) === saved && error.status && error.status < 500 &&
          error.code !== "QUERY_RECONCILIATION_REQUIRED") pending.delete(action);
      if (!isCurrent()) {error.staleContext = true; throw error;}
      if (!error.status || error.status >= 500 || error.code === "QUERY_RECONCILIATION_REQUIRED") {
        notice("변경 결과를 확인하지 못했습니다. 상태·작업을 조회한 뒤 같은 입력으로 다시 누르면 같은 요청 키를 사용합니다.", true);
        if (action === "create-run") {
          try {
            const health = await request("/health/ready");
            if (isCurrent() && health.current_run_id) runId = health.current_run_id;
          } catch (_) { /* Unknown creation keeps its request key. */ }
        }
        if (isCurrent()) try { await refreshAll(isCurrent); } catch (_) { /* Retain the original request key. */ }
      }
      if (!isCurrent()) error.staleContext = true;
      throw error;
    } finally {
      saved.inFlight = false;
    }
  }

  function currentRunContext() {
    const selected = runId, captured = generation;
    return () => identity !== null && selected === runId && captured === generation;
  }

  function managerRole(me) {
    const roles = me.facility_roles?.find(item => item.facility_id === facility)?.roles || [];
    return roles.includes("test_operator") ? "test_operator" : roles.includes("owner") ? "owner" : null;
  }

  async function authenticate() {
    const me = await request(`${api}/me`);
    const role = managerRole(me);
    if (!role) {clear("관리 권한이 없습니다."); return false;}
    if (identity && (identity.user_id !== me.user_id || identity.role !== role || csrf !== me.csrf_token)) {
      clear("계정·역할·세션이 바뀌어 이전 자료를 지웠습니다. 화면을 다시 여세요.");
      return false;
    }
    identity = {user_id:me.user_id, role};
    csrf = me.csrf_token;
    text("identity", `${me.alias || me.user_id} · ${role} · ${runId || "run 미선택"}`);
    buttons();
    return true;
  }

  async function refreshState() {
    if (!runId) {text("state", "run이 없습니다. 시험 운영자가 새 가상 run을 만드세요."); return;}
    const serial = ++viewSerial;
    const selected = runId;
    const result = await request(`${api}/facilities/${facility}/state?run_id=${encodeURIComponent(selected)}`);
    if (serial !== viewSerial || selected !== runId || !identity) return;
    stateVersion = result.applied_state_version;
    text("state", result);
  }

  async function refreshDevices() {
    if (!runId) return;
    const selected = runId;
    const result = await request(`${api}/facilities/${facility}/devices?run_id=${encodeURIComponent(selected)}`);
    if (selected === runId && identity) text("devices", result);
  }

  async function refreshReaction() {
    if (!runId) return;
    const selected = runId;
    const result = await request(`${api}/test/runs/${encodeURIComponent(selected)}/synthetic-users`);
    if (selected === runId && identity) text("reaction", result);
  }

  async function refreshAgent() {
    if (!runId) return;
    const selected = runId;
    const [status,jobs] = await Promise.all([
      request(`${api}/test/agent/operations?run_id=${encodeURIComponent(selected)}`),
      request(`${api}/test/agent/operations/jobs?run_id=${encodeURIComponent(selected)}`)]);
    if (selected === runId && identity) {text("agent-state", status); text("jobs", jobs);}
  }

  async function refreshAll(isCurrent=() => identity !== null) {
    if (!isCurrent()) return;
    const results = await Promise.allSettled([refreshState(), refreshDevices(), refreshReaction(), refreshAgent()]);
    const failed = results.find(item => item.status === "rejected");
    if (failed && isCurrent()) notice(failed.reason.message, true);
  }

  async function bootstrap() {
    const health = await request("/health/ready");
    if (!(await authenticate())) return;
    runId = health.current_run_id || null;
    text("identity", `${identity.user_id} · ${identity.role} · ${runId || "run 미선택"}`);
    await refreshAll();
  }

  async function createRun() {
    const fixture_ref = value("fixture");
    const seed = Number(value("seed"));
    if (!Number.isInteger(seed) || seed < 0 || seed > 2147483647) throw new Error("seed 범위를 확인하세요.");
    const isCurrent = currentRunContext();
    const result = await mutate("create-run", `${api}/test/runs`, {facility_id:facility, fixture_ref,
      config_ref:fixture_ref === "s1a-foundation-v1" ? "foundation-v1" : "sim0-v1", seed}, "POST", isCurrent);
    if (!isCurrent()) return;
    stopSound("새 회차로 바뀌어 이전 신호를 중단했습니다.", "stopped");
    runId = result.run_id;
    stateVersion = null; commandVersion = null; previewedId = null;
    ++viewSerial; ++previewSerial;
    text("identity", `${identity.user_id} · ${identity.role} · ${runId}`);
    notice(`새 가상 run ${runId} 생성`);
    await refreshAll();
  }

  async function control(action) {
    if (!runId) throw new Error("run을 먼저 만드세요.");
    const current = runId;
    const isCurrent = currentRunContext();
    const action_params = {};
    if (action === "step") {
      const vehicleAction = value("step-vehicle");
      if (vehicleAction === "portal_entry" || vehicleAction === "portal_exit") {
        action_params.request_portal_attempt = vehicleAction === "portal_entry" ? "obj-car-s3-u" : "obj-car-s3-w";
      } else if (vehicleAction) action_params[vehicleAction] = vehicleAction === "request_vehicle_departure" ? "obj-car-01" : "obj-car-02";
      if (value("step-observation")) action_params.observation_mode = value("step-observation");
    }
    const result = await mutate(`control:${action}:${current}`, `${api}/test/runs/${encodeURIComponent(current)}/control`,
      {action, action_params:Object.keys(action_params).length ? action_params : null}, "POST", isCurrent);
    if (!isCurrent()) return;
    if (result.run_id && result.run_id !== runId) {
      stopSound("회차가 바뀌어 이전 신호를 중단했습니다.", "stopped");
      runId = result.run_id; ++viewSerial;
    }
    notice(`${action} 요청 결과 확인`);
    await refreshAll();
  }

  async function reaction() {
    if (!runId || stateVersion === null) throw new Error("현재 관측을 먼저 읽으세요.");
    const body = {mode:value("reaction-mode"), response_delay_ms:Number(value("response-delay")),
      movement_delay_ms:Number(value("movement-delay")), expected_state_version:stateVersion};
    if (!Number.isInteger(body.response_delay_ms) || body.response_delay_ms < 0 ||
        !Number.isInteger(body.movement_delay_ms) || body.movement_delay_ms < 0) throw new Error("지연은 0 이상 정수 ms로 입력하세요.");
    const isCurrent = currentRunContext();
    const result = await mutate(`reaction:${runId}`, `${api}/test/runs/${encodeURIComponent(runId)}/synthetic-users`, body, "PUT", isCurrent);
    if (!isCurrent()) return;
    text("reaction", result); notice("합성 반응 설정 결과 확인"); await refreshState();
  }

  async function setTestCondition(kind) {
    if (!runId || stateVersion === null) throw new Error("현재 관측을 먼저 읽으세요.");
    const path = kind === "fault" ? "device-faults" : "s2-reaction";
    const body = kind === "fault"
      ? {expected_state_version:stateVersion, channel:value("fault-channel"), failed:value("fault-failed") === "true"}
      : {expected_state_version:stateVersion, mode:value("s2-mode"), delay_ms:Number(value("s2-delay"))};
    if (kind === "s2" && (!Number.isInteger(body.delay_ms) || body.delay_ms < 0 || body.delay_ms > 3000))
      throw new Error("S2 반응 지연은 0~3000ms 정수입니다.");
    const isCurrent = currentRunContext();
    await mutate(`${path}:${runId}`, `${api}/test/runs/${encodeURIComponent(runId)}/${path}`, body, "PUT", isCurrent);
    if (!isCurrent()) return;
    notice(`${path} 합성 시험 조건 설정 결과 확인`);
    await refreshAll();
  }

  async function agentAction(action) {
    if (!runId) throw new Error("run을 먼저 만드세요.");
    const mode = value("agent-mode");
    if (mode !== "mock" && mode !== "live") throw new Error("Agent 실행 모드를 확인하세요.");
    const body = {run_id:runId, action, mode};
    if (value("scenario")) body.scenario = value("scenario");
    if (action === "process" && !body.scenario) throw new Error("현재 사건을 한 번 처리하려면 장면을 선택하세요. Agent 시작은 자동 감시입니다.");
    if (value("agent-command-id")) body.command_id = value("agent-command-id");
    const isCurrent = currentRunContext();
    const result = await mutate(`agent:${action}:${runId}`, `${api}/test/agent/operations`, body, "POST", isCurrent);
    if (!isCurrent()) return;
    notice(`${mode} Agent ${action} 결과 확인`);
    text("agent-state", result);
    await refreshAgent();
  }

  async function createCommand() {
    if (!runId || !value("command-text")) throw new Error("run과 명령 원문이 필요합니다.");
    if (!Number.isInteger(stateVersion)) throw new Error("현재 상태를 먼저 새로 읽으세요.");
    const sameRun = currentRunContext(), selected = value("command-id"), currentPreview = previewSerial;
    const isCurrent = () => sameRun() && selected === value("command-id") && currentPreview === previewSerial;
    const result = await mutate(`command:create:${runId}`, `${api}/facilities/${facility}/commands`,
      {run_id:runId, based_on_state_version:stateVersion, text:value("command-text"), purpose:"operational_goal"}, "POST", isCurrent);
    if (!isCurrent()) return;
    const commandId = result.command_id;
    if (commandId) el("command-id").value = commandId;
    previewedId = null; commandVersion = null;
    text("command", result); text("plan", "명령이 접수됐습니다. 계획을 따로 읽고 확인하세요.");
    notice("명령 접수 결과 확인. 계획은 아직 실행 승인되지 않았습니다.");
    buttons();
  }

  async function previewCommand() {
    const id = value("command-id");
    if (!id) throw new Error("command ID가 필요합니다.");
    const serial = ++previewSerial;
    const sameRun = currentRunContext();
    const isCurrent = () => sameRun() && serial === previewSerial && id === value("command-id");
    previewedId = null; commandVersion = null; buttons();
    try {
      const [command,plan] = await Promise.all([
        request(`${api}/commands/${encodeURIComponent(id)}`),
        request(`${api}/commands/${encodeURIComponent(id)}/plan`)]);
      if (!isCurrent()) return;
      commandVersion = command.resource_version;
      previewedId = id;
      text("command", command); text("plan", plan); buttons();
      notice("명령 원문과 계획을 다시 읽었습니다. 확인 버튼은 현재 버전에 적용됩니다.");
    } catch (error) {
      if (!isCurrent()) error.staleContext = true;
      throw error;
    }
  }

  async function commandAction(action) {
    const id = value("command-id");
    if (!id) throw new Error("command ID가 필요합니다.");
    if (commandVersion === null || previewedId !== id) throw new Error("현재 명령과 계획을 먼저 읽으세요.");
    const body = {expected_resource_version:commandVersion};
    if (action === "clarify") {
      body.goal = value("clarify-goal");
      if (body.goal === "zone_notice") body.zone_id = value("clarify-zone") || "announcement-a";
    }
    const isCurrent = currentRunContext(), currentPreview = previewSerial;
    const sameCommand = () => isCurrent() && id === value("command-id") && currentPreview === previewSerial;
    const result = await mutate(`command:${action}:${id}`, `${api}/commands/${encodeURIComponent(id)}/${action}`, body, "POST", sameCommand);
    if (!sameCommand()) return;
    previewedId = null; commandVersion = null; buttons();
    text("command", result);
    notice(`${action} 결과 확인. 단계별 실행 결과는 계획을 다시 조회하세요.`);
    await previewCommand();
  }

  // This short browser cue never reports facility playback or resolves work.
  function soundNote(message, phase) {
    text("sound-status", message);
    el("sound-status").dataset.phase = phase;
  }

  function stopSound(message, phase="stopped", reset=false) {
    ++soundEpoch;
    if (soundNode) {
      soundNode.onended = null;
      try {soundNode.stop();} catch (_) { /* Already ended or not started. */ }
      try {soundNode.disconnect();} catch (_) { /* Disconnect independently of stop. */ }
      soundNode = null;
    }
    if (soundGain) {
      try {soundGain.disconnect();} catch (_) { /* Local cleanup. */ }
      soundGain = null;
    }
    if (reset) {
      soundEnabled = false; soundMuted = false;
      if (soundContext) {
        soundContext.onstatechange = null;
        try {Promise.resolve(soundContext.close()).catch(() => {});} catch (_) { /* Local cleanup. */ }
        soundContext = null;
      }
    }
    soundNote(message, phase);
    buttons();
  }

  async function enableSound() {
    if (!identity) return;
    const captured = generation;
    try {
      if (!soundContext) {
        const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
        if (!Context) throw new Error("unsupported");
        soundContext = new Context();
        soundContext.onstatechange = () => {
          if (soundContext && soundContext.state !== "running") {
            soundEnabled = false;
            stopSound("브라우저 소리 출력이 중단됐습니다. 청취는 미확인입니다.", "unknown");
          }
        };
      }
      // Resume directly in the click handler before any network await.
      const context = soundContext;
      // Autoplay denial can leave resume pending until a later user gesture.
      soundEnabled = false;
      soundNote("브라우저 소리 허용 대기 · 차단되면 활성화를 다시 누르세요. 실제 들림은 미확인", "blocked");
      buttons();
      await context.resume();
      if (captured !== generation || context !== soundContext || !identity) return;
      soundEnabled = context.state === "running";
      soundMuted = false;
      soundNote(soundEnabled ? "브라우저 소리 준비 완료 · 실제 들림은 미확인" :
        "브라우저가 재생을 허용하지 않았습니다. 소리 활성화를 다시 누르세요.", soundEnabled ? "enabled" : "blocked");
    } catch (_) {
      if (captured !== generation || !identity) return;
      soundEnabled = false;
      soundNote("브라우저 소리를 준비하지 못했습니다. 화면 안내를 확인하세요.", "failed");
    }
    buttons();
  }

  function startSignal(label) {
    if (!identity || !soundEnabled || soundMuted || soundContext?.state !== "running")
      throw new Error("소리 활성화·음소거 상태를 확인하세요.");
    stopSound("신호 준비", "pending");
    const epoch = soundEpoch;
    const context = soundContext;
    try {
      const node = context.createOscillator();
      const gain = context.createGain();
      soundNode = node; soundGain = gain;
      node.type = "sine"; node.frequency.value = 880; gain.gain.value = 0.035;
      node.connect(gain); gain.connect(context.destination);
      node.onended = () => {
        node.disconnect(); gain.disconnect();
        if (epoch !== soundEpoch || !identity) return;
        soundNode = null; soundGain = null;
        soundNote(`${label} · 신호 재생 종료, 실제 들림은 미확인`, "ended_unconfirmed");
      };
      node.start(); node.stop(context.currentTime + 0.35);
      soundNote(`${label} · 브라우저 신호 처리 중, 실제 들림은 미확인`, "playing");
    } catch (_) {
      stopSound("브라우저 신호 재생에 실패했습니다. 화면 안내를 확인하세요.", "failed");
    }
  }

  async function playSound(testOnly=false) {
    const captured = soundEpoch;
    const selectedRun = runId;
    try {
    if (!(await authenticate()) || captured !== soundEpoch) return;
    if (testOnly) {startSignal("로컬 음향 시험"); return;}
    if (!selectedRun || selectedRun !== runId) throw new Error("현재 회차를 먼저 확인하세요.");
    const state = await request(`${api}/facilities/${facility}/devices?run_id=${encodeURIComponent(selectedRun)}`);
    if (captured !== soundEpoch || selectedRun !== runId || !identity) return;
    if (value("sound-kind") === "alarm") {
      if (!(state.alarms || []).some(alarm => alarm.desired_active)) throw new Error("활성 경보가 없습니다.");
      startSignal("현재 활성 경보 · 가상 장치의 채널 결과와 별도");
    } else {
      const record = (state.broadcasts || []).find(item => item.operation_id === value("sound-broadcast-id"));
      const captions = {closing_notice:"영업 종료 안내", safety_notice:"안전 안내", no_litter_notice:"구역 이용 안내"};
      if (!record || record.receipt !== "accepted" || record.simulated_playback === "cancelled" || !captions[record.message_id])
        throw new Error("현재 회차의 유효한 방송 ID를 확인하세요.");
      startSignal(`${captions[record.message_id]} · 문구는 화면 안내, 소리는 짧은 신호`);
    }
    } catch (error) {
      if (captured === soundEpoch && identity)
        stopSound(`${error.message} 화면 안내를 확인하세요.`, "failed");
    }
  }

  function bind(id, callback) {
    el(id).addEventListener("click", () => callback().catch(error => {
      if (identity && !error.staleContext) notice(error.message, true);
    }));
  }
  bind("sound-enable", enableSound);
  bind("sound-test", () => playSound(true));
  bind("sound-current", () => playSound(false));
  bind("sound-stop", async () => stopSound("신호를 중단했습니다. 소리 전달은 미확인입니다."));
  bind("sound-mute", async () => {
    soundMuted = !soundMuted;
    stopSound(soundMuted ? "음소거 · 화면 안내를 확인하세요." : "음소거 해제 · 자동 재생하지 않습니다.", soundMuted ? "muted" : "enabled");
  });
  bind("refresh", refreshAll);
  bind("create-run", createRun);
  for (const action of ["start","pause","step","reset","replay"]) bind(action, () => control(action));
  bind("save-reaction", reaction);
  bind("save-fault", () => setTestCondition("fault"));
  bind("save-s2", () => setTestCondition("s2"));
  bind("read-reaction", refreshReaction);
  bind("read-devices", refreshDevices);
  bind("agent-refresh", refreshAgent);
  for (const action of ["start","process","stop"]) bind(`agent-${action}`, () => agentAction(action));
  bind("command-create", createCommand);
  bind("command-preview", previewCommand);
  for (const action of ["clarify","confirm","cancel"]) bind(`command-${action}`, () => commandAction(action));
  el("command-id").addEventListener("input", () => {previewedId = null; commandVersion = null; ++previewSerial; buttons();});
  buttons();
  bootstrap().catch(error => {if (identity) notice(error.message, true);});
  setInterval(() => {
    if (!identity) return;
    authenticate().catch(error => {if (identity) notice(error.message, true);});
  }, 1000);
})();
