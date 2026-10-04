import { useEffect, useRef, useState } from 'react';
import { AppContext } from './context';
import type { ModalSpec } from './context';
import { Button, Card, Head, Icon, Notice, RouteLink } from './components';
import { initialState, modes } from './fixtures';
import { LoginForm, ModalContent } from './forms';
import { CommandDetail, CommandPage, DriverVehicle, IncidentDetail, IncidentList, Missing, Monitor, NotificationDetail, Notifications, Relationships } from './pages';
import type { Delivery, Followup, Mode, Outcome, Role, State } from './model';
import { Dialog } from './components';
import { LiveApp } from './live/LiveApp';
const storageKey = 'parkline.frontend.mock.v1';
function restore(): State {
  try { const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); if (saved?.session && ['owner', 'driver'].includes(saved.session.role) && Array.isArray(saved.commands) && saved.drafts) return { ...initialState(), ...saved }; } catch { /* Corrupt/disabled storage starts a clean mock session. */ }
  return initialState();
}
const ownerNav = [['monitor', '관제 현황', 'monitor'], ['incidents', '사건 관리', 'incidents'], ['commands', 'AI 운영 요청', 'commands'], ['relationships', '등록 정보', 'relationships']];
const driverNav = [['vehicle', '내 차량', 'car'], ['notifications', '알림', 'bell']];
function Brand({ role }: { role?: Role }) { return <div className="brand"><span className="brand-mark" aria-hidden="true">P</span><div><div className="brand-name">PARKLINE</div>{role && <div className="brand-sub">{role === 'owner' ? '소유자' : '차주'}</div>}</div></div>; }
export function App() {
  const mode = new URLSearchParams(location.search).get('mode');
  return mode === 'live' || (mode !== 'mock' && import.meta.env.VITE_PARKING_MODE === 'live') ? <LiveApp/> : <MockApp/>;
}
function MockApp() {
  const [state, setState] = useState(restore); const [route, setRoute] = useState(location.hash.slice(1) || '/login');
  const [modal, setModal] = useState<ModalSpec | null>(null); const [menu, setMenu] = useState(false); const [message, setMessage] = useState('');
  const previousRoute = useRef(route); const scrollPositions = useRef(new Map<string, number>()); const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestedRole = route.startsWith('/owner/') ? 'owner' : route.startsWith('/driver/') ? 'driver' : '';
  const blocked = ['offline', 'loading', 'error', 'expired', 'revoked'].includes(state.mode);
  const go = (path: string) => { if (path !== route) location.hash = path; };
  const toast = (text: string) => { if (toastTimer.current) clearTimeout(toastTimer.current); setMessage(text); toastTimer.current = setTimeout(() => setMessage(''), 4500); };
  useEffect(() => { const change = () => { scrollPositions.current.set(previousRoute.current, window.scrollY); setRoute(location.hash.slice(1) || '/login'); }; window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []);
  useEffect(() => { setModal(null); setMenu(false); if (route !== previousRoute.current) { window.scrollTo(0, scrollPositions.current.get(route) || 0); requestAnimationFrame(() => document.querySelector<HTMLElement>('h1')?.focus({ preventScroll: true })); } previousRoute.current = route; }, [route]);
  useEffect(() => { try { if (state.session) sessionStorage.setItem(storageKey, JSON.stringify(state)); else sessionStorage.removeItem(storageKey); } catch { /* The app still works without storage. */ } }, [state]);
  useEffect(() => { if (state.mode === 'expired' || state.mode === 'revoked') requestAnimationFrame(() => document.querySelector<HTMLElement>('h1')?.focus()); }, [state.mode]);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);
  function reset(mode: Mode = 'normal') { if (toastTimer.current) clearTimeout(toastTimer.current); setMessage(''); setModal(null); setMenu(false); scrollPositions.current.clear(); setState({ ...initialState(), mode }); }
  function setMode(mode: Mode) { if (mode === 'expired' || mode === 'revoked') reset(mode); else setState(s => ({ ...s, mode })); }
  const context = {
    state, setState, route, go, toast, blocked, open: (m: ModalSpec) => setModal(m), close: () => setModal(null),
    draft: (key: string, defaults: Record<string, string> = {}) => ({ ...defaults, ...state.drafts[key] }),
    editDraft: (key: string, field: string, value: string, defaults: Record<string, string> = {}) => setState(s => ({ ...s, drafts: { ...s.drafts, [key]: { ...defaults, ...s.drafts[key], [field]: value } } })),
    clearDraft: (key: string) => setState(s => { const drafts = { ...s.drafts }; delete drafts[key]; return { ...s, drafts }; }),
  };
  const nav = (role: Role) => (role === 'owner' ? ownerNav : driverNav).map(([path, label, ico]) => <a className="nav-link" href={`#/${role}/${path}`} key={path} aria-current={route.startsWith(`/${role}/${path}`) ? 'page' : undefined}><Icon name={ico}/><span>{label}</span></a>);
  const preview = <div className="preview-bar"><strong>목업 · 가상 데이터</strong><div className="preview-controls"><details className="preview-info"><summary>목업 안내·상태 설정</summary><div className="preview-info-body"><p>실제 인증·AI 분석·연락·장치 실행은 연결되지 않았습니다.</p><p>가상 입력은 이 탭에서만 보존합니다. 로그아웃·만료·권한 철회 시 초기화됩니다.</p><label>차주 위치<select aria-label="차주 위치 시안" value={state.driverLocation} onChange={e => setState(s => ({ ...s, driverLocation: e.target.value as State['driverLocation'] }))}><option value="bay">주차면 B02</option><option value="aisle">서측 통로</option></select></label><label>요청 결과<select aria-label="요청 결과 시안" value={state.outcome} onChange={e => setState(s => ({ ...s, outcome: e.target.value as Outcome }))}>{[['complete', '전체 완료'], ['partial', '일부 완료·방송 실패'], ['failed', '실패'], ['held', '안전 조건 보류'], ['unknown', '결과 미확인']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>알림 전달<select aria-label="알림 전달 시안" value={state.delivery} onChange={e => setState(s => ({ ...s, delivery: e.target.value as Delivery }))}>{[['queued', '전달 대기'], ['channel_accepted', '채널 접수'], ['client_received', '화면 수신'], ['failed', '전달 실패'], ['unknown', '결과 미확인']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>후속 관측<select aria-label="후속 관측 시안" value={state.followup} onChange={e => setState(s => ({ ...s, followup: e.target.value as Followup }))}>{[['waiting', '확인 대기'], ['blocked', '통로 차단 지속'], ['insufficient', '관측 부족'], ['recovered', '지속 통로 회복 확인']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>응답 기한<select aria-label="응답 기한 시안" value={String(state.deadlinePassed)} onChange={e => setState(s => ({ ...s, deadlinePassed: e.target.value === 'true' }))}><option value="false">기한 전</option><option value="true">기한 경과</option></select></label><label>저장 결과<select aria-label="저장 결과 시안" value={state.saveScenario} onChange={e => setState(s => ({ ...s, saveScenario: e.target.value as State['saveScenario'] }))}><option value="normal">정상</option><option value="conflict">버전 충돌</option><option value="failure">저장 실패</option></select></label></div></details><label>표시 상태<select aria-label="목업 표시 상태" value={state.mode} onChange={e => setMode(e.target.value as Mode)}>{modes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div></div>;
  let content;
  if (route === '/owner/monitor') content = <Monitor/>;
  else if (route === '/owner/incidents') content = <IncidentList/>;
  else if (route.startsWith('/owner/incidents/')) content = <IncidentDetail id={route.split('/').at(-1)!}/>;
  else if (route === '/owner/commands') content = <CommandPage/>;
  else if (route.startsWith('/owner/commands/')) content = <CommandDetail id={route.split('/').at(-1)!}/>;
  else if (route === '/owner/relationships') content = <Relationships/>;
  else if (route === '/driver/vehicle') content = <DriverVehicle/>;
  else if (route === '/driver/notifications') content = <Notifications/>;
  else if (route.startsWith('/driver/notifications/')) content = <NotificationDetail key={route} id={route.split('/').at(-1)!}/>;
  else content = <Missing name="화면" to={state.session?.role === 'owner' ? '/owner/monitor' : '/driver/vehicle'}/>;
  const role = state.session?.role;
  return <AppContext.Provider value={context}><a className="skip-link" href="#main" onClick={e => { e.preventDefault(); const main = document.getElementById('main'); main?.focus(); }}>본문으로 이동</a>{preview}
    {!role || route === '/login' ? <main id="main" className="login-layout" tabIndex={-1}><section className="login-intro"><Brand/><h1 tabIndex={-1}>주차장 관리</h1>{state.mode === 'expired' && <Notice title="세션이 만료됐어요.">다시 로그인해 주세요.</Notice>}{state.mode === 'revoked' && <Notice title="접근 권한이 변경됐어요.">이전 정보는 초기화했습니다. 다시 로그인해 주세요.</Notice>}</section><section className="card login-choice"><h2>로그인</h2><LoginForm/></section></main> : <div className={role}><div className="layout"><aside className="sidebar" aria-label={`${role === 'owner' ? '소유자' : '차주'} 메뉴`}><Brand role={role}/><nav className="sidebar-nav">{nav(role)}</nav></aside><div className="workspace"><header className="topbar"><div className="facility"><Button className="mobile-menu" aria-expanded={menu} aria-controls="mobile-menu-links" onClick={() => setMenu(true)}>메뉴</Button><strong>{role === 'owner' ? '데모 주차장' : '내 주차 정보'}</strong></div><div className="top-actions">{role === 'owner' ? <Button onClick={() => setModal({ type: 'inbox' })}><Icon name="bell"/>알림</Button> : <RouteLink to="/driver/notifications">알림</RouteLink>}<Button variant="flat" onClick={() => { reset(); go('/login'); }}>나가기</Button></div></header><main id="main" className="main" tabIndex={-1}>{requestedRole && requestedRole !== role ? <><Head title="접근할 수 없는 화면"/><Card><RouteLink to={`/${role}/${role === 'owner' ? 'monitor' : 'vehicle'}`}>내 화면으로</RouteLink></Card></> : <>{state.mode === 'offline' && <Notice title="연결이 끊겼어요.">마지막 예시를 표시합니다. 현재 위치와 처리 결과는 확인되지 않았어요.</Notice>}{state.mode === 'stale' && <Notice title="최근 관측을 확인하지 못했어요.">마지막 위치·시각을 표시합니다.</Notice>}{content}</>}</main></div></div>{role === 'driver' && <nav className="mobile-nav" aria-label="차주 하단 메뉴">{driverNav.map(([path, label, ico]) => <a key={path} href={`#/driver/${path}`} aria-current={route.startsWith(`/driver/${path}`) ? 'page' : undefined}><Icon name={ico}/>{label}</a>)}</nav>}</div>}
    {menu && role && <Dialog title="메뉴" onClose={() => setMenu(false)}><nav id="mobile-menu-links" className="sidebar-nav">{nav(role)}</nav></Dialog>}{modal && role && !['expired', 'revoked'].includes(state.mode) && <ModalContent modal={modal}/>}{message && <div className="toast" role="status" aria-live="polite">{message}</div>}
  </AppContext.Provider>;
}
