import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, ParkingApi, list, number, row, rows, text } from '../src/live/api.ts';

type FetchCall = { path: string; init: RequestInit };

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}

async function mocked(run: (calls: FetchCall[], api: ParkingApi) => Promise<void>,
  handler: (call: FetchCall) => Promise<Response> = async () => json({ ok: true })): Promise<void> {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = async (input, init) => {
    const call = { path: String(input), init: init ?? {} };
    calls.push(call);
    return handler(call);
  };
  const api = new ParkingApi(() => {});
  try { await run(calls, api); } finally {
    api.clear();
    globalThis.fetch = original;
  }
}

test('helpers preserve only supported public values', () => {
  assert.deepEqual(row(null), {});
  assert.deepEqual(row([]), {});
  assert.deepEqual(rows([{ id: 1 }, null, [], 'x']), [{ id: 1 }]);
  assert.deepEqual(list(null), []);
  assert.equal(text(0), '0');
  assert.equal(text(true), '—');
  assert.equal(number('3'), null);
  assert.equal(number(Infinity), null);
});

test('login has JSON and same-origin credentials without CSRF or idempotency headers', async () => {
  await mocked(async (calls, api) => {
    api.setCsrf('old-csrf');
    await api.login('demo-owner', 'demo-password');
    const call = calls[0];
    const headers = new Headers(call.init.headers);
    assert.equal(call.path, '/api/v1/auth/session');
    assert.equal(call.init.method, 'POST');
    assert.equal(call.init.credentials, 'same-origin');
    assert.equal(headers.get('content-type'), 'application/json');
    assert.equal(headers.has('x-csrf-token'), false);
    assert.equal(headers.has('idempotency-key'), false);
    assert.deepEqual(JSON.parse(String(call.init.body)), { username: 'demo-owner', password: 'demo-password' });
  });
});

test('401 clears CSRF and ambiguous operations before notifying expiration', async () => {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  let count = 0;
  globalThis.fetch = async (input, init) => {
    calls.push({ path: String(input), init: init ?? {} });
    count += 1;
    if (count === 1) throw new TypeError('connection lost');
    if (count === 2) return json({ error: { code: 'UNAUTHENTICATED', message: '로그인이 필요합니다.' }, correlation_id: 'public-correlation' }, 401);
    return new Response(null, { status: 204 });
  };
  let expired = 0;
  let followup: Promise<unknown> | undefined;
  const api = new ParkingApi(() => {
    expired += 1;
    // A changed body is allowed only if clear() already removed the old operation.
    followup = api.mutate('same-action', '/api/v1/example', { version: 2 });
  });
  try {
    api.setCsrf('expired-csrf');
    await assert.rejects(api.mutate('same-action', '/api/v1/example', { version: 1 }),
      (error: unknown) => error instanceof ApiError && error.ambiguous);
    await assert.rejects(api.get('/api/v1/me'),
      (error: unknown) => error instanceof ApiError && error.status === 401 && error.correlationId === 'public-correlation');
    await followup;
    assert.equal(expired, 1);
    assert.equal(new Headers(calls[2].init.headers).get('x-csrf-token'), '');
    assert.notEqual(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[2].init.headers).get('idempotency-key'));
  } finally { api.clear(); globalThis.fetch = original; }
});

test('generation change aborts and discards a late response even when transport ignores abort', async () => {
  let resolve: ((response: Response) => void) | undefined;
  let count = 0;
  await mocked(async (calls, api) => {
    api.setCsrf('retained-csrf');
    const pending = api.get('/api/v1/me');
    api.invalidate();
    assert.equal(calls[0].init.signal?.aborted, true);
    resolve!(json({ alias: 'old-session' }));
    await assert.rejects(pending, (error: unknown) => error instanceof Error && error.name === 'AbortError');
    await api.mutate('new-screen', '/api/v1/example', {});
    assert.equal(new Headers(calls[1].init.headers).get('x-csrf-token'), 'retained-csrf');
  }, async () => ++count === 1 ? new Promise<Response>((done) => { resolve = done; }) : json({ ok: true }));
});

test('ambiguous mutation permits manual exact retry with the same key and blocks a changed body', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    api.setCsrf('csrf');
    await assert.rejects(api.mutate('approve', '/api/v1/commands/c1/confirm', { version: 1 }),
      (error: unknown) => error instanceof ApiError && error.ambiguous);
    assert.equal(calls.length, 1, 'no automatic retry');
    await assert.rejects(api.mutate('approve', '/api/v1/commands/c1/confirm', { version: 2 }),
      (error: unknown) => error instanceof ApiError && error.code === 'OPERATION_RESULT_UNKNOWN' && error.ambiguous);
    await assert.rejects(api.mutate('approve', '/api/v1/commands/c2/confirm', { version: 1 }),
      (error: unknown) => error instanceof ApiError && error.code === 'OPERATION_RESULT_UNKNOWN');
    await assert.rejects(api.mutate('approve', '/api/v1/commands/c1/confirm', { version: 1 }, 'PUT'),
      (error: unknown) => error instanceof ApiError && error.code === 'OPERATION_RESULT_UNKNOWN');
    assert.equal(calls.length, 1, 'changed request never dispatched');
    await api.mutate('approve', '/api/v1/commands/c1/confirm', { version: 1 });
    assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
    assert.equal(calls[0].init.body, calls[1].init.body);
    await api.mutate('approve', '/api/v1/commands/c1/confirm', { version: 2 });
    assert.notEqual(new Headers(calls[1].init.headers).get('idempotency-key'), new Headers(calls[2].init.headers).get('idempotency-key'));
  }, async () => { if (++count === 1) throw new TypeError('response lost'); return json({ status: 'accepted' }); });
});

test('a timeout leaves mutation outcome unknown and retains the key for manual retry', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    await assert.rejects(api.mutate('timeout', '/api/v1/example', {}),
      (error: unknown) => error instanceof ApiError && error.code === 'REQUEST_TIMEOUT' && error.ambiguous);
    assert.equal(calls.length, 1);
    await api.mutate('timeout', '/api/v1/example', {});
    assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
  }, async () => {
    if (++count === 1) throw new DOMException('timed out', 'TimeoutError');
    return json({ ok: true });
  });
});

test('a reopened form manually retries the same unresolved request key and clears every alias on success', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    const body = { run_id: 'run-1', goal: 'regulation', query: '통로 규정' };
    await assert.rejects(api.mutate('first-form', '/api/v1/test/agent/live-queries', body),
      (error: unknown) => error instanceof ApiError && error.ambiguous);
    assert.equal(calls.length, 1);
    await api.mutate('reopened-form', '/api/v1/test/agent/live-queries', body);
    const key = (index: number) => new Headers(calls[index].init.headers).get('idempotency-key');
    assert.equal(key(0), key(1));
    assert.equal(calls[0].init.body, calls[1].init.body);
    await api.mutate('first-form', '/api/v1/test/agent/live-queries', body);
    assert.notEqual(key(1), key(2), 'a completed request permits a new intention');
    await api.mutate('reopened-form', '/api/v1/test/agent/live-queries', body);
    assert.notEqual(key(1), key(3), 'the alias does not retain the completed key');
  }, async () => {
    if (++count === 1) throw new TypeError('response lost');
    return json({ status: 'completed' });
  });
});

test('definite failure of an aliased retry clears the original action before a changed request', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    await assert.rejects(api.mutate('first-form', '/api/v1/example', { version: 1 }),
      (error: unknown) => error instanceof ApiError && error.ambiguous);
    await assert.rejects(api.mutate('reopened-form', '/api/v1/example', { version: 1 }),
      (error: unknown) => error instanceof ApiError && error.status === 409 && !error.ambiguous);
    assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
    await api.mutate('first-form', '/api/v1/example', { version: 2 });
    assert.notEqual(new Headers(calls[1].init.headers).get('idempotency-key'), new Headers(calls[2].init.headers).get('idempotency-key'));
  }, async () => {
    count += 1;
    if (count === 1) throw new TypeError('response lost');
    return count === 2 ? json({ error: { code: 'VERSION_CONFLICT', message: '최신 버전을 확인하세요.' } }, 409) : json({ accepted: true });
  });
});

test('a second form cannot dispatch an identical pending request while a different intention is allowed', async () => {
  let count = 0;
  let finish: ((response: Response) => void) | undefined;
  await mocked(async (calls, api) => {
    const pending = api.mutate('first-form', '/api/v1/example', { goal: 'closing' });
    await assert.rejects(api.mutate('second-form', '/api/v1/example', { goal: 'closing' }),
      (error: unknown) => error instanceof ApiError && error.code === 'OPERATION_IN_PROGRESS' && !error.ambiguous);
    assert.equal(calls.length, 1, 'identical pending request is not sent');
    await api.mutate('different-form', '/api/v1/example', { goal: 'announce' });
    assert.equal(calls.length, 2, 'a different request remains allowed');
    finish!(json({ accepted: true }));
    await pending;
    await api.mutate('second-form', '/api/v1/example', { goal: 'closing' });
    assert.notEqual(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[2].init.headers).get('idempotency-key'));
  }, async () => ++count === 1
    ? new Promise<Response>((resolve) => { finish = resolve; })
    : json({ accepted: true }));
});

test('definite version conflict allows explicit retry with a new key and body', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    await assert.rejects(api.mutate('edit', '/api/v1/example', { version: 1 }, 'PATCH'),
      (error: unknown) => error instanceof ApiError && error.status === 409 && !error.ambiguous);
    await api.mutate('edit', '/api/v1/example', { version: 2 }, 'PATCH');
    assert.notEqual(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
  }, async () => ++count === 1 ? json({ error: { code: 'VERSION_CONFLICT', message: '최신 버전을 확인하세요.' } }, 409) : json({ updated: true }));
});

test('storage failure retains the original mutation key for result reconciliation', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    await assert.rejects(api.mutate('storage', '/api/v1/example', { version: 1 }),
      (error: unknown) => error instanceof ApiError && error.status === 503 && error.code === 'STORAGE_UNAVAILABLE' && error.ambiguous);
    await assert.rejects(api.mutate('storage', '/api/v1/example', { version: 2 }),
      (error: unknown) => error instanceof ApiError && error.code === 'OPERATION_RESULT_UNKNOWN');
    assert.equal(calls.length, 1);
    await api.mutate('storage', '/api/v1/example', { version: 1 });
    assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
  }, async () => ++count === 1
    ? json({ error: { code: 'STORAGE_UNAVAILABLE', message: '같은 요청 키로 결과를 확인하세요.' } }, 503)
    : json({ accepted: true }));
});

test('unstructured 503 mutation responses retain the key without automatic retries', async () => {
  for (const content of ['upstream unavailable', '{"error":', '']) {
    let count = 0;
    await mocked(async (calls, api) => {
      await assert.rejects(api.mutate('unstructured', '/api/v1/example', {}),
        (error: unknown) => error instanceof ApiError && error.status === 503 && error.ambiguous);
      assert.equal(calls.length, 1);
      await api.mutate('unstructured', '/api/v1/example', {});
      assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
    }, async () => ++count === 1 ? new Response(content, { status: 503 }) : json({ accepted: true }));
  }
});

test('gateway failures after commit preserve exact retries, including reopened forms', async () => {
  for (const status of [502, 504]) {
    for (const content of ['<html>gateway failure</html>', '', JSON.stringify({ error: { code: 'UPSTREAM_TIMEOUT' } })]) {
      const committed = new Map<string, { command_id: string }>();
      let attempts = 0;
      await mocked(async (calls, api) => {
        const body = { text: 'closing', run_id: 'run-1' };
        await assert.rejects(api.mutate('original-form', '/api/v1/commands', body),
          (error: unknown) => error instanceof ApiError && error.status === status && error.ambiguous);
        assert.equal(calls.length, 1, 'no automatic retry');
        await assert.rejects(api.mutate('original-form', '/api/v1/commands', { ...body, text: 'changed' }),
          (error: unknown) => error instanceof ApiError && error.code === 'OPERATION_RESULT_UNKNOWN');
        assert.equal(calls.length, 1, 'changed intention cannot replace an uncertain request');
        const result = await api.mutate('reopened-form', '/api/v1/commands', body);
        assert.deepEqual(result, { command_id: 'command-1' });
        assert.equal(committed.size, 1, 'manual retry must not create another backend command');
        assert.equal(calls[0].init.body, calls[1].init.body);
        assert.equal(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
      }, async call => {
        const key = new Headers(call.init.headers).get('idempotency-key')!;
        if (!committed.has(key)) committed.set(key, { command_id: `command-${committed.size + 1}` });
        // The backend committed; only its response was lost at the gateway.
        if (++attempts === 1) return new Response(content, { status });
        return json(committed.get(key));
      });
    }
  }
});

test('gateway failures on reads are not uncertain mutations', async () => {
  for (const status of [502, 504]) {
    await mocked(async (calls, api) => {
      await assert.rejects(api.get('/api/v1/me'),
        (error: unknown) => error instanceof ApiError && error.status === status && !error.ambiguous);
      assert.equal(calls.length, 1);
    }, async () => new Response('gateway failure', { status }));
  }
});

test('a definite model configuration 503 releases the key for a new attempt', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    await assert.rejects(api.mutate('model', '/api/v1/example', { provider: 'openai' }),
      (error: unknown) => error instanceof ApiError && error.status === 503 && error.code === 'MODEL_NOT_CONFIGURED' && !error.ambiguous);
    await api.mutate('model', '/api/v1/example', { provider: 'gemini' });
    assert.notEqual(new Headers(calls[0].init.headers).get('idempotency-key'), new Headers(calls[1].init.headers).get('idempotency-key'));
  }, async () => ++count === 1
    ? json({ error: { code: 'MODEL_NOT_CONFIGURED', message: '모델 설정이 필요합니다.' } }, 503)
    : json({ accepted: true }));
});

test('logout uses CSRF when available and never an idempotency key', async () => {
  await mocked(async (calls, api) => {
    api.setCsrf('logout-csrf');
    assert.deepEqual(await api.logout(), {});
    assert.equal(calls[0].init.method, 'DELETE');
    assert.equal(new Headers(calls[0].init.headers).get('x-csrf-token'), 'logout-csrf');
    assert.equal(new Headers(calls[0].init.headers).has('idempotency-key'), false);
    await api.logout();
    assert.equal(new Headers(calls[1].init.headers).has('x-csrf-token'), false);
  }, async () => new Response(null, { status: 204 }));
});

test('failed logout retains CSRF and session for an explicit retry', async () => {
  let count = 0;
  await mocked(async (calls, api) => {
    api.setCsrf('retained-session-csrf');
    await assert.rejects(api.logout(), (error: unknown) => error instanceof ApiError && error.ambiguous);
    await assert.rejects(api.logout(), (error: unknown) => error instanceof ApiError && error.status === 403 && !error.ambiguous);
    await api.logout();
    for (const call of calls) {
      assert.equal(new Headers(call.init.headers).get('x-csrf-token'), 'retained-session-csrf');
      assert.equal(new Headers(call.init.headers).has('idempotency-key'), false);
    }
    await api.logout();
    assert.equal(new Headers(calls[3].init.headers).has('x-csrf-token'), false);
  }, async () => {
    count += 1;
    if (count === 1) throw new TypeError('connection lost');
    if (count === 2) return json({ error: { code: 'CSRF_REJECTED', message: '다시 확인하세요.' } }, 403);
    return new Response(null, { status: 204 });
  });
});

test('absolute URLs are rejected before dispatch', async () => {
  await mocked(async (calls, api) => {
    await assert.rejects(api.get('https://example.invalid/api/v1/me'),
      (error: unknown) => error instanceof ApiError && error.code === 'INVALID_PATH');
    assert.equal(calls.length, 0);
  });
});
