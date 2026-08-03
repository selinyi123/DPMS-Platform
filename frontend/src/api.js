const ENV = import.meta.env || {};
const BASE = ENV.VITE_API_BASE || '/api';
const DEFAULT_TIMEOUT_MS = Number(ENV.VITE_API_TIMEOUT_MS || 15000);

export class ApiRequestError extends Error {
  constructor(
    message,
    {
      code = 'api_error',
      path = '',
      retryable = false,
      status = null,
      serverCode = null,
      details = null,
      cause = null,
    } = {},
  ) {
    super(message);
    this.name = 'ApiRequestError';
    this.code = code;
    this.path = path;
    this.retryable = Boolean(retryable);
    this.status = Number.isInteger(status) ? status : null;
    this.serverCode = typeof serverCode === 'string' && serverCode ? serverCode : null;
    this.details = details && typeof details === 'object' ? details : null;
    if (cause) this.cause = cause;
  }
}

export function isRetryableApiError(error) {
  return error?.retryable === true;
}

export function isAuthenticationApiError(error) {
  return error?.status === 401 || error?.status === 403;
}

function adminHeaders(extra = {}) {
  const token = localStorage.getItem('dpms_admin_token') || '';
  const headers = { ...extra };
  if (token) headers['x-admin-token'] = token;
  return headers;
}

function confirmedHeaders(extra = {}) {
  return adminHeaders({ ...extra, 'x-confirm-action': 'true' });
}

export function apiPath(path) {
  const source = String(path || '');
  if (/^[a-z][a-z0-9+.-]*:/i.test(source) || source.startsWith('//')) {
    // Every authenticated request carries the administrator credential.
    // Accept only application-relative paths so future call sites cannot
    // accidentally send it to an operator-controlled absolute URL.
    throw new Error('Absolute authenticated API paths are forbidden');
  }
  const normalizedBase = BASE.endsWith('/') ? BASE.slice(0, -1) : BASE;
  const normalizedPath = source.startsWith('/') ? source : `/${source}`;
  return `${normalizedBase}${normalizedPath}`;
}

async function parseResponse(res, path = '') {
  const text = await res.text();
  const contentType = res.headers.get('content-type') || '';
  let data = null;
  let jsonError = null;

  if (text) {
    if (contentType.includes('application/json')) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        jsonError = err;
      }
    } else {
      try {
        data = JSON.parse(text);
      } catch {
        data = { message: text.slice(0, 500) };
      }
    }
  }

  if (!res.ok) {
    const isHtml = contentType.includes('text/html')
      || /^\s*(?:<!doctype\s+html|<html\b)/i.test(text);
    const isServerFailure = res.status >= 500 && res.status <= 599;
    const detail = isHtml && isServerFailure
      ? 'API service is temporarily unavailable'
      : data?.detail || data?.message || data?.error || res.statusText || `HTTP ${res.status}`;
    const structuredDetail = detail && typeof detail === 'object' && !Array.isArray(detail)
      ? detail
      : null;
    const serverCode = typeof structuredDetail?.code === 'string'
      ? structuredDetail.code
      : null;
    const message = typeof detail === 'string'
      ? detail
      : String(structuredDetail?.message || serverCode || res.statusText || `HTTP ${res.status}`);
    throw new ApiRequestError(`${res.status}: ${message}`, {
      code: 'http_error',
      path,
      retryable: isServerFailure,
      status: res.status,
      serverCode,
      details: structuredDetail,
      cause: jsonError,
    });
  }

  if (jsonError) {
    throw new Error(`Invalid JSON response from API: ${jsonError.message}`);
  }

  return data;
}

async function requestJSON(path, options = {}) {
  const controller = new AbortController();
  const callerSignal = options.signal;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
  }
  const timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const res = await fetch(apiPath(path), {
      ...options.fetchOptions,
      method: options.method || 'GET',
      headers: options.headers,
      body: options.body,
      signal: controller.signal,
    });
    return await parseResponse(res, path);
  } catch (err) {
    if (err.name === 'AbortError') {
      if (!timedOut && callerSignal?.aborted) {
        throw err;
      }
      throw new ApiRequestError(
        `API request timed out after ${timeoutMs}ms: ${path}`,
        {
          code: 'timeout',
          path,
          retryable: true,
          cause: err,
        },
      );
    }
    if (err instanceof TypeError) {
      throw new ApiRequestError(`API network error for ${path}`, {
        code: 'network_error',
        path,
        retryable: true,
        cause: err,
      });
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
    callerSignal?.removeEventListener('abort', abortFromCaller);
  }
}

export async function fetchJSON(path, options = {}) {
  // Always attach the admin token: GET /api endpoints are now authenticated
  // (default-closed), so every read must carry the token. The previous
  // opt-in `options.auth` is kept as a harmless no-op for older call sites.
  return requestJSON(path, {
    headers: adminHeaders(),
    signal: options.signal,
    timeoutMs: options.timeoutMs,
  });
}

export async function postJSON(path, body, options = {}) {
  return requestJSON(path, {
    method: 'POST',
    headers: options.confirm
      ? confirmedHeaders({ 'Content-Type': 'application/json' })
      : adminHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
    signal: options.signal,
    timeoutMs: options.timeoutMs,
  });
}

export async function putJSON(path, body, options = {}) {
  return requestJSON(path, {
    method: 'PUT',
    headers: options.confirm
      ? confirmedHeaders({ 'Content-Type': 'application/json' })
      : adminHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
    signal: options.signal,
    timeoutMs: options.timeoutMs,
  });
}

export async function deleteJSON(path, options = {}) {
  return requestJSON(path, {
    method: 'DELETE',
    headers: options.confirm ? confirmedHeaders() : adminHeaders(),
    signal: options.signal,
    timeoutMs: options.timeoutMs,
  });
}

export function getAdminHeaders(extra = {}) {
  return adminHeaders(extra);
}

export function getConfirmedHeaders(extra = {}) {
  return confirmedHeaders(extra);
}

export const AUTHENTICATED_BLOB_MAX_BYTES = 32 * 1024 * 1024;
export const AUTHENTICATED_BLOB_MAX_CHUNKS = 4_096;
export const SSE_MAX_EVENT_BUFFER_CHARS = 256 * 1024;
export const SSE_MAX_READ_CHUNK_BYTES = 1024 * 1024;
export const SSE_MIN_RETRY_MS = 250;
export const SSE_MAX_RETRY_MS = 60_000;

function declaredContentLength(response) {
  const raw = response.headers.get('content-length');
  if (raw === null || raw === '') return null;
  if (!/^(0|[1-9][0-9]*)$/.test(raw)) {
    throw new Error('Authenticated response has an invalid Content-Length');
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) {
    throw new Error('Authenticated response has an invalid Content-Length');
  }
  return value;
}

export async function readBoundedResponseBlob(
  response,
  {
    maxBytes = AUTHENTICATED_BLOB_MAX_BYTES,
    mediaType = 'application/octet-stream',
  } = {},
) {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    throw new Error('Authenticated response byte limit is invalid');
  }
  const declaredBytes = declaredContentLength(response);
  if (declaredBytes !== null && declaredBytes > maxBytes) {
    await response.body?.cancel().catch(() => {});
    throw new Error('Authenticated evidence image exceeds the safety limit');
  }
  if (!response.body) {
    throw new Error('Authenticated evidence response body is unavailable');
  }
  const reader = response.body.getReader();
  const chunks = [];
  let receivedBytes = 0;
  let completed = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) {
        throw new Error('Authenticated evidence response chunk is invalid');
      }
      if (value.byteLength > maxBytes - receivedBytes) {
        await reader.cancel().catch(() => {});
        throw new Error('Authenticated evidence image exceeds the safety limit');
      }
      if (chunks.length >= AUTHENTICATED_BLOB_MAX_CHUNKS) {
        await reader.cancel().catch(() => {});
        throw new Error('Authenticated evidence response is too fragmented');
      }
      receivedBytes += value.byteLength;
      chunks.push(value);
    }
    completed = true;
  } finally {
    if (!completed) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  return new Blob(chunks, { type: mediaType });
}

export async function fetchAuthenticatedBlob(path, options = {}) {
  const controller = new AbortController();
  const callerSignal = options.signal;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
  }
  const timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const response = await fetch(apiPath(path), {
      method: 'GET',
      headers: adminHeaders({ Accept: 'image/png' }),
      signal: controller.signal,
    });
    if (!response.ok) return await parseResponse(response, path);
    const contentType = String(response.headers.get('content-type') || '')
      .split(';', 1)[0]
      .trim()
      .toLowerCase();
    if (contentType !== 'image/png') {
      throw new Error('Authenticated evidence response is not a PNG image');
    }
    return await readBoundedResponseBlob(response, {
      maxBytes: AUTHENTICATED_BLOB_MAX_BYTES,
      mediaType: contentType,
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      if (!timedOut && callerSignal?.aborted) throw err;
      throw new ApiRequestError(
        `API request timed out after ${timeoutMs}ms: ${path}`,
        {
          code: 'timeout',
          path,
          retryable: true,
          cause: err,
        },
      );
    }
    if (err instanceof TypeError) {
      throw new ApiRequestError(`API network error for ${path}`, {
        code: 'network_error',
        path,
        retryable: true,
        cause: err,
      });
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
    callerSignal?.removeEventListener('abort', abortFromCaller);
  }
}

export function parseServerSentEventBlock(block) {
  const dataLines = [];
  for (const line of String(block || '').split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    if (line === 'data') {
      dataLines.push('');
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''));
    }
  }
  return dataLines.length ? dataLines.join('\n') : null;
}

function retryDelay(delayMs, signal) {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const finish = () => {
      window.clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = window.setTimeout(finish, delayMs);
    signal.addEventListener('abort', finish, { once: true });
  });
}

export function subscribeAuthenticatedEventStream(
  path,
  {
    onOpen = () => {},
    onMessage = () => {},
    onError = () => {},
    retryMs = 3_000,
  } = {},
) {
  const controller = new AbortController();
  const run = async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(apiPath(path), {
          method: 'GET',
          headers: adminHeaders({ Accept: 'text/event-stream' }),
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) {
          await parseResponse(response, path);
          throw new Error(`HTTP ${response.status}`);
        }
        const contentType = String(
          response.headers.get('content-type') || '',
        ).split(';', 1)[0].trim().toLowerCase();
        if (contentType !== 'text/event-stream') {
          await response.body?.cancel().catch(() => {});
          throw new Error('SSE response Content-Type is invalid');
        }
        if (!response.body) {
          throw new Error('SSE response body is unavailable');
        }
        onOpen();
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let buffer = '';
        try {
          while (!controller.signal.aborted) {
            const { value, done } = await reader.read();
            const chunk = value || new Uint8Array();
            if (chunk.byteLength > SSE_MAX_READ_CHUNK_BYTES) {
              throw new Error('SSE response chunk exceeds the safety limit');
            }
            buffer += decoder.decode(chunk, {
              stream: !done,
            });
            let separator = buffer.match(/\r?\n\r?\n/);
            while (separator && separator.index !== undefined) {
              if (separator.index > SSE_MAX_EVENT_BUFFER_CHARS) {
                throw new Error('SSE event exceeds the safety limit');
              }
              const block = buffer.slice(0, separator.index);
              buffer = buffer.slice(separator.index + separator[0].length);
              const data = parseServerSentEventBlock(block);
              if (data !== null) onMessage({ data });
              separator = buffer.match(/\r?\n\r?\n/);
            }
            if (buffer.length > SSE_MAX_EVENT_BUFFER_CHARS) {
              throw new Error('SSE event exceeds the safety limit');
            }
            if (done) throw new Error('SSE stream ended');
          }
        } finally {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        onError(error);
        const requestedRetryMs = Number(retryMs);
        const boundedRetryMs = (
          Number.isFinite(requestedRetryMs)
          && requestedRetryMs >= SSE_MIN_RETRY_MS
          && requestedRetryMs <= SSE_MAX_RETRY_MS
        ) ? requestedRetryMs : 3_000;
        await retryDelay(
          boundedRetryMs,
          controller.signal,
        );
      }
    }
  };
  void run();
  return {
    close() {
      controller.abort();
    },
  };
}
