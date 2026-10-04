import { useEffect, useId, useRef, useState } from 'react';
import { Button, Card, Dialog, Empty, Facts, Field, Head, Icon, Loading, Notice, RouteLink } from '../components';
import { ApiError, ParkingApi, list, number, row, rows, text } from './api';
import type { Row } from './api';
import { readCollection } from './collections';
import { LiveContext, useLive } from './context';
import { DriverCommandDetail } from './DriverCommandDetail';
import type { LiveData } from './context';
import { LiveCommands, LiveCommandDetail, LiveRelationships } from './Operations';
import { LiveMonitor, LiveIncidents, LiveIncidentDetail, LiveVehicle, LiveNotifications, LiveNotificationDetail, clearLivePageMemory } from './Pages';

const emptyData = (): LiveData => ({ readiness: null, view: null, map: null, incidents: [], devices: [], vehicles: [], relationships: null, notifications: [] });
const ownerNav = [['monitor', '관제 현황', 'monitor'], ['incidents', '사건 관리', 'incidents'], ['commands', 'AI 운영 요청', 'commands'], ['relationships', '등록 정보', 'relationships']];
const driverNav = [['vehicle', '내 차량', 'car'], ['notifications', '알림', 'bell']];
function Brand({ role }: { role?: string }) { return <div className="brand"><span className="brand-mark" aria-hidden="true">P</span><div><div className="brand-name">PARKLINE</div>{role && <div className="brand-sub">{role === 'owner' ? '소유자' : '차주'}</div>}</div></div>; }
function aborted(error: unknown) { return error instanceof DOMException && error.name === 'AbortError'; }
function errorText(error: unknown) { return error instanceof Error ? error.message : '연결을 확인해 주세요.'; }

export function LiveApp() {
  const [me, setMe] = useState<Row | null>(null); const [data, setData] = useState<LiveData>(emptyData);
  const [epoch, setEpoch] = useState(0); const [revision, setRevision] = useState(0);
  const [streamGeneration, setStreamGeneration] = useState(0);
  const [route, setRoute] = useState(location.hash.slice(1) || '/login');
  const [checking, setChecking] = useState(true); const [connected, setConnected] = useState(false);
  const [disconnected, setDisconnected] = useState(false);
  const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const [menu, setMenu] = useState(false); const [guide, setGuide] = useState(false); const [loggingOut, setLoggingOut] = useState(false);
  const [knownCommands, setKnownCommands] = useState<string[]>([]);
  const session = useRef<Row | null>(null); const run = useRef(''); const current = useRef(emptyData());
  const scope = useRef(0); const stream = useRef<EventSource | null>(null); const seen = useRef(new Set<string>());
  const expiry = useRef<() => void>(() => {}); const refreshing = useRef<Promise<void> | null>(null); const refreshAgain = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [api] = useState(() => new ParkingApi(() => expiry.current()));
  const grants = rows(me?.facility_roles); const grant = grants.find(g => list(g.roles).some(r => ['owner', 'driver', 'test_operator'].includes(text(r))));
  const facilityId = text(grant?.facility_id, ''); const roles = list(grant?.roles);
  const role: 'owner' | 'driver' = roles.includes('owner') || roles.includes('test_operator') ? 'owner' : 'driver';
  const go = (path: string) => { location.hash = path; };
  const toast = (value: string) => { if (timer.current) clearTimeout(timer.current); setMessage(value); timer.current = setTimeout(() => setMessage(''), 4500); };
  function putData(value: LiveData) { current.current = value; setData(value); }
  function clearRun() {
    scope.current++; api.clear(); if (session.current) api.setCsrf(text(session.current.csrf_token, '')); stream.current?.close(); stream.current = null; seen.current.clear(); run.current = '';
    clearLivePageMemory(); setEpoch(value => value + 1); setKnownCommands([]); putData(emptyData()); setConnected(false); setDisconnected(false); setGuide(false); setMenu(false);
  }
  function clearSession(reason = '') { session.current = null; clearRun(); setMe(null); setError(reason); setChecking(false); setMessage(''); go('/login'); }
  expiry.current = () => clearSession('세션이 만료됐어요. 다시 로그인해 주세요.');
  function acceptMe(value: Row) {
    const validGrant = rows(value.facility_roles).find(g => list(g.roles).some(r => ['owner', 'driver', 'test_operator'].includes(text(r))));
    if (!validGrant || !text(value.csrf_token, '') || !text(value.user_id, '')) throw new Error('접근 가능한 주차장 권한을 확인하지 못했습니다.');
    api.setCsrf(text(value.csrf_token)); session.current = value; setMe(value);
  }
  async function refresh() {
    if (refreshing.current) { refreshAgain.current = true; return refreshing.current; }
    const work = async () => {
      const ticket = scope.current; const identity = session.current;
      if (!identity) return;
      const activeGrant = rows(identity.facility_roles).find(g => list(g.roles).some(r => ['owner', 'driver', 'test_operator'].includes(text(r))));
      const fid = text(activeGrant?.facility_id, ''); const owner = list(activeGrant?.roles).some(r => r === 'owner' || r === 'test_operator');
      try {
        const checkedMe = await api.get('/api/v1/me');
        if (ticket !== scope.current || identity !== session.current) return;
        if (checkedMe.user_id !== identity.user_id || checkedMe.csrf_token !== identity.csrf_token || JSON.stringify(checkedMe.facility_roles) !== JSON.stringify(identity.facility_roles)) { clearSession('로그인 또는 접근 권한이 변경됐어요. 다시 로그인해 주세요.'); return; }
        const ready = await api.get('/health/ready');
        if (ticket !== scope.current || identity !== session.current) return;
        const nextRun = text(ready.current_run_id, '');
        if (nextRun !== run.current) { clearRun(); run.current = nextRun; }
        const activeTicket = scope.current;
        const base = `/api/v1/facilities/${encodeURIComponent(fid)}`; const query = `?run_id=${encodeURIComponent(nextRun)}`;
        const paths: Record<string, string> = { notifications: '/api/v1/notifications', vehicles: `/api/v1/me/vehicles?facility_id=${encodeURIComponent(fid)}` };
        if (owner) { paths.map = `${base}/map`; paths.relationships = `${base}/relationships`; }
        if (nextRun) { paths.view = `${base}/state${query}`; if (owner) { paths.incidents = `${base}/incidents${query}`; paths.devices = `${base}/devices${query}`; } }
        const entries = Object.entries(paths);
        const results = await Promise.allSettled(entries.map(([name, path]) => name === 'notifications' || name === 'incidents' ? readCollection(api, path).then(items => ({ items })) : api.get(path)));
        if (activeTicket !== scope.current || identity !== session.current) return;
        const values: Record<string, Row> = {}; const failed: string[] = [];
        results.forEach((result, i) => { if (result.status === 'fulfilled') values[entries[i][0]] = result.value; else if (!aborted(result.reason)) failed.push(errorText(result.reason)); });
        const view = values.view || null; const snapshot = row(view?.snapshot);
        if (view && text(snapshot.run_id, '') !== nextRun) { refreshAgain.current = true; return; }
        const existing = current.current.view; const existingSnapshot = row(existing?.snapshot);
        const newView = view && text(existingSnapshot.run_id, '') === nextRun && (number(existing?.applied_state_version) ?? number(existingSnapshot.state_version) ?? -1) > (number(view.applied_state_version) ?? number(snapshot.state_version) ?? -1) ? existing : view;
        putData({ readiness: ready, view: newView, map: values.map || null, incidents: rows(values.incidents?.items), vehicles: rows(values.vehicles?.vehicles), relationships: values.relationships || null, notifications: rows(values.notifications?.items).filter(value => !nextRun || text(value.run_id, '') === nextRun), devices: [...rows(values.devices?.gates).map(v => ({ ...v, type: 'gate' })), ...rows(values.devices?.alarms).map(v => ({ ...v, type: 'alarm' })), ...rows(values.devices?.broadcasts).map(v => ({ ...v, type: 'broadcast' }))] });
        setRevision(value => value + 1); setError(failed[0] || '');
        if (!nextRun) setConnected(true);
        else if (!failed.length && newView && stream.current?.readyState === EventSource.OPEN) { setConnected(true); setDisconnected(false); }
      } catch (failure) { if (ticket === scope.current && identity === session.current && !aborted(failure)) { setError(errorText(failure)); setConnected(false); } }
    };
    const promise = work(); refreshing.current = promise;
    try { await promise; } finally { if (refreshing.current === promise) refreshing.current = null; if (refreshAgain.current) { refreshAgain.current = false; void refresh(); } }
  }
  const refreshLatest = useRef(refresh); refreshLatest.current = refresh;
  useEffect(() => {
    let active = true;
    const title = document.title; document.title = 'PARKLINE · 서버 연결';
    void api.get('/api/v1/me').then(value => { if (active) { acceptMe(value); setError(''); setChecking(false); void refreshLatest.current(); } }).catch(failure => { if (active && !aborted(failure)) { setChecking(false); if (!session.current) setError(failure.status === 401 ? '' : errorText(failure)); } });
    return () => { active = false; api.clear(); stream.current?.close(); document.title = title; if (timer.current) clearTimeout(timer.current); };
  }, [api]);
  const runId = text(data.readiness?.current_run_id, '');
  useEffect(() => {
    if (!me || !facilityId || !runId) return;
    const ticket = scope.current; const expectedRun = runId;
    const source = new EventSource(`/api/v1/facilities/${encodeURIComponent(facilityId)}/events?run_id=${encodeURIComponent(runId)}`); stream.current = source;
    const valid = () => ticket === scope.current && source === stream.current && session.current !== null;
    source.onopen = () => { if (valid()) void api.get('/api/v1/me').then(value => { if (valid()) { const old = session.current; if (JSON.stringify(value.facility_roles) !== JSON.stringify(old?.facility_roles) || value.user_id !== old?.user_id || value.csrf_token !== old?.csrf_token) { clearSession('로그인 또는 접근 권한이 변경됐어요. 다시 로그인해 주세요.'); return; } api.setCsrf(text(value.csrf_token)); setConnected(true); setDisconnected(false); void refreshLatest.current(); } }).catch(failure => { if (valid() && !aborted(failure)) { setConnected(false); setError(errorText(failure)); } }); };
    let rechecking = false;
    source.onerror = () => {
      if (!valid()) return;
      setConnected(false); setDisconnected(true);
      if (rechecking) return;
      rechecking = true;
      void api.get('/api/v1/me').then(value => {
        if (valid() && (JSON.stringify(value.facility_roles) !== JSON.stringify(session.current?.facility_roles) || value.user_id !== session.current?.user_id || value.csrf_token !== session.current?.csrf_token)) clearSession('로그인 또는 접근 권한이 변경됐어요. 다시 로그인해 주세요.');
      }).catch(failure => { if (valid() && !aborted(failure)) setError(errorText(failure)); }).finally(() => { rechecking = false; });
    };
    source.addEventListener('access.revoked', () => { if (valid()) clearSession('접근 권한이 변경됐어요. 다시 로그인해 주세요.'); });
    source.addEventListener('reset_required', (event: Event) => {
      if (!valid()) return;
      let nextRun = '';
      try { nextRun = text(row(row(JSON.parse((event as MessageEvent<string>).data)).payload).run_id, ''); } catch { /* Re-read readiness if the reset payload is unavailable. */ }
      if (nextRun !== expectedRun) clearRun();
      else {
        // A cursor resync must retain in-flight mutation attempts and their keys.
        scope.current++; source.close(); stream.current = null; seen.current.clear();
        putData({ ...emptyData(), readiness: current.current.readiness });
        setConnected(false); setRevision(value => value + 1); setStreamGeneration(value => value + 1);
      }
      refreshAgain.current = true; void refreshLatest.current();
    });
    function handle(event: Event) {
      if (!valid()) return;
      try {
        const messageEvent = event as MessageEvent<string>; const envelope = row(JSON.parse(messageEvent.data));
        if (text(envelope.facility_id) !== facilityId || text(envelope.run_id) !== expectedRun) return;
        const id = text(envelope.event_id, ''); if (id && seen.current.has(id)) return;
        if (id) { seen.current.add(id); if (seen.current.size > 1000) seen.current.delete(seen.current.values().next().value!); }
        if (event.type === 'state.snapshot' || event.type === 'run.updated') {
          const view = row(envelope.payload); const snapshot = row(view.snapshot); const previous = row(current.current.view?.snapshot);
          if (text(snapshot.run_id) !== expectedRun || (number(view.applied_state_version) ?? number(envelope.state_version) ?? number(snapshot.state_version) ?? -1) < (number(current.current.view?.applied_state_version) ?? number(previous.state_version) ?? -1)) return;
          putData({ ...current.current, view });
          if (event.type === 'run.updated') void refreshLatest.current();
        } else {
          const payload = row(envelope.payload);
          if (event.type === 'command.updated' && text(payload.command_id, '')) setKnownCommands(old => old.includes(text(payload.command_id)) ? old : [...old, text(payload.command_id)]);
          void refreshLatest.current();
        }
      } catch { setConnected(false); setError('갱신 정보를 확인하지 못했습니다. 새로고침해 주세요.'); }
    }
    const events = ['state.snapshot', 'run.updated', 'incident.updated', 'command.updated', 'execution.updated', 'notification.updated', 'relationship.updated'];
    events.forEach(name => source.addEventListener(name, handle));
    return () => { source.close(); if (stream.current === source) stream.current = null; };
  }, [api, me, facilityId, runId, epoch, streamGeneration]);
  useEffect(() => { const change = () => setRoute(location.hash.slice(1) || '/login'); window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []);
  useEffect(() => { setMenu(false); setGuide(false); window.scrollTo(0, 0); requestAnimationFrame(() => document.querySelector<HTMLElement>('h1')?.focus({ preventScroll: true })); }, [route]);
  useEffect(() => { if (me && route === '/login') go(role === 'owner' ? '/owner/monitor' : '/driver/vehicle'); }, [me, role, route]);
  const blocked = !connected || !!error || !runId || !!data.view?.recovery_required || data.view?.run_status === 'replaying';
  const nav = () => (role === 'owner' ? ownerNav : driverNav).map(([path, label, icon]) => <a className="nav-link" href={`#/${role}/${path}`} key={path} aria-current={route.startsWith(`/${role}/${path}`) ? 'page' : undefined}><Icon name={icon}/><span>{label}</span></a>);
  let id = ''; try { id = decodeURIComponent(route.split('/').at(-1) || ''); } catch { /* Invalid route gets the missing-resource view. */ } let content;
  if (route.startsWith(role === 'owner' ? '/driver/' : '/owner/')) content = <><Head title="접근할 수 없는 화면"/><RouteLink to={role === 'owner' ? '/owner/monitor' : '/driver/vehicle'}>내 화면으로</RouteLink></>;
  else if (route === '/owner/monitor') content = <LiveMonitor/>;
  else if (route === '/owner/incidents') content = <LiveIncidents/>;
  else if (route.startsWith('/owner/incidents/')) content = <LiveIncidentDetail id={id}/>;
  else if (route === '/owner/commands') content = <LiveCommands/>;
  else if (route.startsWith('/owner/commands/')) content = <LiveCommandDetail id={id}/>;
  else if (route === '/owner/relationships') content = <LiveRelationships/>;
  else if (route === '/driver/vehicle') content = <LiveVehicle/>;
  else if (route.startsWith('/driver/commands/')) content = <DriverCommandDetail id={id}/>;
  else if (route === `/${role}/notifications`) content = <LiveNotifications/>;
  else if (route.startsWith(`/${role}/notifications/`)) content = <LiveNotificationDetail id={id}/>;
  else content = <Empty title="화면을 찾을 수 없습니다"><RouteLink to={role === 'owner' ? '/owner/monitor' : '/driver/vehicle'}>내 화면으로</RouteLink></Empty>;
  async function logout() { if (loggingOut) return; setLoggingOut(true); const ticket = scope.current; try { await api.logout(); clearSession(); } catch (failure) { if (ticket === scope.current && !aborted(failure)) toast(errorText(failure)); } finally { setLoggingOut(false); } }
  return <><a className="skip-link" href="#main" onClick={e => { e.preventDefault(); document.getElementById('main')?.focus(); }}>본문으로 이동</a><div className="preview-bar"><strong>서버 연결 · 가상 주차장</strong><div className="preview-controls"><a href="?mode=mock#/login">목업 보기</a><span>{me ? connected ? '연결됨' : '연결 확인 중' : '로그인 필요'}</span></div></div>
    {checking ? <main id="main" className="login-layout"><Loading/></main> : !me ? <main id="main" className="login-layout" tabIndex={-1}><section className="login-intro"><Brand/><h1 tabIndex={-1}>주차장 관리</h1>{error && <Notice title={error}/>}</section><section className="card login-choice"><h2>로그인</h2><LiveLogin api={api} onLogin={async value => { clearRun(); acceptMe(value); setError(''); await refreshLatest.current(); }}/></section></main> : <LiveContext.Provider value={{ api, me, role, facilityId, data, epoch, revision, refresh, go, toast, knownCommands, rememberCommand: command => setKnownCommands(old => old.includes(command) ? old : [...old, command]), blocked }}><div className={role}><div className="layout"><aside className="sidebar" aria-label={`${role === 'owner' ? '소유자' : '차주'} 메뉴`}><Brand role={role}/><nav className="sidebar-nav">{nav()}</nav></aside><div className="workspace"><header className="topbar"><div className="facility"><Button className="mobile-menu" aria-expanded={menu} onClick={() => setMenu(true)}>메뉴</Button><strong>{role === 'owner' ? '데모 주차장' : '내 주차 정보'}</strong></div><div className="top-actions"><RouteLink to={`/${role}/notifications`}><Icon name="bell"/>알림</RouteLink><Button variant="flat" disabled={loggingOut} onClick={() => void logout()}>나가기</Button></div></header><main id="main" className="main" tabIndex={-1}>{error && <Notice title={error}><Button onClick={() => void refresh()}>다시 불러오기</Button></Notice>}{disconnected && runId && <Notice title="연결이 끊겼어요.">마지막 수신 정보를 표시합니다. 새 정보와 처리 결과는 확인되지 않았어요.<Button onClick={() => void refresh()}>다시 확인</Button></Notice>}{!runId && <Notice title="시작된 시험이 없습니다."/>}<div key={epoch}>{content}<div className="app-footer"><Button variant="flat" onClick={() => setGuide(true)}>주차장 이용안내</Button><Button variant="flat" onClick={() => void refresh()}>새로고침</Button></div>{roles.includes('test_operator') && <LocalControls/>}</div></main></div></div>{role === 'driver' && <nav className="mobile-nav" aria-label="차주 하단 메뉴">{driverNav.map(([path, label, icon]) => <a key={path} href={`#/driver/${path}`} aria-current={route.startsWith(`/driver/${path}`) ? 'page' : undefined}><Icon name={icon}/>{label}</a>)}</nav>}</div>{menu && <Dialog title="메뉴" onClose={() => setMenu(false)}><nav className="sidebar-nav">{nav()}</nav></Dialog>}{guide && <Dialog title="주차장 이용안내" onClose={() => setGuide(false)}><Guide/></Dialog>}</LiveContext.Provider>}{message && <div className="toast" role="status">{message}</div>}</>;
}

function LiveLogin({ api, onLogin }: { api: ParkingApi; onLogin: (value: Row) => Promise<void> }) {
  const [username, setUsername] = useState('demo-owner'); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const pending = useRef(false);
  return <form onSubmit={e => { e.preventDefault(); if (pending.current || !username.trim() || !password) return; pending.current = true; setBusy(true); setError(''); void api.login(username.trim(), password).then(() => { setPassword(''); return api.get('/api/v1/me'); }).then(onLogin).catch(failure => { if (!aborted(failure)) setError(errorText(failure)); }).finally(() => { pending.current = false; setBusy(false); }); }}><Field label="계정" input={{ autoComplete: 'username', required: true, value: username, onChange: e => setUsername(e.target.value), disabled: busy }}/><Field label="비밀번호" input={{ autoComplete: 'current-password', required: true, type: 'password', value: password, onChange: e => setPassword(e.target.value), disabled: busy }}/><p className="form-note">가상 계정: demo-owner / demo-driver / demo-operator<br/>비밀번호: parking-demo-only</p>{error && <Notice title={error}/>}<Button type="submit" variant="primary full" disabled={busy || !username.trim() || !password}>{busy ? '로그인 중' : '로그인'}</Button></form>;
}

function LocalControls() {
  const { api, data, facilityId, refresh, toast } = useLive(); const actionId = useId(); const [fixture, setFixture] = useState('s1b-blocked-v1'); const [busy, setBusy] = useState(false); const pending = useRef(false);
  const runId = text(data.readiness?.current_run_id, '');
  async function act(action: string) { if (pending.current) return; pending.current = true; setBusy(true); try { await api.mutate(actionId, action === 'create' ? '/api/v1/test/runs' : `/api/v1/test/runs/${encodeURIComponent(runId)}/control`, action === 'create' ? { facility_id: facilityId, fixture_ref: fixture, seed: 42, config_ref: fixture === 's1a-foundation-v1' ? 'foundation-v1' : 'sim0-v1' } : { action }); await refresh(); toast(action === 'create' ? '시험 회차를 만들었습니다.' : '시험 제어를 접수했습니다.'); } catch (failure) { if (!aborted(failure)) toast(errorText(failure)); } finally { pending.current = false; setBusy(false); } }
  if (!data.readiness?.test_control_enabled) return null;
  return <details className="section-gap"><summary>로컬 시험 제어</summary><Card><Field label="가상 상황" as="select" input={{ value: fixture, onChange: e => setFixture(e.target.value), disabled: busy }}><option value="s1b-blocked-v1">출차 통로 막힘</option><option value="s1b-clear-v1">정상 통로</option><option value="s1c-overlap-v1">구역 겹침</option><option value="s2-crossing-v1">보행자 교차</option><option value="s3-closing-v1">마감 운영</option><option value="s3-gate-obstacle-v1">게이트 장애물</option></Field><div className="row"><Button disabled={busy} onClick={() => void act('create')}>새 시험 회차</Button>{['start', 'pause', 'step', 'reset'].map((action, i) => <Button key={action} disabled={busy || !runId} onClick={() => void act(action)}>{['진행', '일시정지', '한 단계', '초기화'][i]}</Button>)}</div><p className="form-note section-gap">가상 장치를 제어하는 로컬 시험입니다. 실제 AI 운영은 자동으로 시작되지 않습니다.</p></Card></details>;
}
function Guide() {
  const { api, data, role, blocked } = useLive(); const actionId = useId(); const [query, setQuery] = useState(''); const [provider, setProvider] = useState('mock'); const [goal, setGoal] = useState('regulation'); const [result, setResult] = useState<Row | null>(null); const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [ambiguous, setAmbiguous] = useState(false); const pending = useRef(false);
  const attempt = useRef<{ path: string; body: Row } | null>(null); const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function ask() {
    if (pending.current) return; pending.current = true; setBusy(true); setError(''); setResult(null);
    if (!attempt.current) { const body: Row = { run_id: data.readiness?.current_run_id, goal, query }; if (provider !== 'mock') body.provider = provider; attempt.current = { path: provider === 'mock' ? '/api/v1/test/agent/queries' : '/api/v1/test/agent/live-queries', body }; }
    try { const answer = await api.mutate(actionId, attempt.current.path, attempt.current.body); if (alive.current) { attempt.current = null; setAmbiguous(false); setResult(answer); } }
    catch (failure) { if (alive.current && !aborted(failure)) { const unknown = failure instanceof ApiError && failure.ambiguous; setAmbiguous(unknown); if (!unknown) attempt.current = null; setError(errorText(failure)); } }
    finally { pending.current = false; if (alive.current) setBusy(false); }
  }
  return <><Field label="조회 내용" as="select" input={{ value: goal, onChange: e => setGoal(e.target.value), disabled: busy || ambiguous }}><option value="regulation">주차장 이용 규정</option><option value={role === 'owner' ? 'current_state' : 'my_vehicle'}>{role === 'owner' ? '현재 주차장 상태' : '내 차량 상태'}</option></Field><Field label="질문" as="textarea" input={{ value: query, maxLength: 500, onChange: e => setQuery(e.target.value), disabled: busy || ambiguous }}/><Field label="답변 방식" as="select" input={{ value: provider, onChange: e => setProvider(e.target.value), disabled: busy || ambiguous }}><option value="mock">모의 AI</option>{data.readiness?.llm === 'live_read_configured' && <><option value="openai">OpenAI · 유료 API</option><option value="gemini">Gemini · 유료 API</option></>}</Field>{provider !== 'mock' && <p className="form-note">질문을 보내면 선택한 서비스의 API 비용이 발생합니다.</p>}{error && <Notice title={error}/>}<Button variant="primary" disabled={busy || blocked || !data.readiness?.test_control_enabled || (goal === 'regulation' && !query.trim())} onClick={() => void ask()}>{busy ? '조회 중' : ambiguous ? '같은 질문 결과 확인' : '질문 보내기'}</Button>{result && <GuideResult result={result}/>}</>;
}
function GuideResult({ result }: { result: Row }) {
  const results = rows(result.tool_results); const references = results.flatMap(tool => rows(row(tool.result).references));
  const state = results.find(tool => tool.name === 'get_parking_state'); const view = row(state?.result); const snapshot = row(view.snapshot);
  return <Card title={result.status === 'completed' ? '조회 결과' : '확인 필요'}>
    {typeof result.answer === 'string' && result.answer ? <p style={{ whiteSpace: 'pre-wrap' }}>{result.answer}</p> : <p>{result.mode === 'mock' ? '모의 AI가 조회한 자료입니다.' : '답변이 제공되지 않았습니다.'}</p>}
    {references.length > 0 && <details className="section-gap" open={result.mode === 'mock'}><summary>조회 근거 {references.length}건</summary>{references.map((reference, index) => <details className="section-gap" key={text(reference.reference_id, String(index))}><summary>{text(reference.title)} · {text(reference.section)}</summary><p style={{ whiteSpace: 'pre-wrap' }}>{text(reference.excerpt)}</p><small>{text(reference.document_id)} · 문서 버전 {text(reference.document_version)}</small></details>)}</details>}
    {state && <div className="section-gap"><Facts items={[["관측 시각", text(snapshot.observed_at)], ['관측 범위', text(snapshot.coverage)], ['실행 상태', text(view.run_status)], ['관측 객체 수', String(rows(snapshot.objects).length)]]}/></div>}
    <p className="form-note section-gap">{result.mode === 'live' ? '실제 AI 조회' : '모의 AI 조회'}{result.status !== 'completed' ? ` · ${text(result.reason_code)}` : ''}{result.cost_estimated_krw != null ? ` · 예상 비용 ${text(result.cost_estimated_krw)}원` : ''}</p>
  </Card>;
}
