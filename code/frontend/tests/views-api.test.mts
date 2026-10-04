import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError } from '../src/live/api.ts';
import { cursorNeedsRestart, getCommandProgress, getHistoryPage, getParkingMap, getVehicleLocations } from '../src/live/viewsApi.ts';
import type { Row } from '../src/live/api.ts';

const command = { command_id: 'c1', facility_id: 'f1', run_id: 'r1', purpose: 'operational_goal', aggregate_status: 'pending', created_at: '2026-10-04T00:00:00Z', updated_at: '2026-10-04T00:00:00Z', resource_version: 1 };
const page = (items: Row[], next_cursor: string | null = null): Row => ({ items, next_cursor, as_of_utc: '2026-10-04T00:00:00Z', consistency: 'immutable_projection_current_authority', preservation_limits: ['latest state only'] });
const location: Row = { registered_vehicle_id: 'v1', display_alias: '가상 차량', object_id: null, position: null, size: null, heading_deg: null, quality: null, observed_bay_id: null, location_status: 'unknown', location_reason: 'no_verified_relationship', evidence_observation_ids: [], evidence_reasons: [], stationary_duration_ms: null, bay_semantics: 'observed_not_assigned' };
const locations = (vehicle: Row = location): Row => ({ view_version: 'own-vehicle-locations-v1', view_scope: 'own_vehicles', facility_id: 'f1', run_id: 'r1', map_version: 'map1', observation_id: 'o1', state_version: 1, sim_time_ms: 10, observed_at: '2026-10-04T00:00:00Z', received_at: '2026-10-04T00:00:00Z', coverage: 'complete', run_status: 'paused', recovery_required: false, applied_state_version: 2, applied_sim_time_ms: 20, settings_version: null, vehicles: [vehicle] });
function reader(values: Row[]) {
  const paths: string[] = [];
  return { paths, get: async (path: string) => { paths.push(path); const value = values.shift(); assert.ok(value); return value; } };
}
test('history uses opaque next_cursor and allows empty permission pages to advance', async () => {
  const api = reader([page([], 'opaque+/=cursor'), page([command])]);
  const first = await getHistoryPage(api, 'commands', 'f1', 'r1');
  assert.equal(first.items.length, 0);
  assert.equal(first.next_cursor, 'opaque+/=cursor');
  const next = await getHistoryPage(api, 'commands', 'f1', 'r1', first.next_cursor!);
  assert.equal(next.items.length, 1); assert.equal(next.next_cursor, null);
  const initial = new URL(api.paths[0], 'http://test.invalid');
  assert.equal(initial.searchParams.has('cursor'), false);
  assert.equal(initial.searchParams.get('limit'), '50');
  const url = new URL(api.paths[1], 'http://test.invalid');
  assert.equal(url.searchParams.get('cursor'), 'opaque+/=cursor');
  assert.equal(url.searchParams.get('run_id'), 'r1');
});
test('malformed, nonadvancing, and wrong scope history responses fail closed', async () => {
  for (const value of [page([], ''), page([], 'old'), { ...page([]), next_cursor: 1 }, page([{ ...command, facility_id: 'f2' }]), page([{ ...command, run_id: 'r2' }]), page([{ ...command, resource_version: '1' }])]) {
    await assert.rejects(getHistoryPage(reader([value]), 'commands', 'f1', 'r1', 'old'), (e: unknown) => e instanceof ApiError && e.code === 'INVALID_VIEW_RESPONSE');
  }
});
test('expired or changed cursors propagate without automatic retry or mutation', async () => {
  for (const code of ['HISTORY_CURSOR_EXPIRED', 'HISTORY_CURSOR_SCOPE_CHANGED', 'INVALID_HISTORY_CURSOR']) {
    let calls = 0; const error = new ApiError(409, code, 'new read required');
    await assert.rejects(getHistoryPage({ get: async () => { calls++; throw error; } }, 'commands', 'f1'), e => e === error);
    assert.equal(calls, 1); assert.equal(cursorNeedsRestart(error), true);
  }
  assert.equal(cursorNeedsRestart(new ApiError(403, 'ACCESS_CHANGED', 'revoked')), false);
});
test('own vehicle unknown remains null and observation/apply clocks remain separate', async () => {
  const result = await getVehicleLocations(reader([locations()]), 'f1', 'r1');
  assert.equal(result.vehicles[0].position, null); assert.equal(result.vehicles[0].observed_bay_id, null);
  assert.equal(result.state_version, 1); assert.equal(result.applied_state_version, 2);
  assert.equal(result.sim_time_ms, 10); assert.equal(result.applied_sim_time_ms, 20);
  const visible = { ...location, object_id: 'o-car', position: { x: 1, y: 2 }, location_status: 'observed_bay', observed_bay_id: 'B1', location_reason: 'stationary_whole_footprint_in_unique_bay', quality: { visibility: 'visible', uncertainty_m: null, missing_fields: [] } };
  assert.equal((await getVehicleLocations(reader([locations(visible)]), 'f1', 'r1')).vehicles[0].observed_bay_id, 'B1');
});
test('invalid unknown position, inferred bay, missing evidence identity and wrong run are rejected', async () => {
  for (const vehicle of [{ ...location, position: { x: 1, y: 2 } }, { ...location, observed_bay_id: 'B1' }, { ...location, location_status: 'observed_bay', observed_bay_id: 'B1', position: { x: 1, y: 2 } }]) {
    await assert.rejects(getVehicleLocations(reader([locations(vehicle)]), 'f1', 'r1'));
  }
  await assert.rejects(getVehicleLocations(reader([{ ...locations(), run_id: 'other' }]), 'f1', 'r1'));
});
test('driver static map requests selected version and rejects mismatching map', async () => {
  const map: Row = { view_version: 'static-parking-map-v1', view_scope: 'static_geometry', facility_id: 'f1', map_version: 'm/1', coordinate_system: { unit: 'm', origin: 'southwest', x_axis: 'east', y_axis: 'north' }, bounds: { min_x: 0, min_y: 0, max_x: 10, max_y: 10 }, zones: [], parking_bays: [] };
  const api = reader([map]); await getParkingMap(api, 'f1', 'm/1');
  assert.equal(new URL(api.paths[0], 'http://test.invalid').searchParams.get('map_version'), 'm/1');
  await assert.rejects(getParkingMap(reader([map]), 'f1', 'wrong'));
});
test('progress preserves every plan, attempt and unknown linkage rather than guessing', async () => {
  const value: Row = { as_of_utc: '2026-10-04T00:00:00Z', command, preservation_limits: ['explicit only'], plans: [
    { plan_id: 'p-old', status: 'cancelled', resource_version: 1, steps: [{ index: 0, step_id: null, tool_name: null, zone_id: null, status: 'unknown', reason_code: 'legacy', attempts: [{ execution_id: 'e-old', linkage_status: 'unknown', reason_code: 'identity_missing', execution: null }] }] },
    { plan_id: 'p-new', status: 'active', resource_version: 2, steps: [] },
  ] };
  const result = await getCommandProgress(reader([value]), 'c1');
  assert.equal(result.plans.length, 2); assert.equal(result.plans[0].steps[0].attempts[0].linkage_status, 'unknown');
  assert.equal(result.plans[0].steps[0].attempts[0].execution, null);
  await assert.rejects(getCommandProgress(reader([value]), 'other'));
});
test('timeline validates identity and retains UTC versus simulation and client receipt clocks', async () => {
  const record = { event_id: 'receipt:n1', kind: 'notification.receipt', record_id: 'n1', recorded_at_utc: '2026-10-04T00:00:00Z', sim_time_ms: 10, details: { received_at: '2026-10-03T23:59:59Z' } };
  const value = { ...page([record]), incident_id: 'i1', facility_id: 'f1', run_id: 'r1' };
  const result = await getHistoryPage(reader([value]), 'timeline', 'i1');
  assert.equal(result.items.length, 1);
  await assert.rejects(getHistoryPage(reader([value]), 'timeline', 'other'));
});
