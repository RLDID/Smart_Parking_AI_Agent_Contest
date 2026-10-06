import test from 'node:test';
import assert from 'node:assert/strict';
import { hasAudibleAlarm } from '../src/live/alarmTone.ts';

test('only current run active audio feedback causes browser alarm', () => {
  const alarms = [{ desired_active: true, audio: 'on' }];
  assert.equal(hasAudibleAlarm({ run_id: 'r1', alarms }, 'r1'), true);
  assert.equal(hasAudibleAlarm({ run_id: 'old', alarms }, 'r1'), false);
  for (const audio of ['off', 'unknown', 'failed', undefined]) {
    assert.equal(hasAudibleAlarm({ run_id: 'r1', alarms: [{ desired_active: true, audio }] }, 'r1'), false);
  }
  assert.equal(hasAudibleAlarm({ run_id: 'r1', alarms: [{ desired_active: false, audio: 'on' }] }, 'r1'), false);
  for (const invalid of [null, {}, { run_id: 'r1', alarms: [null, {}] }]) assert.equal(hasAudibleAlarm(invalid, 'r1'), false);
});
