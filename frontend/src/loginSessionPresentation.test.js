import assert from 'node:assert/strict';
import test from 'node:test';

import {
  browserLoginImagePath,
  isActiveLoginSession,
  isTerminalLoginStatus,
  loginSessionPollRetryDelay,
} from './loginSessionPresentation.js';

test('terminal login status does not keep the opening placeholder active', () => {
  for (const status of ['confirmed', 'expired', 'failed']) {
    assert.equal(isTerminalLoginStatus(status), true);
    assert.equal(isActiveLoginSession({ session_id: 'session-1', status }), false);
  }
  assert.equal(isActiveLoginSession({ session_id: 'session-1', status: 'waiting_scan' }), true);
});

test('browser image revision changes without exposing the login URL', () => {
  const session = {
    session_id: '9ef05a47-3ef6-42d5-a485-b4374a60d442',
    login_mode: 'browser',
  };
  assert.equal(
    browserLoginImagePath(session, 3),
    '/accounts/login/qr/9ef05a47-3ef6-42d5-a485-b4374a60d442/image?revision=3',
  );
  assert.notEqual(browserLoginImagePath(session, 3), browserLoginImagePath(session, 4));
  assert.equal(browserLoginImagePath({ ...session, login_mode: 'official_qr' }, 4), '');
});

test('login session polling selects retry only for structured retryable failures', () => {
  assert.equal(loginSessionPollRetryDelay({ retryable: true }), 5_000);
  assert.equal(loginSessionPollRetryDelay({ status: 401, retryable: false }), null);
});
