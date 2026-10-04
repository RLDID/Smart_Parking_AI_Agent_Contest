import test from 'node:test';
import assert from 'node:assert/strict';
import { cancelCommand, checkMapping, newCommand, notificationSummary, resolveCommand, startCommand, canReceive } from '../code/frontend/src/model.ts';
import type { Mapping } from '../code/frontend/src/model.ts';

test('cancel while running keeps the running step and cancels only not-started steps', () => {
  const cancelled = cancelCommand(startCommand(newCommand('c1', '종료', 'closing')));
  assert.equal(cancelled.cancelRequested, true);
  assert.equal(cancelled.phase, 'running');
  assert.deepEqual(cancelled.steps, ['running', 'cancelled', 'cancelled', 'cancelled']);
});
test('partial failure cancellation retains completed and failed results', () => {
  const command = resolveCommand(startCommand(newCommand('c1', '종료', 'closing')), 'partial');
  assert.deepEqual(cancelCommand(command).steps, ['succeeded', 'failed', 'cancelled', 'cancelled']);
  assert.equal(cancelCommand(command).phase, 'partial');
});
test('unknown execution is never rewritten as cancelled', () => {
  const cancelled = cancelCommand(resolveCommand(startCommand(newCommand('c1', '종료', 'closing')), 'unknown'));
  assert.equal(cancelled.phase, 'unknown'); assert.equal(cancelled.steps[1], 'unknown');
});
test('complete and failed scenarios have different outcomes and dependency holds', () => {
  const command = startCommand(newCommand('c1', '종료', 'closing'));
  assert.ok(resolveCommand(command, 'complete').steps.every(s => s === 'succeeded'));
  assert.deepEqual(resolveCommand(command, 'failed').steps, ['failed', 'held', 'held', 'held']);
});
test('a driver reply does not establish incident recovery', () => {
  const summary = notificationSummary({ delivery: 'client_received', responses: { move: '이동할게요' }, followup: 'waiting', deadlinePassed: false });
  assert.match(summary, /확인 대기/); assert.doesNotMatch(summary, /사건 해결/);
});
test('cannot move, questions and missed deadline require owner review', () => {
  for (const move of ['이동하기 어려워요', '문의할게요'] as const) assert.match(notificationSummary({ delivery: 'client_received', responses: { move }, followup: 'waiting', deadlinePassed: false }), /소유자 확인 필요/);
  assert.match(notificationSummary({ delivery: 'client_received', responses: {}, followup: 'waiting', deadlinePassed: true }), /사건 미해결/);
});
test('failed, queued and unknown deliveries are absent from driver inbox', () => {
  for (const delivery of ['failed', 'queued', 'unknown'] as const) assert.equal(canReceive(delivery), false);
  assert.equal(canReceive('client_received'), true);
});
test('mapping rejects outdated versions, absent target and duplicate verified target', () => {
  const mapping: Mapping = { object: '01', kind: 'vehicles', target: 'a', status: '검토된 연결', reason: '확인', version: 2 };
  assert.match(checkMapping(mapping, [], 1), /다른 변경/);
  assert.match(checkMapping({ ...mapping, target: '' }, [], 2), /선택/);
  assert.match(checkMapping(mapping, [{ ...mapping, object: '02' }], 2), /이미 연결/);
  assert.match(checkMapping({ ...mapping, status: '불확실' }, [{ ...mapping, object: '02' }], 2), /이미 연결/);
  assert.equal(checkMapping({ ...mapping, status: '미연결', target: '' }, [], 2), '');
});
