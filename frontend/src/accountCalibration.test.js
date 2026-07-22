import assert from 'node:assert/strict';
import test from 'node:test';

import {
  calibrationNeedsIdentityReview,
  isWeiboOAuthAccount,
  parseCalibrationResult,
  weiboOAuthCapabilityPresentation,
} from './accountCalibration.js';

test('parses nested calibration JSON without trusting malformed values', () => {
  assert.deepEqual(parseCalibrationResult('{"identity":{"verified":false}}'), {
    identity: { verified: false },
  });
  assert.deepEqual(parseCalibrationResult('{broken'), {});
  assert.deepEqual(parseCalibrationResult(['not', 'an', 'object']), {});
});

test('flags succeeded session-only calibration for manual identity review', () => {
  assert.equal(calibrationNeedsIdentityReview({
    status: 'succeeded',
    result: JSON.stringify({ calibration_scope: 'session_only' }),
  }), true);
  assert.equal(calibrationNeedsIdentityReview({
    status: 'succeeded',
    result: { identity: { verified: true } },
  }), false);
  assert.equal(calibrationNeedsIdentityReview({
    status: 'failed',
    result: { identity: { verified: false } },
  }), false);
});

function weiboAccount(overrides = {}) {
  const actions = Object.fromEntries([
    ['followed', 'friendships/create', 'advanced'],
    ['liked', 'attitudes/create', 'advanced'],
    ['commented', 'comments/create', 'standard'],
    ['favorited', 'favorites/create', 'standard'],
    ['reposted', 'statuses/repost', 'standard'],
  ].map(([action, endpoint, permission]) => [
    action,
    { endpoint, permission, granted: true },
  ]));
  return {
    id: 7,
    platform: 'weibo',
    credential_kind: 'weibo_oauth',
    execution_revision: 3,
    latest_calibration: {
      calibration_id: '123e4567-e89b-42d3-a456-426614174000',
      status: 'succeeded',
      result: {
        identity: {
          verified: true,
          method: 'weibo_account_get_uid',
          uid: '1234567890',
        },
        calibration_scope: 'oauth_identity_and_capabilities',
        oauth_capabilities: {
          contract_version: 1,
          calibration_id: '123e4567-e89b-42d3-a456-426614174000',
          account_id: 7,
          execution_revision: 3,
          credential_kind: 'weibo_oauth',
          identity_verified: true,
          app_review_status: 'approved',
          client_type: 'weibo',
          verified_at: '2026-07-22T02:00:00Z',
          evidence_source: 'operator_attested_app_capabilities',
          attested_by: 'admin-1',
          attested_at: '2026-07-22T01:55:00Z',
          actions,
        },
      },
    },
    ...overrides,
  };
}

test('presents only non-secret fresh Weibo OAuth capability evidence', () => {
  const presentation = weiboOAuthCapabilityPresentation(
    weiboAccount(),
    new Date('2026-07-22T03:00:00Z'),
  );
  assert.deepEqual(presentation.blockers, []);
  assert.deepEqual(presentation.grantedActions, [
    'followed', 'liked', 'commented', 'favorited', 'reposted',
  ]);
  assert.equal(presentation.fresh, true);
  assert.equal(presentation.attestedBy, 'admin-1');
  assert.equal('access_token' in presentation, false);
});

test('fails closed for missing, stale, test-only, or account-mismatched OAuth evidence', () => {
  const identityOnly = weiboAccount();
  identityOnly.latest_calibration.result = {
    identity: { verified: true, method: 'weibo_account_get_uid', uid: '1234567890' },
    calibration_scope: 'oauth_identity_only',
  };
  assert.deepEqual(
    weiboOAuthCapabilityPresentation(identityOnly).blockers,
    ['weibo_oauth_capability_evidence_required'],
  );
  const staleTestOnly = weiboAccount();
  staleTestOnly.latest_calibration.result.oauth_capabilities.app_review_status = 'test_only';
  staleTestOnly.latest_calibration.result.oauth_capabilities.account_id = 8;
  const presentation = weiboOAuthCapabilityPresentation(
    staleTestOnly,
    new Date('2026-07-24T03:00:00Z'),
  );
  assert.ok(presentation.blockers.includes('weibo_oauth_capability_contract_mismatch'));
  assert.ok(presentation.blockers.includes('weibo_oauth_app_review_required'));
  assert.ok(presentation.blockers.includes('weibo_oauth_capability_evidence_stale'));
});

test('keeps legacy Weibo browser-session accounts outside the OAuth capability gate', () => {
  const browserAccount = {
    platform: 'weibo',
    credential_kind: 'browser_session',
    latest_calibration: {
      status: 'succeeded',
      result: { calibration_scope: 'identity_and_session', identity: { verified: true } },
    },
  };
  assert.equal(isWeiboOAuthAccount(browserAccount), false);
  assert.equal(weiboOAuthCapabilityPresentation(browserAccount), null);

  const olderResponse = { ...browserAccount };
  delete olderResponse.credential_kind;
  assert.equal(isWeiboOAuthAccount(olderResponse), false);
  assert.equal(weiboOAuthCapabilityPresentation(olderResponse), null);
});

test('rejects secret or undeclared fields in presented capability evidence', () => {
  const tampered = weiboAccount();
  tampered.latest_calibration.result.oauth_capabilities.access_token = 'must-not-render';
  const presentation = weiboOAuthCapabilityPresentation(
    tampered,
    new Date('2026-07-22T03:00:00Z'),
  );
  assert.ok(presentation.blockers.includes('weibo_oauth_capability_contract_mismatch'));
  assert.equal(JSON.stringify(presentation).includes('must-not-render'), false);
});
