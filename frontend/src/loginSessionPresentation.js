export const TERMINAL_LOGIN_STATUSES = Object.freeze([
  'confirmed',
  'expired',
  'failed',
]);
export const LOGIN_SESSION_POLL_RETRY_MS = 5_000;

export function isTerminalLoginStatus(status) {
  return TERMINAL_LOGIN_STATUSES.includes(String(status || '').trim().toLowerCase());
}

export function isActiveLoginSession(session) {
  return Boolean(session?.session_id) && !isTerminalLoginStatus(session?.status);
}

export function loginSessionPollRetryDelay(error) {
  return error?.retryable === true ? LOGIN_SESSION_POLL_RETRY_MS : null;
}

export function browserLoginImagePath(session, revision = 0) {
  if (!session?.session_id || session?.login_mode === 'official_qr') return '';
  const safeRevision = Number.isSafeInteger(revision) && revision >= 0 ? revision : 0;
  return `/accounts/login/qr/${encodeURIComponent(session.session_id)}/image?revision=${safeRevision}`;
}
