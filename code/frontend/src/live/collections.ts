import type { ParkingApi, Row } from './api';

const PAGE_LIMIT = 100;
const MAX_PAGES = 1000;

function isRow(value: unknown): value is Row {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function nonnegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

/** Read a complete authorized collection; never return a partial result. */
export async function readCollection(api: Pick<ParkingApi, 'get'>, path: string): Promise<Row[]> {
  const separator = path.indexOf('?');
  const pathname = separator < 0 ? path : path.slice(0, separator);
  const query = new URLSearchParams(separator < 0 ? '' : path.slice(separator + 1));
  const merged = new Map<string, Row>();
  let cursor = 0;
  query.set('limit', String(PAGE_LIMIT));

  for (let pageIndex = 0; pageIndex < MAX_PAGES; pageIndex += 1) {
    query.set('cursor', String(cursor));
    const page = await api.get(`${pathname}?${query.toString()}`);
    if (!isRow(page) || !Array.isArray(page.items) || page.items.length > PAGE_LIMIT) {
      throw new Error('목록 응답 형식을 확인하지 못했습니다.');
    }
    const nextCursor = page.cursor;
    if (!nonnegativeInteger(nextCursor) || nextCursor < cursor) {
      throw new Error('목록 조회 커서가 올바르지 않습니다.');
    }

    for (const item of page.items) {
      if (!isRow(item) || !nonnegativeInteger(item.resource_version)) {
        throw new Error('목록 항목의 버전을 확인하지 못했습니다.');
      }
      const kind = typeof item.notification_id === 'string' && item.notification_id.length > 0
        ? 'notification_id' : 'incident_id';
      const id = item[kind];
      if (typeof id !== 'string' || !id) {
        throw new Error('목록 항목의 식별자를 확인하지 못했습니다.');
      }
      const key = `${kind}:${id}`;
      const previous = merged.get(key);
      if (!previous || item.resource_version >= (previous.resource_version as number)) {
        merged.set(key, item);
      }
    }

    // An empty page may still advance past records excluded by authorization.
    if (nextCursor === cursor) return [...merged.values()];
    cursor = nextCursor;
  }
  throw new Error('목록 조회 한도를 초과했습니다. 전체 목록을 확인하지 못했습니다.');
}
