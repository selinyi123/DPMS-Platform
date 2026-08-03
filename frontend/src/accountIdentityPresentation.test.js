import assert from 'node:assert/strict';
import test from 'node:test';

import { accountIdentityPresentation } from './accountIdentityPresentation.js';

function account(platform, identity, overrides = {}) {
  return {
    id: 7,
    platform,
    fingerprint_id: 93,
    credential_kind: 'browser_session',
    latest_calibration: {
      status: 'succeeded',
      result: { identity },
    },
    ...overrides,
  };
}

test('presents only verified public Bilibili identity fields', () => {
  assert.deepEqual(accountIdentityPresentation(account('bilibili', {
    verified: true,
    mid: '123456',
    uname: '示例昵称',
    level: 6,
    title: '官方认证',
  })), {
    state: 'verified',
    verified: true,
    uid: '123456',
    nickname: '示例昵称',
    title: '官方认证',
    level: '6',
  });
});

test('supports the currently persisted Xiaohongshu and Weibo identity IDs', () => {
  assert.equal(accountIdentityPresentation(account('xiaohongshu', {
    verified: true,
    user_id: 'xhs-user-1',
  })).uid, 'xhs-user-1');
  assert.equal(accountIdentityPresentation(account('weibo', {
    verified: true,
    uid: '9876543210',
  })).uid, '9876543210');
});

test('never substitutes a fingerprint, credential hash, or internal account id for platform identity', () => {
  const presented = accountIdentityPresentation(account('bilibili', {
    verified: true,
    credential_fingerprint: 'must-not-render',
    remote_subject: 'must-not-render',
  }));
  assert.deepEqual(presented, {
    state: 'verified_without_public_profile',
    verified: true,
    uid: '',
    nickname: '',
    title: '',
    level: '',
  });
  assert.doesNotMatch(JSON.stringify(presented), /must-not-render|93|7/);
});

test('does not display identity claims from incomplete or unverified calibration', () => {
  assert.deepEqual(accountIdentityPresentation(account('bilibili', {
    verified: false,
    mid: '123456',
  })), { state: 'unverified', verified: false });
  assert.deepEqual(accountIdentityPresentation(account('bilibili', {
    verified: true,
    mid: '123456',
  }, {
    latest_calibration: {
      status: 'running',
      result: { identity: { verified: true, mid: '123456' } },
    },
  })), { state: 'calibration_incomplete', verified: false });
});
