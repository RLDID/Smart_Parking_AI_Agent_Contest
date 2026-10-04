import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Badge, Button, Card, Dialog, Empty, Facts, Field, Head, Icon, Loading, Notice, RouteLink } from '../components';
import { ApiError, list, number, row, rows, text } from './api';
import type { Row } from './api';
import { useLive, useResource } from './context';
import { CommandsHistory, IncidentTimelineView, OwnVehicleObservation } from './ContestViews';

const statusNames: Record<string, string> = {
  candidate: '후보', active: '진행 중', monitoring: '후속 확인 중', needs_review: '검토 필요', escalated: '소유자 확인 필요', resolved: '해결', closed_no_issue: '문제 없음', closed_false_positive: '오탐 종료',
  running: '진행 중', paused: '일시정지', stopped: '중지', replaying: '재생 중', complete: '전체 관측', partial: '부분 관측', unavailable: '관측 불가',
  visible: '관측됨', occluded: '가림', missing: '관측 없음', vehicle: '차량', pedestrian: '보행자', unknown: '미확인',
  allow: '허용', deny: '제한', open: '열림', closed: '닫힘', opening: '열리는 중', closing: '닫히는 중',
  queued: '전달 대기', channel_accepted: '채널 접수', client_received: '화면 수신', failed: '실패', accepted: '접수', pending: '대기', played: '재생됨', not_requested: '미요청',
  on: '켜짐', off: '꺼짐', succeeded: '완료', held: '보류', cancelled: '취소', requested: '요청',
  acknowledged: '확인했어요', will_move: '이동할게요', cannot_move: '이동하기 어려워요', question: '문의할게요',
  synthetic_consumer: '합성 이용자', user: '사용자', human_or_unconfirmed: '사용자·확인 전', live: '로컬 웹 수신함', simulated: '모의 실행', synthetic_demo: '합성 시연',
  aisle_obstruction: '통로 차단', exit_blocked: '출차 방해', bay_intrusion: '주차면 침범', approach_risk: '접근 위험',
  blocked: '차단', clear: '통과 가능', supported: '분석 범위 내', unsupported: '추가 확인 필요',
};
const label = (value: unknown, fallback = '미제공') => statusNames[text(value, '')] || text(value, fallback);
const closedIncident = (item: Row) => ['resolved', 'closed_no_issue', 'closed_false_positive'].includes(text(item.status, ''));
function stamp(value: unknown): string {
  if (typeof value !== 'string' || !value) return '미제공';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `${date.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false })} KST`;
}
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : '요청 결과를 확인하지 못했습니다.'; }
function valueText(value: unknown): string {
  if (value === null || value === undefined) return '미제공';
  if (typeof value === 'boolean') return value ? '예' : '아니요';
  if (typeof value === 'object') return JSON.stringify(value);
  return text(value);
}
function jsonRow(value: unknown): Row {
  if (typeof value !== 'string') return row(value);
  try { return row(JSON.parse(value)); } catch { return {}; }
}
function ImpactFacts({ impact }: { impact: Row }) {
  const condition = jsonRow(impact.condition_json);
  const items: string[][] = [['대상', text(impact.object_id, '미제공')], ['구역', text(impact.zone_id)]];
  const passage = condition.passage || condition.candidate_passage;
  if (typeof passage === 'string') items.push([impact.type === 'exit_blocked' ? '출차 후보 경로' : '통로 상태', label(passage)]);
  const available = number(condition.available_clearance_m); const required = number(condition.required_clearance_m); const duration = number(condition.blocked_duration_ms);
  if (available !== null) items.push(['확보된 통로 폭', `${available.toFixed(2)} m`]);
  if (required !== null) items.push(['필요한 통로 폭', `${required.toFixed(2)} m`]);
  if (duration !== null) items.push(['차단 지속 시간', `${(duration / 1000).toFixed(1)}초`]);
  const blockers = list(condition.collision_object_ids || condition.occupied_object_ids).filter((value): value is string => typeof value === 'string');
  if (blockers.length) items.push(['방해 관측 객체', blockers.join(', ')]);
  if (typeof condition.clearance_sustained === 'boolean') items.push(['지속 통로 회복', condition.clearance_sustained ? '확인됨' : '미확인']);
  if (typeof condition.support_status === 'string') items.push(['분석 범위', label(condition.support_status)]);
  return <><Facts items={items}/>{impact.type === 'exit_blocked' && <p className="form-note section-gap">서버가 지원하는 출차 후보 경로를 기준으로 판단한 결과입니다.</p>}</>;
}
function SnapshotFacts({ snapshot, view }: { snapshot: Row; view: Row }) {
  return <Facts items={[
    ['관측 시각', stamp(snapshot.observed_at)], ['수신 시각', stamp(snapshot.received_at)],
    ['관측 범위', label(snapshot.coverage)], ['실행 상태', label(view.run_status)],
    ['관측 버전', text(snapshot.state_version)], ['합성 시각', number(snapshot.sim_time_ms) === null ? '미제공' : `${snapshot.sim_time_ms} ms`],
  ]}/>;
}
function PositionFacts({ object, unavailable = false }: { object: Row; unavailable?: boolean }) {
  const position = row(object.position); const quality = row(object.quality);
  const x = number(position.x); const y = number(position.y);
  return <Facts items={[
    ['관측 객체', text(object.object_id)], ['종류', label(object.object_type)],
    ['관측 품질', label(quality.visibility)], ['좌표', unavailable ? '관측 불가 · 위치 확인 전' : x === null || y === null ? '위치 미제공' : `x ${x.toFixed(2)} m · y ${y.toFixed(2)} m`],
    ['방향', number(object.heading_deg) === null ? '미제공' : `${object.heading_deg}°`], ['주차면 번호', '미제공'],
  ]}/>;
}

type Point = { x: number; y: number };
function point(value: unknown): Point | null { const p = row(value); const x = number(p.x); const y = number(p.y); return x === null || y === null ? null : { x, y }; }
function activate(e: KeyboardEvent<SVGGElement>, action: () => void) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); action(); } }
function LiveMap({ map, snapshot, onDevice }: { map: Row; snapshot: Row; onDevice: (id: string) => void }) {
  const [selected, setSelected] = useState('');
  const bounds = row(map.bounds); const minX = number(bounds.min_x); const minY = number(bounds.min_y); const maxX = number(bounds.max_x); const maxY = number(bounds.max_y);
  const objects = rows(snapshot.objects); const current = objects.find(o => text(o.object_id, '') === selected);
  if (minX === null || minY === null || maxX === null || maxY === null || maxX <= minX || maxY <= minY) return <Empty title="지도 좌표를 확인할 수 없어요."/>;
  const scale = 16; const width = (maxX - minX) * scale; const height = (maxY - minY) * scale;
  const px = (x: number) => (x - minX) * scale; const py = (y: number) => (maxY - y) * scale;
  const coverageAvailable = snapshot.coverage !== 'unavailable';
  return <><div className="map-wrap" tabIndex={0} aria-label="주차장 지도"><svg className="parking-map" viewBox={`-12 -12 ${width + 24} ${height + 24}`} role="group" aria-label="실제 지도와 합성 관측">
    <rect width={width} height={height} fill="#edf5fa" stroke="#c2d6e2"/>
    {rows(map.zones).map((zone, i) => { const polygon = list(zone.polygon).map(point).filter((p): p is Point => p !== null); return polygon.length >= 3 ? <polygon key={text(zone.zone_id, String(i))} points={polygon.map(p => `${px(p.x)},${py(p.y)}`).join(' ')} fill={zone.type === 'parking_bay' ? '#fafdff' : zone.type === 'pedestrian' || zone.type === 'walkway' ? '#d6dfcc' : '#e0edf5'} stroke="#b7cdd9"/> : null; })}
    {rows(map.gates).map((gate, i) => { const segment = list(gate.segment).map(point).filter((p): p is Point => p !== null); const id = text(gate.gate_id, ''); if (!id || segment.length !== 2) return null; return <g key={id || i} className="map-car" role="button" tabIndex={0} aria-label={`게이트 ${id}`} onClick={() => onDevice(id)} onKeyDown={e => activate(e, () => onDevice(id))}><line x1={px(segment[0].x)} y1={py(segment[0].y)} x2={px(segment[1].x)} y2={py(segment[1].y)} stroke="transparent" strokeWidth="48"/><line x1={px(segment[0].x)} y1={py(segment[0].y)} x2={px(segment[1].x)} y2={py(segment[1].y)} stroke="#6b8395" strokeWidth="5"/><text className="gate-label" x={px((segment[0].x + segment[1].x) / 2)} y={py((segment[0].y + segment[1].y) / 2) + 28} textAnchor="middle">{id}</text></g>; })}
    {coverageAvailable && objects.map((object, i) => {
      const position = point(object.position); const quality = row(object.quality); const id = text(object.object_id, ''); if (!position || !id || quality.visibility === 'missing') return null;
      const size = row(object.size); const length = number(size.length_m); const breadth = number(size.width_m); const heading = number(object.heading_deg);
      const bodyWidth = length === null ? 12 : length * scale; const bodyHeight = breadth === null ? 12 : breadth * scale;
      const uncertain = quality.visibility !== 'visible'; const vehicle = object.object_type === 'vehicle';
      return <g key={id || i} className="map-car" role="button" tabIndex={0} aria-label={`${label(object.object_type)} ${id} · ${label(quality.visibility)}`} aria-pressed={selected === id} onClick={() => setSelected(id)} onKeyDown={e => activate(e, () => setSelected(id))} opacity={uncertain ? .45 : 1}>
        <rect className="car-hit" x={px(position.x) - 24} y={py(position.y) - 24} width="48" height="48" fill="transparent"/>
        {selected === id && <circle cx={px(position.x)} cy={py(position.y)} r={Math.max(bodyWidth, bodyHeight) / 2 + 8} fill="none" stroke="#36788f" strokeWidth="3"/>}
        {vehicle && heading !== null && length !== null && breadth !== null ? <rect x={px(position.x) - bodyWidth / 2} y={py(position.y) - bodyHeight / 2} width={bodyWidth} height={bodyHeight} rx="3" fill={selected === id ? '#36788f' : '#343330'} stroke={uncertain ? '#9c6e32' : 'none'} strokeDasharray={uncertain ? '4 3' : undefined} transform={`rotate(${-heading} ${px(position.x)} ${py(position.y)})`}/> : <circle cx={px(position.x)} cy={py(position.y)} r="7" fill={vehicle ? '#343330' : '#5d7959'}/>}
        <text x={px(position.x)} y={py(position.y) - Math.max(bodyWidth, bodyHeight) / 2 - 12} textAnchor="middle">{id}</text>
      </g>;
    })}
  </svg></div><p className="map-scroll-hint">지도를 좌우로 밀어 확인하세요.</p><div className="map-legend"><span><i className="swatch"/>차량 관측</span><span><i className="swatch walk"/>보행자 관측</span></div>{!coverageAvailable && <div className="card-body"><Notice title="관측 불가 · 현재 위치를 표시하지 않습니다." neutral/></div>}{current && <div className="card-body map-selection" role="status"><PositionFacts object={current} unavailable={!coverageAvailable}/></div>}</>;
}

function DeviceFacts({ device }: { device: Row }) {
  const fields = device.type === 'gate' || device.gate_id || device.device_id ? [['게이트', device.gate_id || device.device_id], ['입차 정책', label(device.entry_policy)], ['물리 상태', label(device.physical_state)], ['장애물 관측', device.obstacle_detected === null || device.obstacle_detected === undefined ? '미확인' : valueText(device.obstacle_detected)], ['피드백', label(device.last_feedback)], ['버전', device.resource_version]]
    : device.type === 'broadcast' || device.operation_id ? [['방송 요청', device.operation_id], ['구역', device.zone_id], ['접수', label(device.receipt)], ['합성 재생', label(device.simulated_playback)], ['브라우저 재생', label(device.browser_playback)]]
      : [['경보 구역', device.zone_id], ['요청 활성', valueText(device.desired_active)], ['시각 경보', label(device.visual)], ['음향 장치', label(device.audio)], ['버전', device.resource_version]];
  return <Facts items={fields.map(([name, value]) => [text(name), valueText(value)])}/>;
}
function IncidentRow({ incident }: { incident: Row }) {
  const id = text(incident.incident_id, '');
  return <a className="list-item clickable" href={`#/owner/incidents/${encodeURIComponent(id)}`}><span className={`list-dot ${closedIncident(incident) ? 'green' : ''}`}/><span className="list-text"><strong>{text(incident.reason_summary, id)}</strong><small>{text(incident.primary_object_id)} · {stamp(incident.created_at)}</small><Badge tone={closedIncident(incident) ? 'green' : ['needs_review', 'escalated'].includes(text(incident.status, '')) ? 'amber' : 'red'}>{label(incident.status)}</Badge></span><span className="list-arrow" aria-hidden="true">↗</span></a>;
}
export function LiveMonitor() {
  const { data, refresh } = useLive(); const [deviceId, setDeviceId] = useState('');
  const view = row(data.view); const snapshot = row(view.snapshot); const run = text(snapshot.run_id, ''); const objects = rows(snapshot.objects); const active = data.incidents.filter(item => !closedIncident(item));
  const device = data.devices.find(d => text(d.gate_id || d.device_id || d.operation_id || d.zone_id, '') === deviceId) || rows(snapshot.devices).find(d => text(d.device_id, '') === deviceId);
  return <><Head title="관제 현황" action={<RouteLink to="/owner/commands" className="btn primary">운영 요청</RouteLink>}/>{!run ? <Card><Empty title="실행 없음"/><Button onClick={() => void refresh()}>다시 조회</Button></Card> : <><div className="stats"><div className="stat"><span>관측 차량</span><strong>{snapshot.coverage === 'unavailable' ? '미확인' : objects.filter(o => o.object_type === 'vehicle').length}</strong></div><div className="stat"><span>진행 사건</span><strong className="accent">{active.length}</strong></div><div className="stat"><span>검토 필요</span><strong>{data.incidents.filter(i => ['needs_review', 'escalated'].includes(text(i.status, ''))).length}</strong></div><div className="stat"><span>실행 상태</span><strong className="policy-value">{label(view.run_status)}</strong></div></div>
    {view.recovery_required === true && <Notice title="복구 확인 필요" neutral>자동으로 업무를 재개하지 않습니다.</Notice>}
    <div className="grid two"><Card title="주차장 지도" body={false} action={<Badge>{label(snapshot.coverage)}</Badge>} footer={`관측: ${stamp(snapshot.observed_at)}`}>{data.map ? <LiveMap map={row(data.map)} snapshot={snapshot} onDevice={setDeviceId}/> : <Empty title="지도 정보 없음"/>}</Card><div className="stack"><Card title="진행 사건" body={false} action={<RouteLink to="/owner/incidents" className="btn-link">전체 보기</RouteLink>}>{active.length ? active.map(i => <IncidentRow key={text(i.incident_id)} incident={i}/>) : <Empty title="진행 사건 없음"/>}</Card><Card title="관측 상태"><SnapshotFacts snapshot={snapshot} view={view}/></Card></div></div>
    <div className="device-grid">{data.devices.map((d, i) => { const id = text(d.gate_id || d.device_id || d.operation_id || d.zone_id, ''); return <button type="button" className="device-tile" key={`${text(d.type)}:${id}:${i}`} onClick={() => setDeviceId(id)}><Icon name={d.type === 'gate' ? 'gate' : d.type === 'broadcast' ? 'sound' : 'bell'}/><strong>{d.type === 'gate' ? '입출차 게이트' : d.type === 'broadcast' ? '안내 방송' : '시각·음향 경보'}</strong><small>{id} · {label(d.physical_state || d.receipt || d.visual)} ↗</small></button>; })}</div></>}{deviceId && <Dialog title="장치 상태" onClose={() => setDeviceId('')} drawer>{device ? <DeviceFacts device={device}/> : <Empty title="현재 장치 관측 없음"/>}<p className="pending-features section-gap">합성 장치 관측 · 현장 장치 확인 아님</p></Dialog>}</>;
}
export function LiveIncidents() {
  const { data, refresh } = useLive(); const [filter, setFilter] = useState('all');
  const filtered = data.incidents.filter(i => filter === 'all' || (filter === 'resolved' ? closedIncident(i) : filter === 'review' ? ['needs_review', 'escalated'].includes(text(i.status, '')) : !closedIncident(i)));
  return <><Head title="사건 관리" action={<Badge>{filtered.length}건</Badge>}/><div className="filters" role="group" aria-label="사건 상태 필터">{[['all', '전체'], ['active', '진행 중'], ['review', '검토 필요'], ['resolved', '종료']].map(([value, name]) => <button type="button" className="filter" key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{name}</button>)}</div><Card body={false}>{filtered.length ? filtered.map(i => <IncidentRow key={text(i.incident_id)} incident={i}/>) : <Empty title="사건 없음"/>}</Card><div className="section-gap"><Button onClick={() => void refresh()}>다시 조회</Button></div></>;
}
export function LiveIncidentDetail({ id }: { id: string }) {
  const { value, loading, error, reload } = useResource(id ? `/api/v1/incidents/${encodeURIComponent(id)}` : null);
  if (loading) return <><Head title="사건 상세"/><Card body={false}><Loading/></Card></>;
  if (error) return <><Head title="사건 상세"/><Notice title={error}/><Button onClick={reload}>다시 조회</Button><RouteLink to="/owner/incidents">사건 목록</RouteLink></>;
  if (!value || !value.incident_id) return <><Head title="사건 없음"/><RouteLink to="/owner/incidents">사건 목록</RouteLink></>;
  const impacts = rows(value.impacts);
  return <><RouteLink to="/owner/incidents" className="back">사건 목록으로</RouteLink><Head title="사건 상세" action={<Badge tone={closedIncident(value) ? 'green' : 'amber'}>{label(value.status)}</Badge>}/><div className="detail-grid"><div className="stack"><Card title="사건 정보"><p className="incident-summary">{text(value.reason_summary)}</p><div className="section-gap"><Facts items={[
    ['사건', text(value.incident_id)], ['관측 객체', text(value.primary_object_id)], ['발생 시각', stamp(value.created_at)], ['변경 시각', stamp(value.updated_at)], ['버전', text(value.resource_version)],
  ]}/></div></Card><Card title="영향 정보">{impacts.length ? impacts.map((impact, i) => { return <div key={text(impact.impact_id, String(i))} className={i ? 'section-gap' : ''}><Badge>{label(impact.type)}</Badge><div className="section-gap"><ImpactFacts impact={impact}/></div></div>; }) : <Empty title="영향 정보 없음"/>}</Card></div><div className="stack"><Card title="사건 위치"><RouteLink to="/owner/monitor" className="btn full">지도 보기</RouteLink></Card><Button onClick={reload}>최신 내용 조회</Button></div></div><IncidentTimelineView id={id}/></>;
}

type Attempt = { actionId: string; body: Row };
function useAlive() { const alive = useRef(true); useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []); return alive; }
export function LiveVehicle() {
  const { api, data, facilityId, epoch, refresh, rememberCommand, blocked, toast } = useLive();
  const [selected, setSelected] = useState(''); const [reportOpen, setReportOpen] = useState(false); const [description, setDescription] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [ambiguous, setAmbiguous] = useState(false); const [accepted, setAccepted] = useState('');
  const pending = useRef(false); const attempt = useRef<Attempt | null>(null); const alive = useAlive();
  const vehicle = data.vehicles.find(v => text(v.registered_vehicle_id, '') === selected); const view = row(data.view); const snapshot = row(view.snapshot); const runId = text(snapshot.run_id, ''); const version = number(view.applied_state_version);
  useEffect(() => { if (selected && !data.vehicles.some(v => text(v.registered_vehicle_id, '') === selected)) { setSelected(''); setReportOpen(false); } }, [data.vehicles, selected]);
  async function submitReport() {
    if (pending.current || blocked || !vehicle || !runId || version === null || !description.trim()) return;
    if (!attempt.current) attempt.current = { actionId: `report:${epoch}:${crypto.randomUUID()}`, body: { run_id: runId, purpose: 'report_exit_blocked', target_vehicle_id: selected, based_on_state_version: version, text: description.trim() } };
    pending.current = true; setBusy(true); setError('');
    try {
      const result = await api.mutate(attempt.current.actionId, `/api/v1/facilities/${encodeURIComponent(facilityId)}/commands`, attempt.current.body);
      const id = text(result.command_id, ''); if (!id) throw new ApiError(0, 'INVALID_RESPONSE', '접수 결과를 확인하지 못했습니다.', true);
      if (!alive.current) return;
      attempt.current = null; setAmbiguous(false); setAccepted(id); setDescription(''); setReportOpen(false); rememberCommand(id); toast('신고 접수 · 처리 대기'); await refresh().catch(() => { if (alive.current) toast('신고 접수 완료 · 최신 정보 다시 조회 필요'); });
    } catch (cause) {
      if (!alive.current || cause instanceof DOMException && cause.name === 'AbortError') return;
      const unknown = cause instanceof ApiError && cause.ambiguous; setError(errorMessage(cause)); setAmbiguous(unknown); if (!unknown) attempt.current = null;
    } finally { pending.current = false; if (alive.current) setBusy(false); }
  }
  return <><Head title="내 차량"/><div className="grid equal"><Card title="등록 차량"><Field label="차량 선택" as="select" input={{ value: selected, disabled: busy || ambiguous, onChange: e => { setSelected(e.target.value); setAccepted(''); setError(''); } }}><option value="">차량을 선택해 주세요.</option>{data.vehicles.map(v => <option key={text(v.registered_vehicle_id)} value={text(v.registered_vehicle_id)}>{text(v.display_alias, text(v.registered_vehicle_id))}</option>)}</Field>{vehicle ? <><div className="vehicle-title">{text(vehicle.display_alias)}</div><Facts items={[
    ['등록 차량', text(vehicle.registered_vehicle_id)],
  ]}/><Button variant="primary full" disabled={blocked || !runId || version === null} onClick={() => setReportOpen(true)}>출차 방해 신고</Button></> : <Empty title={data.vehicles.length ? '차량을 선택해 주세요.' : '등록 차량 없음'}/>}{accepted && <Notice title="신고 접수 · 처리 대기" neutral>{accepted}</Notice>}</Card><div className="stack"><OwnVehicleObservation selected={selected}/><Card title="알림"><RouteLink to="/driver/notifications" className="btn full">내 알림 보기</RouteLink></Card></div></div><CommandsHistory driver/>{reportOpen && <Dialog title="출차 방해 신고" onClose={() => { if (!busy) setReportOpen(false); }}><form onSubmit={e => { e.preventDefault(); void submitReport(); }}><Facts items={[
    ['신고 차량', text(vehicle?.display_alias)], ['관측 버전', version === null ? '미제공' : String(version)],
  ]}/><div className="section-gap"><Field label="상황 설명" as="textarea" input={{ value: description, required: true, maxLength: 2000, disabled: busy || ambiguous, onChange: e => setDescription(e.target.value) }}/></div>{error && <Notice title={error}/>}<div className="dialog-actions"><Button disabled={busy} onClick={() => setReportOpen(false)}>닫기</Button><Button type="submit" variant="primary" disabled={busy || blocked || !description.trim()}>{busy ? '접수 중' : ambiguous ? '같은 신고 결과 확인' : '신고 접수'}</Button></div></form></Dialog>}</>;
}

type ReceiptAttempt = Attempt & { state: 'pending' | 'complete' | 'failed'; error: string };
const receipts = new Map<string, ReceiptAttempt>();
const receiptListeners = new Set<() => void>();
function receiptChanged() { receiptListeners.forEach(listener => listener()); }
export function clearLivePageMemory() { receipts.clear(); receiptChanged(); }
function useReceipts(items: Row[]) {
  const { api, epoch, refresh, blocked } = useLive(); const [, changed] = useState(0); const alive = useAlive(); const itemsRef = useRef(items); itemsRef.current = items;
  const ids = items.map(n => text(n.notification_id, '')).filter(Boolean).join('|');
  async function receive(id: string, manual = false) {
    if (blocked || !itemsRef.current.some(n => n.notification_id === id)) return;
    const key = `${epoch}:${id}`; let attempt = receipts.get(key);
    if (attempt && (attempt.state !== 'failed' || !manual)) return;
    if (!attempt) { attempt = { actionId: `receipt:${key}`, body: { client_request_id: crypto.randomUUID(), received_at: new Date().toISOString() }, state: 'pending', error: '' }; receipts.set(key, attempt); }
    attempt.state = 'pending'; attempt.error = ''; receiptChanged();
    try {
      const result = await api.mutate(attempt.actionId, `/api/v1/notifications/${encodeURIComponent(id)}/receipts`, attempt.body);
      if (!text(result.receipt_id, '')) throw new ApiError(0, 'INVALID_RESPONSE', '수신 확인 결과를 확인하지 못했습니다.', true);
      attempt.state = 'complete'; receiptChanged(); if (alive.current) await refresh().catch(() => undefined);
    } catch (cause) { if (cause instanceof DOMException && cause.name === 'AbortError') return; attempt.state = 'failed'; attempt.error = errorMessage(cause); receiptChanged(); }
  }
  useEffect(() => { const listener = () => changed(v => v + 1); receiptListeners.add(listener); return () => { receiptListeners.delete(listener); }; }, []);
  useEffect(() => { for (const key of receipts.keys()) if (!key.startsWith(`${epoch}:`)) receipts.delete(key); for (const id of ids.split('|').filter(Boolean)) void receive(id); }, [epoch, ids, blocked]);
  return { receive, status: (id: string) => receipts.get(`${epoch}:${id}`) };
}
function NotificationRow({ notification }: { notification: Row }) {
  const { role } = useLive(); const message = row(notification.message);
  return <a className="list-item clickable" href={`#/${role}/notifications/${encodeURIComponent(text(notification.notification_id, ''))}`}><span className="list-dot"/><span className="list-text"><strong>{text(message.text || message.summary, notification.purpose === 'owner_report' ? '운영 보고' : '알림')}</strong><small>{stamp(notification.created_at)}</small><Badge>{label(notification.delivery_status)}</Badge></span><span className="list-arrow" aria-hidden="true">↗</span></a>;
}
export function LiveNotifications() {
  const { data, refresh } = useLive(); const received = useReceipts(data.notifications);
  return <><Head title="알림" action={<Badge>{data.notifications.length}건</Badge>}/><Card body={false}>{data.notifications.length ? data.notifications.map(notification => { const id = text(notification.notification_id, ''); const receipt = received.status(id); return <div key={id}><NotificationRow notification={notification}/>{receipt?.state === 'failed' && <div className="card-body"><Notice title={receipt.error}/><Button onClick={() => void received.receive(id, true)}>수신 확인 재시도</Button></div>}</div>; }) : <Empty title="알림 없음"/>}</Card><div className="section-gap"><Button onClick={() => void refresh()}>다시 조회</Button></div></>;
}
export function LiveNotificationDetail({ id }: { id: string }) {
  const { api, data, role, epoch, refresh, blocked, toast } = useLive(); const notification = data.notifications.find(n => n.notification_id === id); const receipt = useReceipts(notification ? [notification] : []);
  const [choice, setChoice] = useState(''); const [description, setDescription] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [ambiguous, setAmbiguous] = useState(false); const [saved, setSaved] = useState('');
  const pending = useRef(false); const attempt = useRef<Attempt | null>(null); const alive = useAlive();
  const scope = useRef(`${epoch}:${id}`); scope.current = `${epoch}:${id}`;
  useEffect(() => { setChoice(''); setDescription(''); setError(''); setAmbiguous(false); setSaved(''); setBusy(false); pending.current = false; attempt.current = null; }, [epoch, id]);
  if (!notification) return <><Head title="알림 없음"/><RouteLink to={`/${role}/notifications`}>알림 목록</RouteLink></>;
  const message = row(notification.message); const receiptState = receipt.status(id); const responses = rows(notification.responses); const latest = responses.at(-1);
  async function respond() {
    if (pending.current || blocked || !choice || !notification) return;
    if (!attempt.current) attempt.current = { actionId: `response:${epoch}:${id}:${crypto.randomUUID()}`, body: { client_request_id: crypto.randomUUID(), response: choice, ...(description.trim() ? { text: description.trim() } : {}) } };
    const scopeAtStart = scope.current;
    pending.current = true; setBusy(true); setError('');
    try {
      const result = await api.mutate(attempt.current.actionId, `/api/v1/notifications/${encodeURIComponent(id)}/responses`, attempt.current.body);
      if (!text(result.response_id, '')) throw new ApiError(0, 'INVALID_RESPONSE', '응답 접수 결과를 확인하지 못했습니다.', true);
      if (!alive.current || scope.current !== scopeAtStart) return; attempt.current = null; setAmbiguous(false); setSaved(choice); toast('응답 접수 · 차량 이동 확인은 별도'); await refresh().catch(() => { if (alive.current && scope.current === scopeAtStart) toast('응답 접수 완료 · 최신 정보 다시 조회 필요'); });
    } catch (cause) { if (!alive.current || scope.current !== scopeAtStart || cause instanceof DOMException && cause.name === 'AbortError') return; const unknown = cause instanceof ApiError && cause.ambiguous; setError(errorMessage(cause)); setAmbiguous(unknown); if (!unknown) attempt.current = null; }
    finally { if (scope.current === scopeAtStart) { pending.current = false; if (alive.current) setBusy(false); } }
  }
  return <><RouteLink to={`/${role}/notifications`} className="back">알림 목록으로</RouteLink><Head title={notification.purpose === 'owner_report' ? '운영 보고' : '차량 이동 요청'} action={<Badge>{label(notification.delivery_status)}</Badge>}/>{role === 'owner' && typeof notification.incident_id === 'string' && <div className="section-gap"><RouteLink to={`/owner/incidents/${encodeURIComponent(notification.incident_id)}`}>관련 사건 보기</RouteLink></div>}<div className="grid equal"><Card title="알림 내용"><p>{text(message.text || message.summary, '메시지 내용 미제공')}</p><div className="section-gap"><Facts items={[
    ['알림 시각', stamp(notification.created_at)], ['응답 기한', stamp(notification.response_due_at)], ['전달 상태', label(notification.delivery_status)], ['채널', label(notification.mode)],
    ...(typeof message.zone_label === 'string' ? [['구역', message.zone_label]] : []),
  ]}/></div>{receiptState?.state === 'failed' && <><Notice title={receiptState.error}/><Button onClick={() => void receipt.receive(id, true)}>수신 확인 재시도</Button></>}{receiptState?.state === 'complete' && <p className="form-note section-gap">화면 수신 확인 접수</p>}{role === 'driver' && notification.purpose === 'move_request' && <form onSubmit={e => { e.preventDefault(); void respond(); }}><div className="reply-buttons" role="group" aria-label="차주 응답">{[['acknowledged', '확인했어요'], ['will_move', '이동할게요'], ['cannot_move', '이동하기 어려워요'], ['question', '문의할게요']].map(([value, name]) => <Button key={value} variant={choice === value ? 'selected' : ''} aria-pressed={choice === value} disabled={busy || ambiguous || blocked} onClick={() => { setChoice(value); setError(''); }}>{name}</Button>)}</div><Field label="추가 설명" as="textarea" input={{ value: description, maxLength: 1000, disabled: busy || ambiguous, onChange: e => setDescription(e.target.value) }}/>{error && <Notice title={error}/>}<Button type="submit" variant="primary full" disabled={busy || blocked || !choice}>{busy ? '응답 접수 중' : ambiguous ? '같은 응답 결과 확인' : '응답 보내기'}</Button>{saved && <div className="section-gap"><Notice title={`응답 접수: ${label(saved)}`} neutral>차량 이동·통로 회복·사건 해결은 별도 확인이 필요합니다.</Notice></div>}</form>}</Card><Card title="응답 기록">{latest ? responses.map((response, i) => <div key={`${text(response.responded_at)}:${i}`} className={i ? 'section-gap' : ''}><Badge>{label(response.response)}</Badge><p className="form-note">{stamp(response.responded_at)} · {label(response.response_source)}</p>{typeof response.text === 'string' && <p>{response.text}</p>}</div>) : <Empty title="응답 없음"/>}<p className="form-note section-gap">응답 접수는 차량 이동이나 사건 해결을 뜻하지 않습니다.</p></Card></div></>;
}
