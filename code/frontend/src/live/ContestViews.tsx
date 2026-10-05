import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Badge, Button, Card, Empty, Facts, Field, Loading, Notice, RouteLink } from '../components';
import { useLive } from './context';
import { cursorNeedsRestart, getCommandProgress, getHistoryPage, getParkingMap, getVehicleLocations } from './viewsApi';
import type { CommandProgress, HistoryCommand, HistoryExecution, HistoryKind, HistoryPage, OwnParkingMap, TimelineRecord, VehicleLocations } from './viewsApi';

const encode = encodeURIComponent;
const names: Record<string, string> = {
  unknown: '미확인', pending: '처리 대기', running: '진행 중', succeeded: '성공', failed: '실패', held: '보류', cancelled: '취소',
  accepted: '접수', requested: '요청', partial: '일부 완료', completed: '계획 완료', proposed: '계획 확인 대기', active: '진행 중',
  live: 'live · 로컬 실행 경로', synthetic_demo: '합성 시연', simulated: '모의 실행', query: '일반 질문', operational_goal: '운영 목표',
  report_exit_blocked: '출차 방해 신고', own_vehicle_query: '본인 차량 조회', play_announcement: '안내 방송', set_entry_policy: '입차 정책 변경',
  channel_accepted: '채널 접수', client_received: '화면 수신', acknowledged: '확인했어요', will_move: '이동할게요', cannot_move: '이동하기 어려워요',
  question: '문의할게요', queued: '전달 대기', resolved: '해결', monitoring: '후속 확인 중', needs_review: '검토 필요',
  paused: '일시정지', stopped: '중지', replaying: '재생 중', visible: '관측됨', occluded: '가림', missing: '관측 없음',
};
const label = (value?: string | null) => value ? names[value] || value : '미제공';
const time = (value?: string | null) => value ? new Date(value).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false }) + ' KST' : '미제공';
const errorText = (error: unknown) => error instanceof Error ? error.message : '조회하지 못했습니다.';
const aborted = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
const reasonNames: Record<string, string> = {
  stationary_whole_footprint_in_unique_bay: '차량 전체가 하나의 주차면 안에 정차한 관측 근거',
  no_verified_relationship: '확인된 차량 연결 없음', ambiguous_relationship: '차량 연결 확인 필요', target_not_observed: '본인 차량 관측 없음',
  observation_unavailable: '관측 불가', observation_insufficient: '관측 품질 부족', stale_observation: '관측 갱신 필요', run_not_ready: '회차·복구 확인 필요',
  moving: '이동 관측', insufficient_history: '연속 관측 부족', uncertain_stationarity: '정차 확인 필요',
  outside_or_intruding_bay: '주차면 안 정차 확인 전', ambiguous_bay: '주차면 식별 불확실', unsupported_geometry: '지도 분석 범위 밖',
};

function useTyped<T>(key: string | null, read: () => Promise<T>, updates = true, retainOnRefresh = false) {
  const { epoch, revision } = useLive(); const serial = useRef(0); const reader = useRef(read); reader.current = read;
  const scope = key === null ? null : `${epoch}:${key}`;
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<{ key: string | null; value: T | null; loading: boolean; error: string }>({ key, value: null, loading: !!key, error: '' });
  const update = updates ? revision : 0;
  useEffect(() => {
    const ticket = ++serial.current;
    setState(previous => ({ key: scope, value: retainOnRefresh && previous.key === scope ? previous.value : null, loading: !!key, error: '' }));
    if (key) void reader.current().then(value => { if (ticket === serial.current) setState({ key: scope, value, loading: false, error: '' }); })
      .catch(error => { if (ticket === serial.current && !aborted(error)) setState({ key: scope, value: null, loading: false, error: errorText(error) }); });
    return () => { serial.current++; };
  }, [key, epoch, update, retry, retainOnRefresh]);
  return { ...(state.key === scope ? state : { value: null, loading: !!key, error: '' }), reload: () => setRetry(v => v + 1) };
}

export function OwnVehicleObservation({ selected }: { selected: string }) {
  const { api, data, facilityId, blocked } = useLive();
  const runId = typeof data.readiness?.current_run_id === 'string' ? data.readiness.current_run_id : '';
  const observation = useTyped<VehicleLocations>(runId ? `${facilityId}:${runId}` : null,
    () => getVehicleLocations(api, facilityId, runId), true, true);
  const reload = useRef(observation.reload); reload.current = observation.reload;
  useEffect(() => {
    if (!runId || observation.loading) return;
    const timer = setTimeout(() => reload.current(), 5000);
    return () => clearTimeout(timer);
  }, [runId, observation.loading, observation.value, observation.error]);
  const version = observation.value?.map_version;
  const map = useTyped<OwnParkingMap>(`${facilityId}:${version || ''}`, () => getParkingMap(api, facilityId, version), false, true);
  const vehicle = observation.value?.vehicles.find(v => v.registered_vehicle_id === selected);
  return <Card title="본인 차량 위치" action={<Button onClick={() => { observation.reload(); map.reload(); }}>다시 조회</Button>}>
    {observation.loading && !observation.value ? <Loading/> : observation.error ? <Notice title={observation.error}/> : !runId ? <Empty title="관측 회차 없음"/> : observation.value && <>
      {observation.loading && <p className="form-note" role="status">관측 갱신 중 · 아래는 마지막 수신 결과입니다.</p>}
      <Facts items={[
        ['회차', observation.value.run_id], ['관측 시각', time(observation.value.observed_at)], ['수신 시각', time(observation.value.received_at)],
        ['관측 버전', String(observation.value.state_version)], ['적용 버전', String(observation.value.applied_state_version)],
        ['합성 관측 시각', `${observation.value.sim_time_ms} ms`], ['합성 적용 시각', `${observation.value.applied_sim_time_ms} ms`],
        ['관측 범위', { complete: '전체 관측', partial: '부분 관측', unavailable: '관측 불가' }[observation.value.coverage]],
        ['회차 상태', label(observation.value.run_status)], ['복구 확인', observation.value.recovery_required ? '필요' : '불필요'],
      ]}/>
      {vehicle ? <div className="section-gap"><Badge tone={vehicle.location_status === 'unknown' ? 'amber' : ''}>{vehicle.location_status === 'observed_bay' ? '주차면 관측' : vehicle.location_status === 'observed_position' ? '좌표 관측 · 주차면 확인 전' : '현재 위치 미확인'}</Badge><div className="section-gap"><Facts items={[
        ['관측된 주차면', vehicle.observed_bay_id || '미확인'], ['좌표', vehicle.position ? `x ${vehicle.position.x.toFixed(2)} m · y ${vehicle.position.y.toFixed(2)} m` : '현재 위치 미확인'],
        ['판정 근거', reasonNames[vehicle.location_reason] || vehicle.location_reason],
        ['관측 품질', vehicle.quality ? label(vehicle.quality.visibility) : '미제공'],
        ['위치 불확실성', vehicle.quality?.uncertainty_m == null ? '미제공' : `${vehicle.quality.uncertainty_m} m`],
        ['연속 정차 후보', vehicle.stationary_duration_ms === null ? '미제공' : `${vehicle.stationary_duration_ms} ms`],
      ]}/></div></div> : <Empty title={selected ? '이 차량의 위치 조회 결과 없음' : '차량을 선택해 주세요.'}/>}
      <p className="form-note section-gap">주차면은 관측 결과입니다. 예약·배정·점유 확정을 뜻하지 않습니다.</p>
    </>}
    <div className="section-gap">{map.loading && !map.value ? <Loading/> : map.error ? <Notice title={map.error}/> : map.value && <>
      <p className="form-note">정적 지도 · {map.value.map_version} · m · 남서쪽 원점 · 동쪽 x / 북쪽 y</p>
      <OwnMap map={map.value} vehicle={blocked || observation.error ? undefined : vehicle} compatible={map.value.map_version === observation.value?.map_version}/>
    </>}</div>
  </Card>;
}
function OwnMap({ map, vehicle, compatible }: { map: OwnParkingMap; vehicle?: VehicleLocations['vehicles'][number]; compatible: boolean }) {
  const { min_x: x0, min_y: y0, max_x: x1, max_y: y1 } = map.bounds;
  if (x1 <= x0 || y1 <= y0) return <Empty title="지도 좌표를 확인하지 못했습니다."/>;
  const scale = 16; const width = (x1 - x0) * scale; const height = (y1 - y0) * scale;
  const px = (x: number) => (x - x0) * scale; const py = (y: number) => (y1 - y) * scale;
  const position = compatible ? vehicle?.position : null;
  return <><div className="map-wrap live-map-wrap" tabIndex={0} aria-label="본인 차량 정적 주차 지도"><svg className="parking-map" viewBox={`-12 -12 ${width + 24} ${height + 24}`} role="img" aria-label="정적 지도와 확인된 본인 차량 관측">
    <rect width={width} height={height} fill="#edf5fa" stroke="#c2d6e2"/>
    {/* 방송 범위는 도로가 아니므로 지면을 덮는 도형으로 표시하지 않는다. */}
    {map.zones.filter(zone => zone.type !== 'announcement').map(zone => <g key={zone.zone_id}><polygon data-zone-type={zone.type} points={zone.polygon.map(p => `${px(p.x)},${py(p.y)}`).join(' ')} fill={zone.type === 'parking_bay' ? '#ffffff' : zone.type === 'pedestrian' || zone.type === 'walkway' ? '#d6dfcc' : ['aisle', 'entrance', 'exit'].includes(zone.type) ? '#9caebb' : '#e0edf5'} stroke={compatible && vehicle?.observed_bay_id === zone.zone_id ? '#36788f' : '#708695'} strokeWidth={compatible && vehicle?.observed_bay_id === zone.zone_id ? 3 : 1}/>{zone.type === 'parking_bay' && zone.zone_id.length <= 6 && zone.polygon.length > 0 && <text x={px(zone.polygon.reduce((sum, p) => sum + p.x, 0) / zone.polygon.length)} y={py(zone.polygon.reduce((sum, p) => sum + p.y, 0) / zone.polygon.length)} textAnchor="middle" fontSize="13">{zone.zone_id}</text>}</g>)}
    {position && <g><circle cx={px(position.x)} cy={py(position.y)} r="8" fill="#36788f"/><text x={px(position.x)} y={py(position.y) - 16} textAnchor="middle" fontSize="14">내 차량</text></g>}
  </svg></div><p className="map-scroll-hint">지도를 좌우로 밀어 확인하세요.</p></>;
}

function useHistory<T>(kind: HistoryKind, id: string, runId?: string) {
  const { api, epoch } = useLive(); const [cursor, setCursor] = useState<string | undefined>(); const [restart, setRestart] = useState(0);
  const serial = useRef(0); const lock = useRef(false);
  const [state, setState] = useState<{ page: HistoryPage<T> | null; loading: boolean; error: string; restartRequired: boolean }>({ page: null, loading: true, error: '', restartRequired: false });
  useEffect(() => {
    const ticket = ++serial.current; lock.current = true;
    setState({ page: null, loading: true, error: '', restartRequired: false });
    void getHistoryPage<T>(api, kind, id, runId, cursor).then(page => {
      if (ticket === serial.current) setState({ page, loading: false, error: '', restartRequired: false });
    }).catch(error => {
      if (ticket === serial.current && !aborted(error)) setState({ page: null, loading: false, error: errorText(error), restartRequired: cursorNeedsRestart(error) });
    }).finally(() => { if (ticket === serial.current) lock.current = false; });
    return () => { serial.current++; };
  }, [api, kind, id, runId, epoch, cursor, restart]);
  return { ...state, next: () => { if (!lock.current && state.page?.next_cursor) { lock.current = true; setCursor(state.page.next_cursor); } },
    reload: () => { setCursor(undefined); setRestart(v => v + 1); } };
}
function HistoryPanel<T>({ kind, id, runId, title, render }: { kind: HistoryKind; id: string; runId?: string; title: string; render: (item: T) => ReactNode }) {
  const { epoch } = useLive();
  return <HistoryContent<T> key={`${epoch}:${kind}:${id}:${runId || ''}`} kind={kind} id={id} runId={runId} title={title} render={render}/>;
}
function HistoryContent<T>({ kind, id, runId, title, render }: { kind: HistoryKind; id: string; runId?: string; title: string; render: (item: T) => ReactNode }) {
  const history = useHistory<T>(kind, id, runId);
  return <Card title={title} action={<Button disabled={history.loading} onClick={history.reload}>처음부터 다시 조회</Button>}>
    {history.loading ? <Loading/> : history.error ? <Notice title={history.error}>{history.restartRequired ? '이전 커서는 더 사용할 수 없습니다. 처음부터 다시 조회해 주세요.' : '조회 조건·현재 권한을 확인한 뒤 다시 조회해 주세요.'}</Notice> : history.page && <>
      <p className="form-note">조회 기준 {time(history.page.as_of_utc)} · 현재 권한으로 확인한 저장 상태</p>
      {history.page.items.length ? history.page.items.map(render) : <Empty title="이 페이지에 표시할 기록 없음">{history.page.next_cursor ? '다음 페이지에 허용된 기록이 있을 수 있습니다.' : '조회 끝'}</Empty>}
      <Preservation limits={history.page.preservation_limits}/>
      <Button disabled={!history.page.next_cursor} onClick={history.next}>다음 페이지</Button>
      <p className="form-note section-gap">새 기록·상태 변경은 처음부터 다시 조회하면 확인됩니다.</p>
    </>}
  </Card>;
}
function Preservation({ limits }: { limits: string[] }) {
  return <details className="section-gap"><summary>기록 보존 범위</summary><p className="form-note">현재 저장 상태와 실제 보존된 기록을 표시합니다. 저장되지 않은 중간 전환·이동·해결 시각은 추정하지 않습니다.</p>{limits.map((limit, index) => <p className="form-note" key={index}>{limit}</p>)}</details>;
}
export function CommandsHistory({ driver = false }: { driver?: boolean }) {
  const { facilityId } = useLive(); const [runId, setRunId] = useState(''); const [draft, setDraft] = useState('');
  return <><form className="section-gap" onSubmit={e => { e.preventDefault(); setRunId(draft.trim()); }}><Field label="회차 ID (비우면 전체 회차)" input={{ value: draft, onChange: e => setDraft(e.target.value) }}/><Button type="submit">이력 조건 적용</Button></form><HistoryPanel<HistoryCommand> kind="commands" id={facilityId} runId={runId || undefined} title={driver ? '내 요청 이력' : '요청 이력'} render={item => <div className="list-item" key={item.command_id}><div className="list-text"><RouteLink className="btn-link" to={`/${driver ? 'driver' : 'owner'}/commands/${encode(item.command_id)}`}>{label(item.purpose)} · {item.command_id}</RouteLink><Badge>{label(item.aggregate_status)}</Badge><small>회차 {item.run_id} · 접수 {time(item.created_at)}</small><small>변경 {time(item.updated_at)} · 버전 {item.resource_version}</small>{item.cancellation_requested_at && <small>취소 요청 {time(item.cancellation_requested_at)}</small>}</div></div>}/></>;
}
export function ExecutionsHistory() {
  const { facilityId } = useLive(); const [runId, setRunId] = useState(''); const [draft, setDraft] = useState('');
  return <><form className="section-gap" onSubmit={e => { e.preventDefault(); setRunId(draft.trim()); }}><Field label="실행 이력 회차 ID (비우면 전체 회차)" input={{ value: draft, onChange: e => setDraft(e.target.value) }}/><Button type="submit">이력 조건 적용</Button></form><HistoryPanel<HistoryExecution> kind="executions" id={facilityId} runId={runId || undefined} title="실행 이력" render={item => <div className="list-item" key={item.execution_id}><div className="list-text"><strong>{label(item.tool_name)} · {item.execution_id}</strong><ExecutionFacts execution={item}/></div></div>}/></>;
}
function ExecutionFacts({ execution: e }: { execution: HistoryExecution }) {
  return <><Badge>{label(e.status)} · {label(e.mode)}</Badge><Facts items={[
    ['회차', e.run_id], ['계획', e.plan_id || '미제공'], ['사건', e.incident_id || '미제공'],
    ['기록 시각', time(e.created_at)], ['갱신 시각', time(e.updated_at)], ['합성 적용 시각', e.applied_sim_time_ms == null ? '미제공' : `${e.applied_sim_time_ms} ms`],
    ['오류', e.error_code || '없음'],
  ]}/>{e.command_id && <RouteLink className="btn-link" to={`/owner/commands/${encode(e.command_id)}`}>요청 {e.command_id}</RouteLink>}</>;
}
export function CommandProgressView({ id }: { id: string }) {
  const { api } = useLive(); const progress = useTyped<CommandProgress>(id, () => getCommandProgress(api, id));
  return <Card title="계획·단계·실행 시도" action={<Button onClick={progress.reload}>진행 다시 조회</Button>}>
    {progress.loading ? <Loading/> : progress.error ? <Notice title={progress.error}/> : progress.value && <>
      <Facts items={[["요청 전체 상태", label(progress.value.command.aggregate_status)], ['회차', progress.value.command.run_id], ['조회 시각', time(progress.value.as_of_utc)]]}/>
      {progress.value.plans.length ? progress.value.plans.map(plan => <section className="section-gap" key={plan.plan_id}><strong>{plan.plan_id}</strong> <Badge>{label(plan.status)}</Badge><small> · 버전 {plan.resource_version}</small>{plan.steps.map(step => <div className="section-gap" key={`${step.index}:${step.step_id}`}><Facts items={[
        ['단계', String(step.index + 1)], ['단계 ID', step.step_id || '미확인'], ['업무', label(step.tool_name)], ['구역', step.zone_id || '미제공'], ['단계 상태', label(step.status)], ['확인 사유', step.reason_code || '없음'],
      ]}/>{step.attempts.length ? step.attempts.map(attempt => <div className="section-gap" key={attempt.execution_id}><strong>실행 시도 {attempt.execution_id}</strong>{attempt.linkage_status === 'linked' && attempt.execution ? <ExecutionFacts execution={attempt.execution}/> : <Notice title="단계와 실행의 연결 미확인">{attempt.reason_code || '명시적 연결 근거가 없습니다.'}</Notice>}</div>) : <p className="form-note section-gap">기록된 실행 시도 없음 · 실행 성공을 뜻하지 않습니다.</p>}</div>)}</section>) : <Empty title="보존된 계획 없음"/>}
      <Preservation limits={progress.value.preservation_limits}/>
    </>}
  </Card>;
}
const kinds: Record<string, string> = {
  'incident.latest_state': '사건의 저장된 최신 상태', 'execution.latest_state': '실행의 저장된 최신 상태',
  'notification.latest_state': '알림의 저장된 최신 상태', 'delivery_attempt.latest_state': '전달 시도의 저장된 최신 상태',
  'notification.receipt': '화면 수신 기록', 'notification.response': '응답 기록', 'followup.latest_state': '후속 확인의 저장된 최신 상태', audit: '감사 기록',
};
export function IncidentTimelineView({ id }: { id: string }) {
  return <><p className="form-note section-gap">응답·화면 수신은 차량 이동이나 사건 해결을 뜻하지 않습니다. 기록 시각은 저장 시각이며, 최신 상태만 남은 행에서 해결 시각을 추정하지 않습니다.</p><HistoryPanel<TimelineRecord> kind="timeline" id={id} title="사건 기록" render={item => <div className="list-item" key={item.event_id}><div className="list-text"><strong>{kinds[item.kind] || item.kind}</strong><small>{item.record_id} · 기록 {time(item.recorded_at_utc)}</small>{item.parent_record_id && <small>부모 기록 {item.parent_record_id}</small>}<Facts items={timelineFacts(item)}/></div></div>}/></>;
}
function timelineFacts(item: TimelineRecord): string[][] {
  const d = item.details; const values: string[][] = [];
  for (const [title, value] of [['상태', d.status], ['실행 경로', d.mode], ['업무', d.tool_name], ['목적', d.purpose],
    ['전달', d.delivery_status], ['응답', d.response], ['결과', d.outcome], ['동작', d.action], ['오류', d.error_code], ['사유', d.reason_code], ['시계', d.clock]]) {
    if (value != null) values.push([title!, label(value)]);
  }
  if (d.attempt_number != null) values.push(['전달 시도 번호', String(d.attempt_number)]);
  if (item.sim_time_ms != null) values.push(['합성 시각', `${item.sim_time_ms} ms`]);
  if (d.due_sim_time_ms != null) values.push(['합성 확인 예정', `${d.due_sim_time_ms} ms`]);
  if (d.due_at != null) values.push(['실시간 확인 예정', time(d.due_at)]);
  if (d.received_at != null) values.push(['화면 보고 수신 시각', time(d.received_at)]);
  return values;
}
