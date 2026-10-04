import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Badge, Button, Card, Empty, Facts, Field, Head, Loading, Notice, RouteLink } from '../components';
import { ApiError, number, row, rows, text } from './api';
import type { Row } from './api';
import { useLive, useResource } from './context';
import { CommandProgressView, CommandsHistory, ExecutionsHistory } from './ContestViews';
type Method = 'POST' | 'PATCH' | 'PUT' | 'DELETE';
type PendingRequest = { path: string; body: Row; method: Method; success: (result: Row) => void | Promise<void> };
const encode = encodeURIComponent;
const messageOf = (error: unknown) => error instanceof Error ? error.message : '요청 결과를 확인하지 못했습니다.';
const commandLabels: Record<string, string> = { pending: '접수 · 처리 대기', running: '처리 중', succeeded: '완료', failed: '실패', partial: '일부 완료', held: '보류', cancelled: '취소', unknown: '결과 미확인' };
const planLabels: Record<string, string> = { not_ready: '계획 준비 대기', proposed: '계획 확인 대기', active: '확인된 계획', completed: '계획 완료', held: '계획 보류', cancelled: '계획 취소' };
const mappingLabels: Record<string, string> = { proposed: '연결 제안', uncertain: '불확실', verified: '검토된 연결', unmapped: '미연결·해제' };
const label = (value: unknown, labels: Record<string, string>) => labels[text(value, '')] || text(value);
const statusTone = (value: unknown) => value === 'succeeded' || value === 'completed' ? 'green' : value === 'failed' ? 'red' : ['held', 'partial', 'unknown'].includes(text(value, '')) ? 'amber' : '';
function parsedGoal(value: unknown): Row {
  if (typeof value !== 'string') return row(value);
  try { return row(JSON.parse(value)); } catch { return {}; }
}

// A failed transport keeps the original action/body for an explicit same-attempt retry.
function useMutation(scope: string) {
  const { api, epoch } = useLive();
  const actionId = useId(); const sequence = useRef(0); const lock = useRef(false);
  const current = useRef({ epoch, scope }); current.current = { epoch, scope };
  const mounted = useRef(true); const request = useRef<PendingRequest | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false); const [pending, setPending] = useState(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { lock.current = false; request.current = null; sequence.current += 1; setBusy(false); setError(''); setConflict(false); setPending(false); }, [epoch, scope]);
  const valid = (origin: { epoch: number; scope: string }) => mounted.current && current.current.epoch === origin.epoch && current.current.scope === origin.scope;
  async function perform(operation: PendingRequest) {
    if (lock.current) return;
    lock.current = true; const origin = { epoch, scope }; setBusy(true); setError(''); setConflict(false);
    try {
      const result = await api.mutate(`${actionId}:${sequence.current}:${scope}`, operation.path, operation.body, operation.method);
      if (!valid(origin)) return;
      request.current = null; setPending(false); sequence.current += 1;
      try { await operation.success(result); } catch (error) { if (valid(origin)) setError(`요청은 접수됐지만 최신 조회를 완료하지 못했습니다. ${messageOf(error)}`); }
    } catch (error) {
      if (!valid(origin)) return;
      setError(messageOf(error));
      const unknown = !(error instanceof ApiError) || error.ambiguous;
      request.current = unknown ? operation : null; setPending(unknown);
      setConflict(error instanceof ApiError && error.status === 409 && !unknown);
      if (!unknown) sequence.current += 1;
    } finally { if (valid(origin)) { lock.current = false; setBusy(false); } }
  }
  return {
    busy, error, conflict, pending,
    run: (path: string, body: Row, success: PendingRequest['success'], method: Method = 'POST') => {
      if (!request.current) void perform({ path, body, method, success });
    },
    retry: () => { if (request.current) void perform(request.current); },
    clear: () => { if (!request.current) { setError(''); setConflict(false); } },
  };
}
function MutationStatus({ mutation, blocked }: { mutation: ReturnType<typeof useMutation>; blocked: boolean }) {
  return <>{mutation.busy && <Notice title="요청 처리 중"/>}{mutation.error && <Notice title={mutation.error}>{mutation.pending && <><span>입력은 유지됩니다. 같은 요청의 결과를 확인해 주세요.</span><Button disabled={blocked || mutation.busy} onClick={mutation.retry}>같은 요청 다시 확인</Button></>}</Notice>}</>;
}

function KnownCommand({ id }: { id: string }) {
  const resource = useResource(`/api/v1/commands/${encode(id)}`);
  return <div className="list-item"><div className="list-text"><RouteLink to={`/owner/commands/${id}`} className="btn-link">{resource.value ? text(resource.value.request_text, id) : id}</RouteLink>{resource.loading ? <small>조회 중</small> : resource.error ? <small>{resource.error}</small> : resource.value && <><small>{text(resource.value.created_at)}</small><Badge tone={statusTone(resource.value.aggregate_status)}>{label(resource.value.aggregate_status, commandLabels)}</Badge></>}</div></div>;
}
export function LiveCommands() {
  const { data, facilityId, epoch, blocked, rememberCommand, go, toast, refresh } = useLive();
  const [copy, setCopy] = useState(''); const [purpose, setPurpose] = useState('operational_goal'); const [unconfirmed, setUnconfirmed] = useState(false);
  const mutation = useMutation('new-command'); const { knownCommands } = useLive();
  const snapshot = row(data.view?.snapshot); const runId = text(snapshot.run_id, ''); const stateVersion = number(snapshot.state_version);
  useEffect(() => { setCopy(''); setPurpose('operational_goal'); setUnconfirmed(false); }, [epoch]);
  const disabled = blocked || !runId || stateVersion === null || unconfirmed || mutation.busy || mutation.pending;
  return <><Head title="AI 운영 요청"/><div className="grid equal"><Card title="새 요청"><form onSubmit={event => {
    event.preventDefault(); if (disabled || !copy.trim()) return;
    mutation.run(`/api/v1/facilities/${encode(facilityId)}/commands`, { run_id: runId, purpose, text: copy.trim(), based_on_state_version: stateVersion }, async result => {
      const id = text(result.command_id, ''); if (!id) { setUnconfirmed(true); throw new Error('반환된 요청 ID가 없습니다. 새 요청을 다시 제출하지 말고 접수 결과를 확인하세요.'); }
      rememberCommand(id); toast('요청 접수 · 처리 대기'); go(`/owner/commands/${id}`); await refresh();
    });
  }}><Field label="요청 유형" as="select" input={{ value: purpose, disabled, onChange: event => setPurpose(event.target.value) }}><option value="operational_goal">운영 목표</option><option value="query">일반 질문 · 접수</option></Field><Field label="요청 내용" as="textarea" input={{ required: true, value: copy, maxLength: 2000, disabled, onChange: event => setCopy(event.target.value), placeholder: '수행할 운영 목표와 대상 범위를 입력하세요.' }}/>{!runId && <Notice title="현재 실행이 없습니다.">관측 준비 후 요청할 수 있습니다.</Notice>}{purpose === 'query' && <p className="pending-features">일반 질문은 접수 기록으로 남습니다. 규정·현재 상태 답변은 하단의 주차장 이용안내에서 조회할 수 있습니다.</p>}<MutationStatus mutation={mutation} blocked={blocked}/><Button type="submit" variant="primary full" disabled={disabled || !copy.trim()}>요청 접수</Button></form></Card><div className="stack"><Card title="이번 세션의 요청" body={false}>{knownCommands.length ? knownCommands.map(id => <KnownCommand key={id} id={id}/>) : <Empty title="이번 세션에 접수된 요청 없음"/>}</Card><CommandsHistory/><ExecutionsHistory/></div></div></>;
}

export function LiveCommandDetail({ id }: { id: string }) {
  const { api, data, epoch, blocked, refresh, toast } = useLive();
  const command = useResource(`/api/v1/commands/${encode(id)}`); const plan = useResource(`/api/v1/commands/${encode(id)}/plan`);
  const [goal, setGoal] = useState('closing'); const [reason, setReason] = useState(''); const [reviewing, setReviewing] = useState(false); const [processResult, setProcessResult] = useState<Row | null>(null);
  const mutation = useMutation(`command:${id}`);
  useEffect(() => { setGoal('closing'); setReason(''); setReviewing(false); setProcessResult(null); }, [epoch, id]);
  const c = command.value; const p = plan.value; const version = number(c?.resource_version);
  const runId = text(c?.run_id, ''); const activeRun = text(row(data.view?.snapshot).run_id, '');
  const paused = data.view?.run_status === 'paused'; const testEnabled = data.readiness?.test_control_enabled === true;
  const cancelled = Boolean(c?.cancellation_requested_at) || c?.aggregate_status === 'cancelled';
  const disabled = blocked || command.loading || mutation.busy || mutation.pending || mutation.conflict || reviewing || !runId || runId !== activeRun || version === null || version < 1 || cancelled || c?.aggregate_status === 'succeeded' || c?.aggregate_status === 'failed';
  const isOperation = c?.purpose === 'operational_goal'; const normalized = parsedGoal(c?.normalized_goal_json);
  const reload = async () => { setReviewing(true); try { await Promise.all([api.get(`/api/v1/commands/${encode(id)}`), api.get(`/api/v1/commands/${encode(id)}/plan`)]); command.reload(); plan.reload(); await refresh(); mutation.clear(); } finally { setReviewing(false); } };
  const changed = async (title: string) => { toast(title); command.reload(); plan.reload(); await refresh(); };
  return <><RouteLink to="/owner/commands" className="back">요청 목록으로</RouteLink><Head title="운영 요청" action={c && <Badge tone={statusTone(c.aggregate_status)}>{label(c.aggregate_status, commandLabels)}</Badge>}/>{command.loading && !c ? <Loading/> : command.error ? <Notice title={command.error}><Button onClick={command.reload}>다시 조회</Button></Notice> : !c ? <Empty title="요청을 확인할 수 없습니다."/> : <>
    <div className="detail-grid"><div className="stack"><Card title="요청 내용"><p className="request-copy">{text(c.request_text)}</p><div className="section-gap"><Facts items={[["요청 ID", id], ['접수 시각', text(c.created_at)], ['현재 버전', text(c.resource_version)], ['운영 목표', text(normalized.kind, '구체화 전')]]}/></div>{Boolean(c.cancellation_requested_at) && <Notice title="취소 요청이 기록되었습니다." neutral>이미 실행된 동작은 유지됩니다. 진행·결과 미확인은 서버 상태를 확인해 주세요.</Notice>}</Card>
      <Card title="실행 계획" action={p && <Badge>{label(p.status, planLabels)}</Badge>}>{plan.loading && !p ? <Loading/> : plan.error ? <Notice title={plan.error}><Button onClick={plan.reload}>계획 다시 조회</Button></Notice> : !p || p.status === 'not_ready' ? <Empty title="계획 준비 대기">업무 Agent의 계획 생성 후 다시 조회해 주세요.</Empty> : <><Facts items={[["계획 ID", text(p.plan_id)], ['계획 버전', text(p.plan_version)]]}/>{rows(p.steps).map((step, index) => <div className="plan-step" key={`${index}:${text(step.tool)}`}><span className="step-number">{index + 1}</span><div className="list-text"><strong>{step.tool === 'play_announcement' ? '안내 방송' : step.tool === 'set_entry_policy' ? '신규 입차 정책 변경' : text(step.tool)}</strong>{Boolean(step.zone_id) && <small>{text(step.zone_id)}</small>}{Boolean(step.depends_on) && <small>선행 조건: {text(step.depends_on)}</small>}</div></div>)}{isOperation && p.status === 'proposed' && <Button variant="primary" disabled={disabled || !testEnabled || plan.loading || number(p.command_version) !== version} onClick={() => mutation.run(`/api/v1/commands/${encode(id)}/confirm`, { expected_resource_version: version, reason: reason.trim() || null }, () => changed('계획 확인 접수 · 서버 처리 상태 확인'))}>계획 확인하고 진행</Button>}</>}

      </Card></div><div className="stack"><Card title="요청 관리">{isOperation && <form onSubmit={event => { event.preventDefault(); if (disabled || !testEnabled) return; mutation.run(`/api/v1/commands/${encode(id)}/clarify`, { expected_resource_version: version, goal, zone_id: goal === 'zone_notice' ? 'announcement-a' : null }, () => changed('운영 목표 구체화 접수 · 새 계획 확인 필요')); }}><Field label="운영 목표" as="select" input={{ value: goal, disabled, onChange: event => setGoal(event.target.value) }}><option value="closing">영업 종료 · 두 방송 구역</option><option value="zone_notice">구역 안내 · announcement-a</option></Field><Button type="submit" disabled={disabled || !testEnabled}>대상·목표 구체화</Button></form>}
        <Field label="확인·취소 사유 (선택)" as="textarea" input={{ value: reason, maxLength: 500, disabled, onChange: event => setReason(event.target.value) }}/><div className="dialog-actions"><Button disabled={disabled} onClick={() => mutation.run(`/api/v1/commands/${encode(id)}/cancel`, { expected_resource_version: version, reason: reason.trim() || null }, () => changed('취소 요청 접수 · 기존 실행 결과 유지'))}>미실행 작업 취소 요청</Button></div>{isOperation && <><Button variant="full" disabled={disabled || !testEnabled || !paused} onClick={() => mutation.run('/api/v1/test/agent/operations', { run_id: runId, action: 'process', mode: 'mock', scenario: 's3', command_id: id }, result => { setProcessResult(result); return changed('모의 업무 응답 수신 · 현재 계획·결과 다시 조회'); })}>{p?.status === 'active' ? '모의 업무 한 단계 처리' : '모의 계획 준비'}</Button><p className="form-note">시험 제어가 켜진 일시정지 실행에서만 사용할 수 있습니다. 확인된 계획은 합성 장치의 다음 단계를 처리합니다.</p>{processResult && <Facts items={[["모의 처리 응답", text(processResult.status)], ['확인 사유', text(processResult.reason_code)]]}/>}</>}
        <MutationStatus mutation={mutation} blocked={blocked}/>{mutation.conflict && <Notice title="버전·계획·정책을 다시 확인해 주세요."><Button disabled={blocked || reviewing || mutation.busy} onClick={() => { void reload().catch(error => toast(messageOf(error))); }}>최신 요청·계획 다시 조회</Button></Notice>}{!isOperation && <p className="pending-features">질문·신고 접수와 사건 처리 완료를 구분합니다. 규정·현재 상태 답변은 주차장 이용안내에서 조회할 수 있습니다.</p>}{runId !== activeRun && <Notice title="이 요청의 실행 회차가 현재 실행과 다릅니다.">이전 요청은 조회만 표시합니다.</Notice>}</Card><RouteLink to="/owner/monitor">관제 화면</RouteLink></div></div>
  </>}<CommandProgressView id={id}/></>;
}

type EditTarget = { kind: 'customer' | 'vehicle' | 'link' | 'vehicle_mapping' | 'person_mapping'; id: string; item: Row; expected: number };
const activeCustomer = (item: Row) => item.disabled_at == null;
const activeVehicle = (item: Row) => item.active === true || item.active === 1;
function relationVersion(snapshot: Row, scope: string, subject: string): number {
  return number(rows(snapshot.versions).find(item => item.scope === scope && item.subject === subject)?.resource_version) ?? 0;
}
function versionFor(snapshot: Row, target: EditTarget, runId: string): number {
  const scope = target.kind === 'customer' ? 'customer' : target.kind === 'vehicle' ? 'vehicle' : target.kind === 'link' ? 'vehicle_user' : target.kind === 'vehicle_mapping' ? 'object_mapping' : 'person_mapping';
  return relationVersion(snapshot, scope, target.kind.endsWith('mapping') ? `${runId}:${target.id}` : target.id);
}
function currentMappings(snapshot: Row, key: string, runId: string, simTime: number | null): Row[] {
  return rows(snapshot[key]).filter(item => { const start = number(item.valid_from_sim_time_ms); return item.run_id === runId && item.valid_to_sim_time_ms == null && start !== null && simTime !== null && start <= simTime; });
}
function currentLinks(snapshot: Row): Row[] { return rows(snapshot.vehicle_users).filter(item => item.valid_until == null && (typeof item.valid_from !== 'string' || Date.parse(item.valid_from) <= Date.now())); }
function RelationCell({ label: cellLabel, children }: { label: string; children: ReactNode }) { return <td role="cell" data-label={cellLabel}>{children}</td>; }

export function LiveRelationships() {
  const { facilityId, data, blocked, epoch } = useLive(); const base = `/api/v1/facilities/${encode(facilityId)}/relationships`;
  const resource = useResource(base); const retained = useRef<{ epoch: number; facility: string; value: Row | null }>({ epoch, facility: facilityId, value: null });
  if (retained.current.epoch !== epoch || retained.current.facility !== facilityId) retained.current = { epoch, facility: facilityId, value: null };
  if (resource.value) retained.current.value = resource.value;
  const snapshot = resource.value || retained.current.value;
  const [tab, setTab] = useState('customers'); const [edit, setEdit] = useState<EditTarget | null>(null);
  const observation = row(data.view?.snapshot); const runId = text(observation.run_id, ''); const simTime = number(observation.sim_time_ms);
  useEffect(() => { setEdit(null); }, [epoch, runId]);
  const customers = rows(snapshot?.customers); const vehicles = rows(snapshot?.vehicles); const links = currentLinks(snapshot || {});
  const vehicleMappings = currentMappings(snapshot || {}, 'vehicle_mappings', runId, simTime); const personMappings = currentMappings(snapshot || {}, 'person_mappings', runId, simTime);
  const objects = rows(observation.objects).filter(item => item.object_type === 'vehicle' || item.object_type === 'pedestrian');
  const mappingItems = [...vehicleMappings.map(item => ({ kind: 'vehicle_mapping' as const, item })), ...personMappings.map(item => ({ kind: 'person_mapping' as const, item }))];
  for (const object of objects) {
    const kind = object.object_type === 'vehicle' ? 'vehicle_mapping' as const : 'person_mapping' as const;
    if (!mappingItems.some(entry => entry.kind === kind && entry.item.object_id === object.object_id)) mappingItems.push({ kind, item: { object_id: object.object_id, run_id: runId, mapping_status: 'unmapped' } });
  }
  function open(kind: EditTarget['kind'], id: string, item: Row) { const target = { kind, id, item, expected: 0 }; setEdit({ ...target, expected: snapshot ? versionFor(snapshot, target, runId) : 0 }); }
  const alias = (items: Row[], key: string, id: unknown) => items.find(item => item[key] === id)?.display_alias;
  return <><Head title="등록 정보" action={tab !== 'links' && <Button variant="primary" disabled={blocked || !snapshot || resource.loading} onClick={() => open(tab === 'customers' ? 'customer' : 'vehicle', '', {})}>{tab === 'customers' ? '고객 등록' : '차량 등록'}</Button>}/><div className="filters" role="group" aria-label="등록 정보 종류">{[['customers', '고객'], ['vehicles', '차량'], ['links', '연결 정보']].map(([value, title]) => <button type="button" className="filter" key={value} aria-pressed={tab === value} onClick={() => setTab(value)}>{title}</button>)}</div>
    {resource.error && <Notice title={resource.error}><Button onClick={resource.reload}>다시 조회</Button></Notice>}{resource.loading && !snapshot ? <Loading/> : !snapshot ? <Empty title="등록 정보를 확인할 수 없습니다."/> : <><Card title={tab === 'links' ? '현재 차량·차주 연결' : tab === 'customers' ? '고객' : '차량'} body={false}><div className="table-wrap"><table className="relationship-table" role="table"><thead role="rowgroup"><tr role="row"><th scope="col">{tab === 'customers' ? '고객' : '차량'}</th><th scope="col">{tab === 'links' ? '현재 차주' : '활성 상태'}</th><th scope="col">관리</th></tr></thead><tbody role="rowgroup">{(tab === 'customers' ? customers : vehicles).map(item => {
      const id = text(tab === 'customers' ? item.user_id : item.registered_vehicle_id, ''); const link = links.find(entry => entry.registered_vehicle_id === id);
      const active = tab === 'customers' ? activeCustomer(item) : activeVehicle(item);
      return <tr role="row" key={id}><RelationCell label={tab === 'customers' ? '고객' : '차량'}><strong>{text(item.display_alias)}</strong><small>{id}</small></RelationCell><RelationCell label={tab === 'links' ? '현재 차주' : '활성 상태'}>{tab === 'links' ? text(alias(customers, 'user_id', link?.user_id), '연결 없음') : <Badge>{active ? '활성' : '비활성'}</Badge>}</RelationCell><RelationCell label="관리"><Button disabled={blocked || resource.loading || (tab === 'links' && !active)} onClick={() => open(tab === 'links' ? 'link' : tab === 'customers' ? 'customer' : 'vehicle', id, item)}>{tab === 'links' ? '차주 연결 변경' : '수정'}</Button></RelationCell></tr>;
    })}</tbody></table></div>{!(tab === 'customers' ? customers : vehicles).length && <Empty title="등록된 항목 없음"/>}</Card>
    {tab === 'links' && <Card title="현재 실행의 관측 연결" body={false}>{!runId ? <Empty title="현재 실행 없음"/> : !mappingItems.length ? <Empty title="현재 관측 연결 없음"/> : <div className="table-wrap"><table className="relationship-table" role="table"><thead><tr><th scope="col">관측 대상</th><th scope="col">연결 대상</th><th scope="col">상태</th><th scope="col">관리</th></tr></thead><tbody>{mappingItems.map(({ kind, item }) => <tr key={`${kind}:${text(item.object_id)}`}><RelationCell label="관측 대상"><strong>{text(item.object_id)}</strong><small>{kind === 'vehicle_mapping' ? '차량 객체' : '보행자 객체'}</small></RelationCell><RelationCell label="연결 대상">{kind === 'vehicle_mapping' ? text(alias(vehicles, 'registered_vehicle_id', item.registered_vehicle_id), '연결 없음') : text(alias(customers, 'user_id', item.user_id), '연결 없음')}</RelationCell><RelationCell label="상태"><Badge tone={item.mapping_status === 'uncertain' ? 'amber' : ''}>{text(item.mapping_status)}</Badge></RelationCell><RelationCell label="관리"><Button disabled={blocked || resource.loading} onClick={() => open(kind, text(item.object_id, ''), item)}>연결 검토</Button></RelationCell></tr>)}</tbody></table></div>}</Card>}
    {edit && <RelationshipEditor key={`${edit.kind}:${edit.id}:${runId}:${epoch}`} target={edit} base={base} snapshot={snapshot} runId={runId} simTime={simTime} onClose={() => setEdit(null)} onSaved={resource.reload}/>}
  </>}</>;
}

function RelationshipEditor({ target, base, snapshot, runId, simTime, onClose, onSaved }: { target: EditTarget; base: string; snapshot: Row; runId: string; simTime: number | null; onClose: () => void; onSaved: () => void }) {
  const { api, blocked, epoch, refresh, toast } = useLive();
  const mutation = useMutation(`relationship:${target.kind}:${target.id}:${runId}`); const [expected, setExpected] = useState(target.expected);
  const [latestSnapshot, setLatestSnapshot] = useState<Row | null>(null); const [reviewing, setReviewing] = useState(false); const [reviewError, setReviewError] = useState('');
  const effectiveSnapshot = latestSnapshot && versionFor(latestSnapshot, target, runId) >= versionFor(snapshot, target, runId) ? latestSnapshot : snapshot;
  const currentLink = currentLinks(effectiveSnapshot).find(item => item.registered_vehicle_id === target.id);
  const isMapping = target.kind === 'vehicle_mapping' || target.kind === 'person_mapping'; const isRecord = target.kind === 'customer' || target.kind === 'vehicle';
  const [alias, setAlias] = useState(text(target.item.display_alias, '')); const [active, setActive] = useState(target.kind === 'customer' ? activeCustomer(target.item) : activeVehicle(target.item));
  const [chosen, setChosen] = useState(text(target.kind === 'link' ? currentLink?.user_id : target.kind === 'vehicle_mapping' ? target.item.registered_vehicle_id : target.item.user_id, ''));
  const [status, setStatus] = useState(text(target.item.mapping_status, 'unmapped')); const [reason, setReason] = useState(''); const [validation, setValidation] = useState('');
  const formRef = useRef<HTMLFormElement>(null); const currentEpoch = useRef(epoch); currentEpoch.current = epoch; const alive = useRef(true);
  useEffect(() => { alive.current = true; requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('input,select,textarea')?.focus()); return () => { alive.current = false; }; }, []);
  const versionChanged = Boolean(target.id) && versionFor(effectiveSnapshot, target, runId) !== expected;
  const disabled = blocked || mutation.busy || mutation.pending || reviewing;
  const customers = rows(effectiveSnapshot.customers).filter(activeCustomer); const vehicles = rows(effectiveSnapshot.vehicles).filter(activeVehicle);
  const options = target.kind === 'vehicle_mapping' ? vehicles : customers; const optionId = target.kind === 'vehicle_mapping' ? 'registered_vehicle_id' : 'user_id';
  const serverRecord = rows(effectiveSnapshot[target.kind === 'customer' ? 'customers' : 'vehicles']).find(item => item[target.kind === 'customer' ? 'user_id' : 'registered_vehicle_id'] === target.id) || target.item;
  const originallyActive = target.kind === 'customer' ? activeCustomer(serverRecord) : activeVehicle(serverRecord);
  const title = target.kind === 'link' ? '차주 연결 변경' : isMapping ? '관측 연결 검토' : `${target.kind === 'customer' ? '고객' : '차량'} ${target.id ? '수정' : '등록'}`;
  async function review() {
    if (reviewing) return; const origin = epoch; setReviewing(true); setReviewError('');
    try { const latest = await api.get(base); if (!alive.current || currentEpoch.current !== origin) return; setLatestSnapshot(latest); setExpected(versionFor(latest, target, runId)); mutation.clear(); onSaved(); }
    catch (error) { if (alive.current && currentEpoch.current === origin) setReviewError(messageOf(error)); }
    finally { if (alive.current && currentEpoch.current === origin) setReviewing(false); }
  }
  function submit() {
    if (disabled || mutation.conflict || versionChanged) return;
    const trimmed = reason.trim();
    if (!trimmed || (isRecord && !alias.trim())) { setValidation('별칭과 변경·검토 사유를 입력해 주세요.'); return; }
    if (isMapping && status === 'verified' && !chosen) { setValidation('검토된 연결의 대상을 선택해 주세요.'); return; }
    if (isMapping && !runId) { setValidation('현재 실행이 필요합니다.'); return; }
    if (isMapping && status === 'unmapped' && !text(target.item.mapping_id, '')) { setValidation('해제할 현재 연결이 없습니다. 연결 대상과 검토 상태를 선택해 주세요.'); return; }
    if (target.kind === 'link' && chosen === text(currentLink?.user_id, '')) { setValidation('현재 연결과 다른 고객을 선택하거나 연결을 해제해 주세요.'); return; }
    if (chosen && !options.some(item => item[optionId] === chosen)) { setValidation('현재 활성 상태인 대상을 선택해 주세요.'); return; }
    if (isMapping && status !== 'unmapped' && chosen) {
      const conflicts = currentMappings(effectiveSnapshot, target.kind === 'vehicle_mapping' ? 'vehicle_mappings' : 'person_mappings', runId, simTime);
      if (conflicts.some(item => item.object_id !== target.id && item[target.kind === 'vehicle_mapping' ? 'registered_vehicle_id' : 'user_id'] === chosen)) { setValidation('대상이 다른 관측 객체와 연결돼 있습니다. 현재 연결을 검토해 주세요.'); return; }
    }
    setValidation(''); let path: string; let method: Method; let body: Row;
    if (isRecord) { path = `${base}/${target.kind === 'customer' ? 'customers' : 'vehicles'}${target.id ? `/${encode(target.id)}` : ''}`; method = target.id ? 'PATCH' : 'POST'; body = { display_alias: alias.trim(), reason: trimmed, ...(target.id ? { expected_version: expected, active } : {}) }; }
    else if (target.kind === 'link') { path = `${base}/vehicles/${encode(target.id)}/customer`; method = 'PUT'; body = { expected_version: expected, user_id: chosen || null, reason: trimmed }; }
    else if (target.kind === 'vehicle_mapping') { path = `${base}/vehicle-objects/${encode(target.id)}`; method = 'PUT'; body = { run_id: runId, expected_version: expected, registered_vehicle_id: status === 'unmapped' ? null : chosen || null, mapping_status: status, mapping_source: 'reviewed', reason: trimmed }; }
    else { path = `${base}/person-objects/${encode(target.id)}`; method = 'PUT'; body = { run_id: runId, expected_version: expected, user_id: status === 'unmapped' ? null : chosen || null, status, source: 'reviewed', reason: trimmed }; }
    mutation.run(path, body, async () => { toast('등록 정보 변경 완료'); onClose(); onSaved(); await refresh(); }, method);
  }
  return <Card title={title} action={<Button disabled={mutation.busy || mutation.pending} onClick={onClose}>닫기</Button>}><form ref={formRef} noValidate onSubmit={event => { event.preventDefault(); submit(); }}>
    {target.id && <Facts items={[[isMapping ? '관측 대상' : '항목 ID', target.id], ['검토 중인 버전', String(expected)]]}/>}
    {isRecord ? <><Field label={`${target.kind === 'customer' ? '고객' : '차량'} 별칭`} input={{ value: alias, required: true, maxLength: 64, disabled, onChange: event => { setAlias(event.target.value); setValidation(''); } }}/>{target.id && <Field label="활성 상태" as="select" input={{ value: String(active), disabled, onChange: event => setActive(event.target.value === 'true') }}><option value="true" disabled={!originallyActive}>활성</option><option value="false">비활성</option></Field>}{target.id && !originallyActive && <p className="form-note">비활성 항목 재활성화는 현재 지원하지 않습니다.</p>}</> : <><Field label={target.kind === 'vehicle_mapping' ? '등록 차량' : '연결할 고객'} as="select" error={validation} input={{ value: chosen, disabled: disabled || (isMapping && status === 'unmapped'), onChange: event => { setChosen(event.target.value); setValidation(''); } }}><option value="">연결 없음</option>{chosen && !options.some(item => item[optionId] === chosen) && <option value={chosen} disabled>현재 활성 대상 아님 · {chosen}</option>}{options.map(item => <option key={text(item[optionId])} value={text(item[optionId], '')}>{text(item.display_alias)}</option>)}</Field>{isMapping && <Field label="검토 상태" as="select" input={{ value: status, disabled, onChange: event => { setStatus(event.target.value); if (event.target.value === 'unmapped') setChosen(''); setValidation(''); } }}>{(target.kind === 'person_mapping' ? ['proposed', 'uncertain', 'verified', 'unmapped'] : ['uncertain', 'verified', 'unmapped']).map(value => <option key={value} value={value}>{mappingLabels[value]}</option>)}</Field>}</>}
    <Field label={isMapping ? '검토 사유' : '변경 사유'} as="textarea" input={{ value: reason, required: true, maxLength: 200, disabled, onChange: event => { setReason(event.target.value); setValidation(''); } }}/>{validation && <Notice title={validation}/>}
    <MutationStatus mutation={mutation} blocked={blocked}/>{(mutation.conflict || versionChanged) && <Notice title="현재 정보와 버전이 변경됐습니다. 입력은 유지됩니다."><Button disabled={disabled} onClick={() => { void review(); }}>최신 내용 불러와 검토</Button></Notice>}{reviewError && <Notice title={reviewError}/>}{latestSnapshot && <div className="section-gap"><Facts items={[["최신 서버 버전", String(versionFor(latestSnapshot, target, runId))], ['서버 조회 시각', text(latestSnapshot.updated_at)]]}/><p className="form-note">입력 내용은 유지했습니다. 최신 목록과 대상을 검토한 후 다시 저장해 주세요.</p></div>}
    <div className="dialog-actions"><Button disabled={mutation.busy || mutation.pending} onClick={onClose}>닫기</Button><Button type="submit" variant="primary" disabled={disabled || mutation.conflict || versionChanged || !reason.trim() || (isRecord && !alias.trim()) || (isMapping && status === 'verified' && !chosen)}>{target.kind === 'link' ? '연결 변경' : '저장'}</Button></div>
  </form></Card>;
}
