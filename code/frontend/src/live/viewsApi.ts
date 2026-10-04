import { ApiError } from './api.ts';
import type { Row } from './api';

export type Point = { x: number; y: number };
export type VehicleLocation = {
  registered_vehicle_id: string; display_alias: string; object_id: string | null;
  position: Point | null; size: { length_m: number; width_m: number } | null;
  heading_deg: number | null; quality: { visibility: string; uncertainty_m: number | null; missing_fields: string[] } | null;
  observed_bay_id: string | null; location_status: 'observed_bay' | 'observed_position' | 'unknown';
  location_reason: string; evidence_observation_ids: string[]; evidence_reasons: string[];
  stationary_duration_ms: number | null; bay_semantics: 'observed_not_assigned';
};
export type VehicleLocations = {
  view_version: 'own-vehicle-locations-v1'; view_scope: 'own_vehicles'; facility_id: string;
  run_id: string; map_version: string; observation_id: string; state_version: number;
  sim_time_ms: number; observed_at: string; received_at: string; coverage: 'complete' | 'partial' | 'unavailable';
  run_status: string; recovery_required: boolean; applied_state_version: number;
  applied_sim_time_ms: number; settings_version: string | null; vehicles: VehicleLocation[];
};
export type OwnParkingMap = {
  view_version: 'static-parking-map-v1'; view_scope: 'static_geometry'; facility_id: string; map_version: string;
  coordinate_system: { unit: 'm'; origin: 'southwest'; x_axis: 'east'; y_axis: 'north' };
  bounds: { min_x: number; min_y: number; max_x: number; max_y: number };
  zones: { zone_id: string; type: string; polygon: Point[] }[]; parking_bays: { zone_id: string }[];
};
export type HistoryCommand = {
  command_id: string; facility_id: string; run_id: string; purpose: string; aggregate_status: string;
  cancellation_requested_at?: string | null; created_at: string; updated_at: string; resource_version: number;
};
export type HistoryExecution = {
  execution_id: string; facility_id: string; run_id: string; command_id?: string | null;
  incident_id?: string | null; plan_id?: string | null; tool_name: string; status: string;
  mode: 'synthetic_demo' | 'simulated' | 'live'; applied_sim_time_ms?: number | null;
  cancellation_requested_at?: string | null; error_code?: string | null;
  created_at: string; updated_at: string; resource_version: number;
};
export type HistoryPage<T> = {
  as_of_utc: string; next_cursor: string | null; consistency: 'immutable_projection_current_authority';
  preservation_limits: string[]; items: T[];
};
export type TimelineDetails = {
  status?: string | null; mode?: string | null; tool_name?: string | null; purpose?: string | null;
  delivery_status?: string | null; response?: string | null; attempt_number?: number | null;
  error_code?: string | null; action?: string | null; outcome?: string | null; reason_code?: string | null;
  clock?: string | null; due_sim_time_ms?: number | null; due_at?: string | null; received_at?: string | null;
  created_at?: string | null; updated_at?: string | null; resource_version?: number | null;
};
export type TimelineRecord = {
  event_id: string; kind: string; record_id: string; parent_record_id?: string | null;
  recorded_at_utc: string; sim_time_ms?: number | null; details: TimelineDetails;
};
export type IncidentTimeline = HistoryPage<TimelineRecord> & { incident_id: string; facility_id: string; run_id: string };
export type CommandProgress = {
  as_of_utc: string; command: HistoryCommand; preservation_limits: string[];
  plans: { plan_id: string; status: string; resource_version: number; steps: {
    index: number; step_id?: string | null; tool_name?: string | null; zone_id?: string | null;
    status: string; reason_code?: string | null;
    attempts: { execution_id: string; linkage_status: 'linked' | 'unknown'; reason_code?: string | null; execution?: HistoryExecution | null }[];
  }[] }[];
};

type Reader = { get(path: string): Promise<Row> };
type Check = (value: unknown) => boolean;
const str: Check = v => typeof v === 'string';
const num: Check = v => typeof v === 'number' && Number.isFinite(v);
const integer: Check = v => num(v) && Number.isSafeInteger(v) && (v as number) >= 0;
const nullable = (check: Check): Check => v => v === null || check(v);
const optional = (check: Check): Check => v => v === undefined || v === null || check(v);
const array = (check: Check): Check => v => Array.isArray(v) && v.every(check);
const strings = array(str);
const one = (...values: unknown[]): Check => v => values.includes(v);
const shape = (fields: Record<string, Check>): Check => v => v !== null && typeof v === 'object' && !Array.isArray(v)
  && Object.entries(fields).every(([key, check]) => check((v as Row)[key]));
const point = shape({ x: num, y: num });
const command = shape({ command_id: str, facility_id: str, run_id: str, purpose: str, aggregate_status: str,
  cancellation_requested_at: optional(str), created_at: str, updated_at: str, resource_version: integer });
const execution = shape({ execution_id: str, facility_id: str, run_id: str, command_id: optional(str),
  incident_id: optional(str), plan_id: optional(str), tool_name: str, status: str,
  mode: one('synthetic_demo', 'simulated', 'live'), applied_sim_time_ms: optional(integer),
  cancellation_requested_at: optional(str), error_code: optional(str), created_at: str, updated_at: str, resource_version: integer });
const location = shape({ registered_vehicle_id: str, display_alias: str, object_id: nullable(str), position: nullable(point),
  size: nullable(shape({ length_m: num, width_m: num })), heading_deg: nullable(num),
  quality: nullable(shape({ visibility: one('visible', 'occluded', 'missing'), uncertainty_m: nullable(num), missing_fields: strings })),
  observed_bay_id: nullable(str), location_status: one('observed_bay', 'observed_position', 'unknown'),
  location_reason: one('stationary_whole_footprint_in_unique_bay', 'no_verified_relationship', 'ambiguous_relationship',
    'target_not_observed', 'observation_unavailable', 'observation_insufficient', 'stale_observation', 'run_not_ready',
    'moving', 'insufficient_history', 'uncertain_stationarity', 'outside_or_intruding_bay', 'ambiguous_bay', 'unsupported_geometry'),
  evidence_observation_ids: strings, evidence_reasons: strings, stationary_duration_ms: nullable(integer), bay_semantics: one('observed_not_assigned') });
const locations = shape({ view_version: one('own-vehicle-locations-v1'), view_scope: one('own_vehicles'),
  facility_id: str, run_id: str, map_version: str, observation_id: str, state_version: integer, sim_time_ms: integer,
  observed_at: str, received_at: str, coverage: one('complete', 'partial', 'unavailable'),
  run_status: one('running', 'paused', 'stopped', 'replaying'), recovery_required: v => typeof v === 'boolean',
  applied_state_version: integer, applied_sim_time_ms: integer, settings_version: nullable(str), vehicles: array(location) });
const parkingMap = shape({ view_version: one('static-parking-map-v1'), view_scope: one('static_geometry'),
  facility_id: str, map_version: str, coordinate_system: shape({ unit: one('m'), origin: one('southwest'), x_axis: one('east'), y_axis: one('north') }),
  bounds: shape({ min_x: num, min_y: num, max_x: num, max_y: num }),
  zones: array(shape({ zone_id: str, type: one('parking_bay', 'aisle', 'entrance', 'exit', 'pedestrian', 'announcement'), polygon: array(point) })),
  parking_bays: array(shape({ zone_id: str })) });
const details = shape(Object.fromEntries([
  ...['status', 'mode', 'tool_name', 'purpose', 'delivery_status', 'response', 'error_code', 'action', 'outcome', 'reason_code', 'clock', 'due_at', 'received_at', 'created_at', 'updated_at'].map(key => [key, optional(str)]),
  ...['attempt_number', 'due_sim_time_ms', 'resource_version'].map(key => [key, optional(integer)]),
]));
const timeline = shape({ event_id: str, kind: one('incident.latest_state', 'execution.latest_state', 'notification.latest_state',
  'delivery_attempt.latest_state', 'notification.receipt', 'notification.response', 'followup.latest_state', 'audit'),
  record_id: str, parent_record_id: optional(str), recorded_at_utc: str, sim_time_ms: optional(integer), details });
const progress = shape({ as_of_utc: str, command, preservation_limits: strings,
  plans: array(shape({ plan_id: str, status: str, resource_version: integer,
    steps: array(shape({ index: integer, step_id: optional(str), tool_name: optional(str), zone_id: optional(str), status: str,
      reason_code: optional(str), attempts: array(shape({ execution_id: str, linkage_status: one('linked', 'unknown'),
        reason_code: optional(str), execution: optional(execution) })) })) })) });
function checked<T>(value: unknown, check: Check): T {
  if (!check(value)) throw new ApiError(0, 'INVALID_VIEW_RESPONSE', '조회 응답 형식을 확인하지 못했습니다. 다시 조회해 주세요.');
  return value as T;
}
function query(values: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== '') params.set(key, value); });
  return params.size ? `?${params}` : '';
}
export async function getVehicleLocations(api: Reader, facilityId: string, runId: string): Promise<VehicleLocations> {
  const value = checked<VehicleLocations>(await api.get(`/api/v1/me/vehicle-locations${query({ facility_id: facilityId, run_id: runId })}`), locations);
  if (value.facility_id !== facilityId || value.run_id !== runId || value.vehicles.some(v =>
    v.location_status === 'unknown' && v.position !== null || v.location_status !== 'observed_bay' && v.observed_bay_id !== null
    || v.location_status === 'observed_bay' && (!v.position || !v.object_id || !v.observed_bay_id))) checked(null, locations);
  return value;
}
export async function getParkingMap(api: Reader, facilityId: string, mapVersion?: string): Promise<OwnParkingMap> {
  const value = checked<OwnParkingMap>(await api.get(`/api/v1/me/parking-map${query({ facility_id: facilityId, map_version: mapVersion })}`), parkingMap);
  if (value.facility_id !== facilityId || mapVersion && value.map_version !== mapVersion) checked(null, parkingMap);
  return value;
}
export type HistoryKind = 'commands' | 'executions' | 'timeline';
export function historyPath(kind: HistoryKind, id: string, runId?: string, cursor?: string): string {
  const path = kind === 'timeline' ? `/api/v1/incidents/${encodeURIComponent(id)}/timeline`
    : `/api/v1/facilities/${encodeURIComponent(id)}/${kind}`;
  return path + query({ run_id: kind === 'timeline' ? undefined : runId, limit: '50', cursor });
}
export async function getHistoryPage<T>(api: Reader, kind: HistoryKind, id: string, runId?: string, cursor?: string): Promise<HistoryPage<T>> {
  const item = kind === 'commands' ? command : kind === 'executions' ? execution : timeline;
  const value = checked<HistoryPage<T>>(await api.get(historyPath(kind, id, runId, cursor)), shape({
    as_of_utc: str, next_cursor: nullable(str), consistency: one('immutable_projection_current_authority'), preservation_limits: strings, items: array(item),
    ...(kind === 'timeline' ? { incident_id: one(id), facility_id: str, run_id: str } : {}),
  }));
  if (value.next_cursor !== null && (!value.next_cursor || value.next_cursor === cursor)) checked(null, one(true));
  if (kind !== 'timeline' && value.items.some(item => {
    const record = item as HistoryCommand | HistoryExecution;
    return record.facility_id !== id || runId && record.run_id !== runId;
  })) checked(null, one(true));
  return value;
}
export async function getCommandProgress(api: Reader, id: string): Promise<CommandProgress> {
  const value = checked<CommandProgress>(await api.get(`/api/v1/commands/${encodeURIComponent(id)}/progress`), progress);
  if (value.command.command_id !== id) checked(null, progress);
  return value;
}
export function cursorNeedsRestart(error: unknown): boolean {
  return error instanceof ApiError && ['HISTORY_CURSOR_EXPIRED', 'HISTORY_CURSOR_SCOPE_CHANGED', 'INVALID_HISTORY_CURSOR'].includes(error.code);
}
