
const BASE = '/api';

function adminHeaders(extra = {}) {

  const token = localStorage.getItem('dpms_admin_token') || '';

  const headers = { ...extra };

  if (token) headers['x-admin-token'] = token;

  return headers;

}

function confirmedHeaders(extra = {}) {

  return adminHeaders({ ...extra, 'x-confirm-action': 'true' });

}

async function parseResponse(res) {

  const text = await res.text();

  const data = text ? JSON.parse(text) : null;

  if (!res.ok) {

    const message = data?.detail || data?.message || `HTTP ${res.status}`;

    throw new Error(message);

  }

  return data;

}

export async function fetchJSON(path, options = {}) {

  const headers = options.auth ? adminHeaders() : undefined;

  const res = await fetch(`${BASE}${path}`, headers ? { headers } : undefined);

  return parseResponse(res);

}

export async function postJSON(path, body, options = {}) {

  const res = await fetch(`${BASE}${path}`, {

    method: 'POST',

    headers: options.confirm
      ? confirmedHeaders({ 'Content-Type': 'application/json' })
      : adminHeaders({ 'Content-Type': 'application/json' }),

    body: JSON.stringify(body)

  });

  return parseResponse(res);

}

export async function putJSON(path, body, options = {}) {

  const res = await fetch(`${BASE}${path}`, {

    method: 'PUT',

    headers: options.confirm
      ? confirmedHeaders({ 'Content-Type': 'application/json' })
      : adminHeaders({ 'Content-Type': 'application/json' }),

    body: JSON.stringify(body)

  });

  return parseResponse(res);

}

export async function deleteJSON(path, options = {}) {

  const res = await fetch(`${BASE}${path}`, {

    method: 'DELETE',

    headers: options.confirm ? confirmedHeaders() : adminHeaders()

  });

  return parseResponse(res);

}

export function getAdminHeaders(extra = {}) {

  return adminHeaders(extra);

}

export function getConfirmedHeaders(extra = {}) {

  return confirmedHeaders(extra);

}
