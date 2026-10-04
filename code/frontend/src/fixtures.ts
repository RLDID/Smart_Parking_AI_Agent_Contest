import type { State } from './model';
export const accounts = { owner: { id: 'owner-demo', password: 'parking-demo' }, driver: { id: 'driver-demo', password: 'parking-demo' } };
export const observedAt = '2026-09-30 09:00 KST';
// Display-only examples, not a bay assignment or a conversion of server coordinates.
export const driverLocations = {
  bay: { heading: '주차면 번호', value: 'B02' },
  aisle: { heading: '마지막 관측 위치', value: '서측 통로' },
};
export const cases = [
  { id: 'aisle', title: '통로 차단 후보', place: '서측 통로', type: '주차 질서', status: '후속 확인 중', tone: 'red', time: '14:21', summary: '통로에 머무른 차량과 통행 공간을 다시 확인합니다.', response: '이동할게요', next: '차량 이동·통로 회복 확인 필요' },
  { id: 'exit', title: '출차 방해 신고', place: 'B01 주변', type: '차주 신고', status: '확인 필요', tone: 'amber', time: '14:16', summary: '차주 신고 위치와 출차 공간을 확인합니다.', response: '아직 요청하지 않음', next: '신고 내용·현재 관측 대조 필요' },
  { id: 'line', title: '주차선 침범 후보', place: 'B03 주변', type: '주차 질서', status: '보류', tone: 'amber', time: '14:08', summary: '주차면 영향 확인에 필요한 관측이 부족합니다.', response: '요청 보류', next: '가림 해소 후 주차면·통행 영향 확인' },
  { id: 'risk', title: '접근 위험 확인', place: '보행 구역', type: '접근 위험', status: '해결', tone: 'green', time: '13:52', summary: '경보와 후속 관측 결과를 확인했습니다.', response: '경보 확인', next: '후속 확인 완료' },
];
export const mapCars = [{ id: '01', name: '시연 차량 A', place: 'B01', x: 137, y: 88 }, { id: '02', name: '시연 차량 B', place: '서측 통로', x: 49, y: 161 }];
export const devices = {
  gate: { title: '입출차 게이트', subtitle: '입차 허용 · 열림', facts: [['입차 정책', '허용'], ['게이트 물리 상태', '열림'], ['장애물 관측', '확인 전'], ['현장 연결', '연결 전']] },
  sound: { title: '안내 방송', subtitle: '대기', facts: [['요청 접수', '없음'], ['합성 재생 결과', '확인 전'], ['브라우저 재생', '미시작'], ['시설 스피커 전달', '연결 전']] },
  alarm: { title: '시각·음향 경보', subtitle: '대기', facts: [['시각 경보', '대기'], ['음향 장치 상태', '확인 전'], ['브라우저 청취', '확인 전'], ['현장 경보 전달', '연결 전']] },
};
export const deliveryLabels = { queued: '전달 대기', channel_accepted: '채널 접수', client_received: '화면 수신', failed: '전달 실패', unknown: '결과 미확인' };
export const modes = [['normal', '기본'], ['empty', '빈 상태'], ['loading', '로딩'], ['stale', '오래된 관측'], ['offline', '연결 끊김'], ['error', '조회 실패'], ['expired', '세션 만료'], ['revoked', '권한 철회']] as const;
export function initialState(): State {
  return {
    session: null, mode: 'normal', filter: 'all', tab: 'customers', vehicle: 'car-b', selectedCar: '', driverLocation: 'bay', commands: [],
    customers: [{ id: 'customer-a', name: '가상 차주 A', active: '활성', version: 1, reason: '시안 초기 등록' }, { id: 'customer-b', name: '가상 차주 B', active: '활성', version: 1, reason: '시안 초기 등록' }],
    vehicles: [{ id: 'car-a', name: '시연 차량 A', active: '활성', customer: 'customer-a', version: 1, reason: '시안 초기 등록' }, { id: 'car-b', name: '시연 차량 B', active: '활성', customer: 'customer-b', version: 1, reason: '시안 초기 등록' }],
    mappings: [{ object: '관측 차량 01', kind: 'vehicles', target: 'car-a', status: '검토된 연결', reason: '공개 샘플 연결 예시', version: 1 }, { object: '관측 차량 02', kind: 'vehicles', target: 'car-b', status: '검토된 연결', reason: '공개 샘플 연결 예시', version: 1 }, { object: '관측 사람 01', kind: 'customers', target: '', status: '불확실', reason: '고객 연결 확인 전', version: 1 }],
    drafts: {}, responses: {}, reports: [], delivery: 'client_received', followup: 'waiting', deadlinePassed: false, outcome: 'partial', saveScenario: 'normal', returnCase: '/owner/incidents',
  };
}
