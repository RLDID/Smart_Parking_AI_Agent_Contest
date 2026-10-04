import { useEffect, useRef, useState } from 'react';
import { Button, Dialog, Field, Notice, Facts, RouteLink } from './components';
import { useApp } from './context';
import type { ModalSpec } from './context';
import { accounts, devices, initialState } from './fixtures';
import { checkMapping, requiredError } from './model';
import type { Role } from './model';

function focusError(form: HTMLFormElement) { requestAnimationFrame(() => form.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()); }
export function LoginForm() {
  const { state, setState, draft, editDraft, go } = useApp();
  const values = draft('login', { role: 'owner', account: accounts.owner.id });
  const [password, setPassword] = useState(''); const [errors, setErrors] = useState<Record<string, string>>({});
  const role = values.role as Role;
  const enter = () => { setPassword(''); setState({ ...initialState(), session: { role, account: values.account } }); go(role === 'owner' ? '/owner/monitor' : '/driver/vehicle'); };
  return <form noValidate onSubmit={e => { e.preventDefault(); const next = { account: requiredError(values.account, '계정'), password: requiredError(password, '비밀번호') }; if (!next.account && values.account !== accounts[role].id) next.account = '선택한 유형의 가상 계정을 확인해 주세요.'; if (!next.password && password !== accounts[role].password) next.password = '가상 계정의 비밀번호가 맞지 않습니다.'; setErrors(next); if (Object.values(next).some(Boolean)) focusError(e.currentTarget); else enter(); }}>
    <Field label="사용자 유형" as="select" input={{ value: role, onChange: e => { const role = e.target.value as Role; editDraft('login', 'role', role, values); editDraft('login', 'account', accounts[role].id, values); setErrors({}); } }}><option value="owner">소유자</option><option value="driver">차주</option></Field>
    <Field label="계정" error={errors.account} input={{ required: true, value: values.account, autoComplete: 'off', onChange: e => { editDraft('login', 'account', e.target.value, values); setErrors(x => ({ ...x, account: '' })); } }}/>
    <Field label="비밀번호" error={errors.password} input={{ required: true, type: 'password', value: password, autoComplete: 'off', onChange: e => { setPassword(e.target.value); setErrors(x => ({ ...x, password: '' })); } }}/>
    <p className="form-note">가상 계정: {accounts[role].id} / parking-demo</p>
    <Button type="submit" variant="primary full" disabled={!values.account?.trim() || !password.trim()}>로그인</Button>
    <Button variant="full" onClick={() => { editDraft('login', 'account', accounts[role].id, values); setPassword(accounts[role].password); setErrors({}); }}>가상 계정 채우기</Button>
    {state.mode === 'error' && <Notice title="로그인 정보를 다시 확인해 주세요."/>}
  </form>;
}

export function ModalContent({ modal }: { modal: ModalSpec }) {
  const { state, close } = useApp();
  if (modal.type === 'device') { const device = devices[modal.id as keyof typeof devices]; return <Dialog title={device.title} onClose={close} drawer><Facts items={device.facts}/>{modal.id === 'sound' && <p className="pending-features">브라우저 소리·현장 방송 연결 전</p>}</Dialog>; }
  if (modal.type === 'inbox') return <Dialog title="내게 온 알림" onClose={close} drawer><Notice title="차주 연결 확인 필요">대상 미확인 · 연락 보류</Notice><RouteLink to="/owner/incidents/exit" className="btn full">관련 사건 보기</RouteLink></Dialog>;
  if (modal.type === 'guide') return <GuideForm/>;
  return <EditForm key={`${modal.type}-${modal.kind}-${modal.id}`} modal={modal} stateVersion={state.session?.account || ''}/>;
}
function GuideForm() {
  const { draft, editDraft, close } = useApp(); const values = draft('guide', { question: '' });
  return <Dialog title="주차장 이용안내" onClose={close}><Field label="질문 내용" as="textarea" input={{ value: values.question, maxLength: 500, placeholder: '예: 출차가 막혔을 때 어떻게 하나요?', onChange: e => editDraft('guide', 'question', e.target.value, values) }}/><p className="pending-features">규정 답변·근거: 연결 준비 중</p></Dialog>;
}
function EditForm({ modal, stateVersion }: { modal: ModalSpec; stateVersion: string }) {
  const { state, setState, draft, editDraft, clearDraft, toast, close, blocked } = useApp();
  const formRef = useRef<HTMLFormElement>(null);
  const kind = modal.kind || 'vehicles';
  const item = modal.type === 'mapping' ? state.mappings.find(m => m.object === modal.id) : state[kind].find(r => r.id === modal.id);
  const record = modal.type !== 'mapping' ? state[kind].find(r => r.id === modal.id) : undefined;
  const mapping = modal.type === 'mapping' ? state.mappings.find(m => m.object === modal.id) : undefined;
  const defaults: Record<string, string> = modal.type === 'report' ? { place: state.vehicle === 'car-b' ? '서측 통로' : '', text: '' } : modal.type === 'mapping' ? { target: mapping?.target || '', status: mapping?.status || '미연결', reason: mapping?.reason || '' } : modal.type === 'link' ? { customer: record?.customer || '', reason: '' } : { name: record?.name || '', active: record?.active || '활성', reason: '' };
  const key = `${modal.type}:${kind}:${modal.id || (modal.type === 'report' ? state.vehicle : 'new')}`;
  const values = draft(key, defaults);
  const [expected, setExpected] = useState(modal.version || item?.version || 0);
  const [errors, setErrors] = useState<Record<string, string>>({}); const [conflict, setConflict] = useState(false); const [failed, setFailed] = useState(false); const [injected, setInjected] = useState(false);
  useEffect(() => { if (conflict) requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('[data-conflict-review]')?.focus()); }, [conflict]);
  const update = (field: string, value: string) => { editDraft(key, field, value, defaults); setErrors(e => ({ ...e, [field]: '' })); };
  const title = modal.type === 'report' ? '출차 방해 신고' : modal.type === 'mapping' ? '관측 연결 검토' : modal.type === 'link' ? '차주 연결 변경' : `${kind === 'customers' ? '고객' : '차량'} ${record ? '수정' : '등록'}`;
  const validRequired = modal.type === 'report' ? ['place', 'text'] : modal.type === 'record' ? ['name', 'reason'] : ['reason'];
  const labels: Record<string, string> = { place: '위치', text: '상황 설명', name: '별칭', reason: modal.type === 'mapping' ? '검토 사유' : '변경 사유' };
  const field = (name: string, label: string, textarea = false) => textarea ? <Field label={label} error={errors[name]} as="textarea" input={{ name, required: true, value: values[name], maxLength: 500, onChange: e => update(name, e.target.value) }}/> : <Field label={label} error={errors[name]} input={{ name, required: true, value: values[name], maxLength: name === 'name' ? 40 : 120, onChange: e => update(name, e.target.value) }}/>;
  function submit() {
    if (blocked || !state.session || state.session.account !== stateVersion) return;
    const next: Record<string, string> = Object.fromEntries(validRequired.map(n => [n, requiredError(values[n] || '', labels[n])]));
    if (modal.type === 'mapping' && mapping) { const error = checkMapping({ ...mapping, target: values.target, status: values.status, reason: values.reason }, state.mappings, expected); if (error) next[error.includes('사유') ? 'reason' : 'target'] = error; }
    setErrors(next);
    if (Object.values(next).some(Boolean)) { if (item && item.version !== expected) setConflict(true); focusError(formRef.current!); return; }
    if (item && state.saveScenario === 'conflict' && !injected) {
      setInjected(true); setConflict(true);
      setState(s => modal.type === 'mapping' ? { ...s, mappings: s.mappings.map(m => m.object === modal.id ? { ...m, version: m.version + 1 } : m) } : { ...s, [kind]: s[kind].map(r => r.id === modal.id ? { ...r, version: r.version + 1 } : r) }); return;
    }
    if (item && item.version !== expected) { setConflict(true); return; }
    if (state.saveScenario === 'failure') { setFailed(true); return; }
    setState(s => {
      if (modal.type === 'report') return { ...s, reports: [...s.reports, { vehicle: s.vehicle, place: values.place.trim(), text: values.text.trim() }] };
      if (modal.type === 'mapping') return { ...s, mappings: s.mappings.map(m => m.object === modal.id ? { ...m, target: values.status === '미연결' ? '' : values.target, status: values.status, reason: values.reason.trim(), version: m.version + 1 } : m) };
      if (modal.type === 'link') return { ...s, vehicles: s.vehicles.map(v => v.id === modal.id ? { ...v, customer: values.customer, reason: values.reason.trim(), version: v.version + 1 } : v) };
      const saved = { id: record?.id || `${kind}-${crypto.randomUUID()}`, name: values.name.trim(), active: values.active, reason: values.reason.trim(), version: (record?.version || 0) + 1, customer: record?.customer || '' };
      return { ...s, [kind]: record ? s[kind].map(r => r.id === record.id ? saved : r) : [...s[kind], saved] };
    });
    clearDraft(key); close(); toast(modal.type === 'report' ? '신고 접수 · 검토 대기' : '저장했습니다.');
  }
  return <Dialog title={title} onClose={close}><form ref={formRef} noValidate onSubmit={e => { e.preventDefault(); submit(); }}>
    {modal.type === 'report' ? <><Field label="신고할 내 차량" input={{ value: state.vehicle === 'car-b' ? '내 차량 B' : '내 차량 예시 C', readOnly: true }}/>{field('place', '위치')}{field('text', '상황 설명', true)}</> : modal.type === 'record' ? <>{field('name', `${kind === 'customers' ? '고객' : '차량'} 별칭`)}<Field label="활성 상태" as="select" input={{ value: values.active, onChange: e => update('active', e.target.value) }}><option>활성</option><option>비활성</option></Field>{field('reason', '변경 사유')}</> : modal.type === 'link' ? <><p className="form-note">{record?.name}</p><Field label="연결할 고객" as="select" input={{ value: values.customer, onChange: e => update('customer', e.target.value) }}><option value="">연결 해제</option>{state.customers.filter(c => c.active === '활성').map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</Field>{field('reason', '변경 사유')}</> : <><p className="form-note">{mapping?.object} · 버전 {mapping?.version}</p><Field label={`연결할 ${mapping?.kind === 'vehicles' ? '등록 차량' : '고객'}`} error={errors.target} as="select" input={{ value: values.target, disabled: values.status === '미연결', onChange: e => update('target', e.target.value) }}><option value="">연결 없음</option>{state[mapping?.kind || 'vehicles'].filter(r => r.active === '활성').map(r => <option key={r.id} value={r.id}>{r.name}</option>)}</Field><Field label="검토 상태" as="select" input={{ value: values.status, onChange: e => { update('status', e.target.value); if (e.target.value === '미연결') update('target', ''); } }}>{['검토된 연결', '불확실', '미연결'].map(s => <option key={s}>{s}</option>)}</Field>{field('reason', '검토 사유')}</>}
    {conflict && <Notice title="다른 변경이 있습니다. 최신 내용을 확인해 주세요."><span>입력은 유지됩니다. 현재 버전: {item?.version}</span><Button data-conflict-review onClick={() => { setExpected(item?.version || 0); setConflict(false); setErrors({}); requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('button[type=submit]')?.focus()); }}>최신 내용 확인</Button></Notice>}
    {failed && <Notice title="저장하지 못했습니다. 입력은 유지됩니다."><Button onClick={() => { setState(s => ({ ...s, saveScenario: 'normal' })); setFailed(false); }}>다시 저장 준비</Button></Notice>}
    <div className="dialog-actions"><Button onClick={close}>닫기</Button><Button type="submit" variant="primary" disabled={blocked || conflict || validRequired.some(n => !values[n]?.trim())}>{modal.type === 'report' ? '신고 접수' : modal.type === 'link' ? '연결 변경' : '저장'}</Button></div>
  </form></Dialog>;
}
