import assert from 'node:assert/strict';
import test from 'node:test';
import { readCollection } from '../src/live/collections.ts';
import type { Row } from '../src/live/api.ts';

function fake(pages: Row[]) {
  const paths: string[] = [];
  let pending = false;
  return {
    paths,
    api: { get: async (path: string): Promise<Row> => {
      assert.equal(pending, false, 'pages must be requested sequentially');
      pending = true;
      paths.push(path);
      await Promise.resolve();
      pending = false;
      const next = pages.shift();
      assert.ok(next, 'unexpected additional page');
      return next;
    } },
  };
}

test('empty authorized page does not stop an advancing cursor', async () => {
  const data = fake([
    { items: [], cursor: 10 },
    { items: [{ notification_id: 'n1', resource_version: 1 }], cursor: 11 },
    { items: [], cursor: 11 },
  ]);
  assert.deepEqual(await readCollection(data.api, '/api/v1/notifications'),
    [{ notification_id: 'n1', resource_version: 1 }]);
  assert.deepEqual(data.paths.map((path) => new URL(path, 'http://test.invalid').searchParams.get('cursor')),
    ['0', '10', '11']);
});

test('duplicate IDs retain the greatest resource version across pages', async () => {
  const data = fake([
    { items: [{ incident_id: 'i1', resource_version: 2, status: 'active' }], cursor: 1 },
    { items: [{ incident_id: 'i1', resource_version: 4, status: 'resolved' }], cursor: 2 },
    { items: [{ incident_id: 'i1', resource_version: 3, status: 'monitoring' }], cursor: 3 },
    { items: [], cursor: 3 },
  ]);
  assert.deepEqual(await readCollection(data.api, '/api/v1/facilities/f1/incidents'),
    [{ incident_id: 'i1', resource_version: 4, status: 'resolved' }]);
});

test('unchanged cursor terminates without another request', async () => {
  const data = fake([{ items: [], cursor: 0 }]);
  assert.deepEqual(await readCollection(data.api, '/api/v1/notifications'), []);
  assert.equal(data.paths.length, 1);
});

test('preserves run query and starts with cursor zero and limit one hundred', async () => {
  const data = fake([{ items: [], cursor: 0 }]);
  await readCollection(data.api, '/api/v1/facilities/f1/incidents?run_id=run%2Fone&cursor=999&limit=1&kind=active');
  const url = new URL(data.paths[0], 'http://test.invalid');
  assert.equal(url.pathname, '/api/v1/facilities/f1/incidents');
  assert.equal(url.searchParams.get('run_id'), 'run/one');
  assert.equal(url.searchParams.get('kind'), 'active');
  assert.equal(url.searchParams.get('cursor'), '0');
  assert.equal(url.searchParams.get('limit'), '100');
});

test('rejects invalid and backwards cursors instead of reporting a partial collection', async () => {
  for (const cursor of [-1, 0.5, '2', null, Number.MAX_SAFE_INTEGER + 1]) {
    const data = fake([{ items: [], cursor }]);
    await assert.rejects(readCollection(data.api, '/api/v1/notifications'), /커서/);
  }
  const backwards = fake([
    { items: [{ notification_id: 'n1', resource_version: 1 }], cursor: 4 },
    { items: [], cursor: 3 },
  ]);
  await assert.rejects(readCollection(backwards.api, '/api/v1/notifications'), /커서/);
});

test('rejects malformed items and propagates a page request failure', async () => {
  for (const page of [
    { items: null, cursor: 0 },
    { items: [null], cursor: 1 },
    { items: [{ resource_version: 1 }], cursor: 1 },
    { items: [{ notification_id: 'n1', resource_version: '1' }], cursor: 1 },
  ]) {
    await assert.rejects(readCollection(fake([page]).api, '/api/v1/notifications'));
  }
  const failure = new Error('request aborted');
  let count = 0;
  await assert.rejects(readCollection({ get: async () => {
    if (++count === 1) return { items: [{ notification_id: 'n1', resource_version: 1 }], cursor: 1 };
    throw failure;
  } }, '/api/v1/notifications'), (error: unknown) => error === failure);
});

test('one thousand advancing pages reject rather than return a partial result', async () => {
  let count = 0;
  await assert.rejects(readCollection({ get: async () => ({ items: [], cursor: ++count }) }, '/api/v1/notifications'),
    /조회 한도/);
  assert.equal(count, 1000);
});
