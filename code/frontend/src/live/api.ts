export type Row = Record<string, unknown>;

export function row(value: unknown): Row {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Row : {};
}

export function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function rows(value: unknown): Row[] {
  return list(value).filter((item): item is Row =>
    item !== null && typeof item === 'object' && !Array.isArray(item));
}

export function text(value: unknown, fallback = '—'): string {
  return typeof value === 'string' ? value
    : typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback;
}

export function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export class ApiError extends Error {
  status: number;
  code: string;
  ambiguous: boolean;
  correlationId?: string;

  constructor(status: number, code: string, message: string, ambiguous = false, correlationId?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.ambiguous = ambiguous;
    this.correlationId = correlationId;
  }
}

type MutationMethod = 'POST' | 'PATCH' | 'PUT' | 'DELETE';
type Operation = { signature: string; key: string; pending: boolean; ambiguous: boolean };

function aborted(): DOMException {
  return new DOMException('이전 화면의 요청을 취소했습니다.', 'AbortError');
}

function checkPath(path: string): void {
  if (!(path === '/health/ready' || path.startsWith('/api/v1/')) || /[\\\r\n#]/.test(path)) {
    throw new ApiError(400, 'INVALID_PATH', '같은 서비스의 API 경로가 필요합니다.');
  }
}

export class ParkingApi {
  private csrf = '';
  private generation = 0;
  private controllers = new Set<AbortController>();
  private operations = new Map<string, Operation>();
  private fetcher: typeof fetch;
  private onExpired: () => void;

  constructor(onExpired: () => void) {
    this.onExpired = onExpired;
    this.fetcher = globalThis.fetch.bind(globalThis);
  }

  setCsrf(token: string): void {
    this.csrf = token;
  }

  invalidate(): void {
    this.generation += 1;
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
  }

  clear(): void {
    this.invalidate();
    this.csrf = '';
    this.operations.clear();
  }

  get(path: string): Promise<Row> {
    return this.request(path, 'GET');
  }

  login(username: string, password: string): Promise<Row> {
    this.clear();
    return this.request('/api/v1/auth/session', 'POST', JSON.stringify({ username, password }));
  }

  async logout(): Promise<Row> {
    const headers = this.csrf ? { 'X-CSRF-Token': this.csrf } : undefined;
    const result = await this.request('/api/v1/auth/session', 'DELETE', undefined, headers, true, 204);
    this.clear();
    return result;
  }

  async mutate(actionId: string, path: string, body: Row, method: MutationMethod = 'POST'): Promise<Row> {
    checkPath(path);
    let encoded: string;
    try {
      encoded = JSON.stringify(body);
    } catch {
      throw new ApiError(400, 'INVALID_BODY', '요청 내용을 확인하세요.');
    }
    const signature = JSON.stringify([method, path, encoded]);
    let operation = this.operations.get(actionId);
    if (operation && operation.signature !== signature) {
      throw new ApiError(409, 'OPERATION_RESULT_UNKNOWN', '기존 요청 결과 확인 필요', true);
    }
    if (operation?.pending) {
      throw new ApiError(409, 'OPERATION_IN_PROGRESS', '기존 요청을 처리하고 있습니다.');
    }
    if (!operation) {
      if ([...this.operations.values()].some(value => value.pending && value.signature === signature)) {
        throw new ApiError(409, 'OPERATION_IN_PROGRESS', '기존 요청을 처리하고 있습니다.');
      }
      // Reopening a form creates another action ID, but an unresolved exact
      // request still belongs to its original idempotency key.
      operation = [...this.operations.values()].find(value =>
        !value.pending && value.ambiguous && value.signature === signature)
        ?? { signature, key: globalThis.crypto.randomUUID(), pending: false, ambiguous: false };
      this.operations.set(actionId, operation);
    }
    operation.pending = true;
    try {
      const result = await this.request(path, method, encoded, {
        'X-CSRF-Token': this.csrf,
        'Idempotency-Key': operation.key,
      }, true);
      if (this.operations.get(actionId) === operation) this.forget(operation);
      return result;
    } catch (error) {
      if (this.operations.get(actionId) === operation) {
        if (error instanceof ApiError && !error.ambiguous) this.forget(operation);
        else {
          operation.pending = false;
          operation.ambiguous = true;
        }
      }
      throw error;
    }
  }

  private forget(operation: Operation): void {
    for (const [actionId, value] of this.operations) {
      if (value === operation) this.operations.delete(actionId);
    }
  }

  private async request(path: string, method: string, body?: string,
    extraHeaders?: Record<string, string>, mutation = false, expectedStatus?: number): Promise<Row> {
    checkPath(path);
    const generation = this.generation;
    const controller = new AbortController();
    this.controllers.add(controller);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 30_000);
    try {
      const response = await this.fetcher(path, {
        method, credentials: 'same-origin', signal: controller.signal,
        headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...extraHeaders },
        ...(body === undefined ? {} : { body }),
      });
      if (generation !== this.generation) throw aborted();
      let value: unknown = {};
      let invalidResponse = false;
      try {
        const content = await response.text();
        if (content) value = JSON.parse(content);
        else if (response.status !== 204 && response.status !== 205) invalidResponse = true;
      } catch {
        invalidResponse = true;
      }
      if (generation !== this.generation) throw aborted();
      const data = row(value);
      if (!response.ok) {
        const envelope = row(data.error);
        const code = typeof envelope.code === 'string' && envelope.code ? envelope.code : 'HTTP_ERROR';
        // A gateway may lose the response after the backend commits a mutation.
        // Reconcile with the original key, as for storage/unstructured 503 errors.
        const ambiguous = mutation && (code === 'STORAGE_UNAVAILABLE'
          || response.status === 502 || response.status === 504
          || response.status === 503 && (invalidResponse || code === 'HTTP_ERROR'));
        const error = new ApiError(response.status, code,
          text(envelope.message, '요청을 완료하지 못했습니다.'), ambiguous,
          typeof data.correlation_id === 'string' ? data.correlation_id : undefined);
        if (response.status === 401) {
          this.clear();
          try { this.onExpired(); } finally { throw error; }
        }
        throw error;
      }
      if (invalidResponse) {
        throw new ApiError(response.status, 'INVALID_RESPONSE', '응답을 확인하지 못했습니다.', mutation);
      }
      if (expectedStatus !== undefined && response.status !== expectedStatus) {
        throw new ApiError(response.status, 'INVALID_RESPONSE', '작업 완료 응답을 확인하지 못했습니다.', mutation);
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (generation !== this.generation) throw aborted();
      throw new ApiError(0, timedOut || error instanceof Error && error.name === 'TimeoutError'
        ? 'REQUEST_TIMEOUT' : 'NETWORK_ERROR',
      mutation ? '응답을 확인하지 못했습니다. 기존 요청 결과를 확인하거나 같은 요청으로 다시 시도하세요.'
        : '연결을 확인한 뒤 다시 시도하세요.', mutation);
    } finally {
      clearTimeout(timer);
      this.controllers.delete(controller);
    }
  }
}
