import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DOUYIN_DEVICE_CREDENTIAL_INVALID,
  normalizeDouyinDeviceCredential,
} from './douyinDeviceCredential.js';

const HASHES = Object.freeze({
  agent_id: 'a'.repeat(64),
  manifest_sha256: 'b'.repeat(64),
  device_serial_sha256: 'c'.repeat(64),
  account_id_sha256: 'd'.repeat(64),
});

function envelope(overrides = {}) {
  return JSON.stringify({
    contract_version: 1,
    credential_kind: 'device_agent',
    device_agent: { ...HASHES },
    ...overrides,
  });
}

test('canonicalizes the exact non-secret Douyin device credential envelope', () => {
  assert.deepEqual(JSON.parse(normalizeDouyinDeviceCredential(envelope())), {
    contract_version: 1,
    credential_kind: 'device_agent',
    device_agent: {
      account_id_sha256: 'd'.repeat(64),
      agent_id: 'a'.repeat(64),
      device_serial_sha256: 'c'.repeat(64),
      manifest_sha256: 'b'.repeat(64),
    },
  });
});

test('rejects bearer tokens and extra secret-bearing fields without echoing input', () => {
  const bearer = 'super-secret-device-agent-token';
  const invalid = envelope({ bearer_token: bearer });

  assert.throws(
    () => normalizeDouyinDeviceCredential(invalid),
    error => error?.code === DOUYIN_DEVICE_CREDENTIAL_INVALID
      && error.message === DOUYIN_DEVICE_CREDENTIAL_INVALID
      && !error.message.includes(bearer),
  );
  assert.throws(
    () => normalizeDouyinDeviceCredential(bearer),
    error => error?.code === DOUYIN_DEVICE_CREDENTIAL_INVALID
      && !error.message.includes(bearer),
  );
});

test('rejects wrong versions, non-hash identities and incomplete envelopes', () => {
  const invalidValues = [
    envelope({ contract_version: 2 }),
    envelope({ credential_kind: 'browser_session' }),
    envelope({ device_agent: { ...HASHES, agent_id: 'not-a-hash' } }),
    envelope({ device_agent: { ...HASHES, manifest_sha256: undefined } }),
  ];

  for (const value of invalidValues) {
    assert.throws(
      () => normalizeDouyinDeviceCredential(value),
      error => error?.code === DOUYIN_DEVICE_CREDENTIAL_INVALID,
    );
  }
});
