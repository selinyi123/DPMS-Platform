const BASE = import.meta.env.VITE_API_BASE || '/api';
const DEFAULT_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS || 15000);

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
  if (/^https?:\/\//i.test(path)) return path;
  const normalizedBase = BASE.endsWith('/') ? BASE.slice(0, -1) : BASE;
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

async function parseResponse(res) {
  const text = await res.text();
  const contentType = res.headers.get('content-type') || '';
  let data = null;

  if (text) {
    if (contentType.includes('application/json')) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        throw new Error(`Invalid JSON response from API: ${err.message}`);
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
    const detail = data?.detail || data?.message || data?.error || res.statusText || `HTTP ${res.status}`;
    const message = typeof detail === 'string' ? detail : JSON.stringify(detail);
    throw new Error(`${res.status}: ${message}`);
  }

  return data;
}

async function requestJSON(path, options = {}) {
  const controller = new AbortController();
  const timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(apiPath(path), {
      ...options.fetchOptions,
      method: options.method || 'GET',
      headers: options.headers,
      body: options.body,
      signal: controller.signal,
    });
    return await parseResponse(res);
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error(`API request timed out after ${timeoutMs}ms: ${path}`);
    }
    if (err instanceof TypeError) {
      throw new Error(`API network error for ${path}: ${err.message}`);
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

export async function fetchJSON(path, options = {}) {
  return requestJSON(path, {
    headers: options.auth ? adminHeaders() : undefined,
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
    timeoutMs: options.timeoutMs,
  });
}

export async function deleteJSON(path, options = {}) {
  return requestJSON(path, {
    method: 'DELETE',
    headers: options.confirm ? confirmedHeaders() : adminHeaders(),
    timeoutMs: options.timeoutMs,
  });
}

export function getAdminHeaders(extra = {}) {
  return adminHeaders(extra);
}

export function getConfirmedHeaders(extra = {}) {
  return confirmedHeaders(extra);
}
