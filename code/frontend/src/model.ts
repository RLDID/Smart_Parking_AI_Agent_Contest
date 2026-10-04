// Frontend-only state. No server, AI, message delivery or device operation.
export type Role = 'owner' | 'driver';
export type Mode = 'normal' | 'empty' | 'loading' | 'stale' | 'offline' | 'error' | 'expired' | 'revoked';
export type Outcome = 'complete' | 'partial' | 'failed' | 'held' | 'unknown';
export type ExecutionStatus = 'requested' | 'accepted' | 'running' | 'succeeded' | 'failed' | 'held' | 'cancelled' | 'unknown';
export type Phase = 'clarify' | 'plan' | 'running' | Outcome | 'cancelled';
export type Delivery = 'queued' | 'channel_accepted' | 'client_received' | 'failed' | 'unknown';
export type Followup = 'waiting' | 'blocked' | 'insufficient' | 'recovered';
export type Reply = '확인했어요' | '이동할게요' | '이동하기 어려워요' | '문의할게요';
export interface Command { id: string; text: string; goal: 'closing' | 'notice'; target: string; phase: Phase; steps: ExecutionStatus[]; cancelRequested: boolean }
export interface RecordItem { id: string; name: string; active: string; version: number; reason: string; customer?: string }
export interface Mapping { object: string; kind: 'vehicles' | 'customers'; target: string; status: string; reason: string; version: number }
export interface Report { vehicle: string; place: string; text: string }
export interface State {
  session: { role: Role; account: string } | null; mode: Mode; filter: string;
  tab: 'customers' | 'vehicles' | 'links'; vehicle: string; selectedCar: string;
  driverLocation: 'bay' | 'aisle';
  commands: Command[]; customers: RecordItem[]; vehicles: RecordItem[]; mappings: Mapping[];
  drafts: Record<string, Record<string, string>>; responses: { move?: Reply }; reports: Report[];
  delivery: Delivery; followup: Followup; deadlinePassed: boolean; outcome: Outcome;
  saveScenario: 'normal' | 'conflict' | 'failure'; returnCase: string;
}
export const commandSteps = (goal: Command['goal']) => goal === 'closing'
  ? ['출차 가능 확인', '종료 안내 방송 · A/B 구역', '신규 입차 제한', '장치·출차 통행 재확인']
  : ['대상 구역 확인', '구역 안내 방송', '전달·재생 결과 확인'];
export const executionLabels: Record<ExecutionStatus, string> = { requested: '요청', accepted: '접수', running: '진행 중', succeeded: '완료', failed: '실패', held: '보류', cancelled: '취소', unknown: '결과 미확인' };
export const phaseLabels: Record<Phase, string> = { clarify: '대상 확인 대기', plan: '계획 확인 대기', running: '처리 중', complete: '완료', partial: '일부 완료 · 후속 보류', failed: '실패', held: '보류', unknown: '결과 미확인', cancelled: '미실행 단계 취소' };
export function newCommand(id: string, text: string, goal: Command['goal']): Command {
  return { id, text: text.trim(), goal, target: '', phase: 'clarify', steps: commandSteps(goal).map(() => 'requested'), cancelRequested: false };
}
export function startCommand(command: Command): Command {
  return { ...command, phase: 'running', steps: command.steps.map((_, i) => i === 0 ? 'running' : 'requested') };
}
export function resolveCommand(command: Command, outcome: Outcome): Command {
  if (command.phase !== 'running') return command;
  const steps = command.steps.map((_, i): ExecutionStatus => {
    if (outcome === 'complete') return 'succeeded';
    if (outcome === 'held') return 'held';
    if (outcome === 'failed') return i === 0 ? 'failed' : 'held';
    if (outcome === 'unknown') return i === 0 ? 'succeeded' : i === 1 ? 'unknown' : 'held';
    return i === 0 ? 'succeeded' : i === 1 ? 'failed' : 'held';
  });
  return { ...command, phase: outcome, steps };
}
export function cancelCommand(command: Command): Command {
  const steps = command.steps.map(s => ['requested', 'accepted', 'held'].includes(s) ? 'cancelled' as const : s);
  const pending = steps.some(s => s === 'running' || s === 'unknown');
  const retained = steps.some(s => s === 'succeeded' || s === 'failed');
  return { ...command, steps, cancelRequested: true, phase: pending ? command.phase : retained ? (steps.every(s => s === 'succeeded') ? 'complete' : 'partial') : 'cancelled' };
}
export const commandLabel = (command: Command) => command.cancelRequested && command.phase === 'partial' ? '일부 완료 · 미실행 취소' : phaseLabels[command.phase];
export function notificationSummary(state: Pick<State, 'delivery' | 'responses' | 'followup' | 'deadlinePassed'>): string {
  if (state.followup === 'recovered') return '지속 통로 회복 확인 · 사건 해결';
  if (state.delivery === 'failed') return '알림 전달 실패 · 소유자 확인 필요';
  if (state.delivery === 'unknown') return '전달 결과 미확인 · 결과 확인 필요';
  if (state.delivery === 'queued') return '알림 전달 대기';
  if (state.followup === 'insufficient') return '관측 부족 · 후속 확인 보류';
  if (state.responses.move === '이동하기 어려워요' || state.responses.move === '문의할게요' || (!state.responses.move && state.deadlinePassed)) return '소유자 확인 필요 · 사건 미해결';
  if (state.followup === 'blocked') return '통로 차단 지속 · 사건 미해결';
  return state.responses.move ? '차량 이동·통로 회복 확인 대기' : '차주 응답 대기';
}
export function canReceive(delivery: Delivery): boolean { return delivery === 'channel_accepted' || delivery === 'client_received'; }
export function checkMapping(mapping: Mapping, mappings: Mapping[], expectedVersion: number): string {
  if (mapping.version !== expectedVersion) return '다른 변경이 있습니다. 최신 내용을 확인해 주세요.';
  if (!mapping.reason.trim()) return '검토 사유를 입력해 주세요.';
  if (mapping.status === '검토된 연결' && !mapping.target) return '연결 대상을 선택해 주세요.';
  if (mapping.status !== '미연결' && mapping.target && mappings.some(m => m.object !== mapping.object && m.kind === mapping.kind && m.status !== '미연결' && m.target === mapping.target)) return '다른 관측 대상에 이미 연결되어 있습니다.';
  return '';
}
export const requiredError = (value: string, label: string) => value.trim() ? '' : `${label}을 입력해 주세요.`;
