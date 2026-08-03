const ROOT_FIELDS = Object.freeze([
  'contract_version',
  'credential_kind',
  'device_agent',
]);

const DEVICE_AGENT_FIELDS = Object.freeze([
  'account_id_sha256',
  'agent_id',
  'device_serial_sha256',
  'manifest_sha256',
]);

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export const DOUYIN_DEVICE_CREDENTIAL_INVALID = 'douyin_device_credential_invalid';

function fail() {
  const error = new Error(DOUYIN_DEVICE_CREDENTIAL_INVALID);
  error.code = DOUYIN_DEVICE_CREDENTIAL_INVALID;
  throw error;
}

function hasExactFields(value, expected) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const fields = Object.keys(value).sort();
  return fields.length === expected.length
    && fields.every((field, index) => field === expected[index]);
}

/**
 * Validate and canonicalize the non-secret Douyin device identity envelope.
 *
 * Bearer tokens, raw ADB serials and raw account identifiers have no accepted
 * field in this closed schema. Errors deliberately expose only a stable code,
 * never any part of the submitted value.
 */
export function normalizeDouyinDeviceCredential(value) {
  if (typeof value !== 'string' || value.length > 4096) fail();

  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    fail();
  }

  if (
    !hasExactFields(parsed, ROOT_FIELDS)
    || parsed.contract_version !== 1
    || parsed.credential_kind !== 'device_agent'
    || !hasExactFields(parsed.device_agent, DEVICE_AGENT_FIELDS)
    || !DEVICE_AGENT_FIELDS.every(field => (
      typeof parsed.device_agent[field] === 'string'
      && SHA256_PATTERN.test(parsed.device_agent[field])
    ))
  ) fail();

  return JSON.stringify({
    contract_version: 1,
    credential_kind: 'device_agent',
    device_agent: {
      account_id_sha256: parsed.device_agent.account_id_sha256,
      agent_id: parsed.device_agent.agent_id,
      device_serial_sha256: parsed.device_agent.device_serial_sha256,
      manifest_sha256: parsed.device_agent.manifest_sha256,
    },
  });
}
