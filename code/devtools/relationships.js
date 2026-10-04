"use strict";
(() => {
  const facility = "fac-demo-01";
  const base = `/api/v1/facilities/${facility}/relationships`;
  const status = document.getElementById("status");
  const snapshot = document.getElementById("snapshot");
  let csrf = null;
  let currentUser = null;
  let generation = 0;

  function clear(message) {
    csrf = null;
    currentUser = null;
    generation += 1;
    snapshot.textContent = "";
    status.classList.add("error");
    status.textContent = message;
  }

  async function request(url, options = {}) {
    const current = generation;
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
    if (response.status === 401 || response.status === 403) {
      clear("접근 권한이 바뀌었거나 세션이 끝났습니다. 다시 로그인하세요.");
      throw new Error("접근할 수 없습니다.");
    }
    const result = await response.json();
    if (current !== generation) throw new Error("이전 세션의 응답을 폐기했습니다.");
    if (!response.ok) throw new Error(result.error?.message || `HTTP ${response.status}`);
    return result;
  }

  async function refresh() {
    const me = await request("/api/v1/me");
    if (currentUser !== null && currentUser !== me.user_id) {
      clear("계정이 바뀌었습니다. 다시 화면을 열어 주세요.");
      return;
    }
    if (!me.facility_roles?.some(item => item.facility_id === facility &&
        item.roles?.some(role => role === "owner" || role === "test_operator"))) {
      clear("관리 권한이 없습니다.");
      return;
    }
    csrf = me.csrf_token;
    currentUser = me.user_id;
    const data = await request(base);
    snapshot.textContent = JSON.stringify(data, null, 2);
    status.classList.remove("error");
    status.textContent = `조회 완료: ${data.updated_at}`;
  }

  function input(form, name) { return form.elements.namedItem(name)?.value.trim() ?? ""; }
  function payload(form) {
    const kind = form.dataset.action;
    const v = name => input(form, name);
    const version = () => Number(v("expected_version"));
    if (kind === "customer-create" || kind === "vehicle-create")
      return {method: "POST", path: kind === "customer-create" ? "/customers" : "/vehicles",
              body: {display_alias: v("display_alias"), reason: v("reason")}};
    if (kind === "customer-change" || kind === "vehicle-change") {
      const body = {expected_version: version(), reason: v("reason")};
      if (v("display_alias")) body.display_alias = v("display_alias");
      if (v("active") === "false") body.active = false;
      const prefix = kind === "customer-change" ? "/customers/" : "/vehicles/";
      return {method: "PATCH", path: prefix + encodeURIComponent(v(kind === "customer-change" ? "user_id" : "vehicle_id")), body};
    }
    if (kind === "vehicle-user") return {method: "PUT", path: `/vehicles/${encodeURIComponent(v("vehicle_id"))}/customer`,
      body: {expected_version: version(), user_id: v("user_id") || null, reason: v("reason")}};
    if (kind === "vehicle-object") return {method: "PUT", path: `/vehicle-objects/${encodeURIComponent(v("object_id"))}`,
      body: {run_id: v("run_id"), expected_version: version(), registered_vehicle_id: v("registered_vehicle_id") || null,
             mapping_status: v("mapping_status"), mapping_source: v("mapping_source"), reason: v("reason")}};
    return {method: "PUT", path: `/person-objects/${encodeURIComponent(v("object_id"))}`,
      body: {run_id: v("run_id"), expected_version: version(), user_id: v("user_id") || null,
             status: v("status"), source: v("source"), reason: v("reason")}};
  }

  document.getElementById("refresh").addEventListener("click", () => refresh().catch(err => {
    if (csrf !== null) {status.classList.add("error"); status.textContent = err.message;}
  }));
  document.querySelectorAll("form[data-action]").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    try {
      if (!csrf) await refresh();
      const captured = generation, user = currentUser;
      const op = payload(form);
      const result = await request(base + op.path, {method: op.method,
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf,
                  "Idempotency-Key": crypto.randomUUID()}, body: JSON.stringify(op.body)});
      await refresh();
      if (captured !== generation || user !== currentUser || csrf === null) return;
      status.textContent = `변경 완료: ${JSON.stringify(result)}`;
    } catch (err) {
      if (csrf !== null) {status.classList.add("error"); status.textContent = err.message;}
    }
  }));
  refresh().catch(err => {
    if (csrf !== null) {status.classList.add("error"); status.textContent = err.message;}
  });
  setInterval(() => {
    if (csrf === null) return;
    request("/api/v1/me").then(me => {
      if (currentUser !== me.user_id || !me.facility_roles?.some(item =>
          item.facility_id === facility && item.roles?.some(role => role === "owner" || role === "test_operator"))) {
        clear("관리 권한이나 계정이 바뀌었습니다. 다시 로그인하세요.");
      }
    }).catch(err => {
      if (csrf !== null) {status.classList.add("error"); status.textContent = err.message;}
    });
  }, 1000);
})();
