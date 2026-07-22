export function parseCalibrationResult(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export function calibrationNeedsIdentityReview(calibration) {
  if (!calibration || calibration.status !== 'succeeded') return false;
  const result = parseCalibrationResult(calibration.result);
  return result.requires_manual_identity_review === true
    || result.calibration_scope === 'session_only'
    || result.identity?.verified === false;
}

const WEIBO_ACTIONS = ['followed', 'liked', 'commented', 'favorited', 'reposted'];
const WEIBO_CAPABILITY_ENDPOINTS = {
  followed: ['friendships/create', 'advanced'],
  liked: ['attitudes/create', 'advanced'],
  commented: ['comments/create', 'standard'],
  favorited: ['favorites/create', 'standard'],
  reposted: ['statuses/repost', 'standard'],
};
const WEIBO_CAPABILITY_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const WEIBO_CAPABILITY_KEYS = new Set([
  'contract_version', 'calibration_id', 'account_id', 'execution_revision',
  'credential_kind', 'identity_verified', 'app_review_status', 'client_type',
  'verified_at', 'evidence_source', 'attested_by', 'attested_at', 'actions',
]);
const WEIBO_ACTION_CAPABILITY_KEYS = new Set(['endpoint', 'permission', 'granted']);

function hasExactKeys(value, expected) {
  return value
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).length === expected.size
    && Object.keys(value).every(key => expected.has(key));
}

export function isWeiboOAuthAccount(account) {
  if (String(account?.platform || '').trim().toLowerCase() !== 'weibo') return false;
  const credentialKind = String(account?.credential_kind || '').trim().toLowerCase();
  if (credentialKind === 'weibo_oauth') return true;
  if (credentialKind === 'browser_session') return false;
  const result = parseCalibrationResult(account?.latest_calibration?.result);
  return String(result.calibration_scope || '').startsWith('oauth_')
    || Boolean(result.oauth_capabilities);
}

export function weiboOAuthCapabilityPresentation(account, now = new Date()) {
  if (String(account?.platform || '').trim().toLowerCase() !== 'weibo') return null;
  const calibration = account?.latest_calibration;
  const result = parseCalibrationResult(calibration?.result);
  if (!isWeiboOAuthAccount(account)) return null;
  const value = result.oauth_capabilities;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {
      present: false,
      fresh: false,
      approved: false,
      identityVerified: false,
      clientType: '',
      verifiedAt: '',
      attestedAt: '',
      attestedBy: '',
      grantedActions: [],
      deniedActions: [...WEIBO_ACTIONS],
      blockers: ['weibo_oauth_capability_evidence_required'],
    };
  }

  const blockers = [];
  if (calibration?.status !== 'succeeded') blockers.push('weibo_oauth_capability_evidence_required');
  if (
    !hasExactKeys(value, WEIBO_CAPABILITY_KEYS)
    || value.contract_version !== 1
    || !UUID_PATTERN.test(String(value.calibration_id || ''))
    || value.calibration_id !== String(calibration?.calibration_id || '')
    || value.credential_kind !== 'weibo_oauth'
    || !Number.isInteger(value.account_id)
    || value.account_id !== Number(account.id)
    || !Number.isInteger(value.execution_revision)
    || value.execution_revision !== Number(account.execution_revision)
    || value.evidence_source !== 'operator_attested_app_capabilities'
    || !['approved', 'test_only', 'unknown'].includes(value.app_review_status)
    || !['weibo', 'other'].includes(value.client_type)
    || typeof value.attested_by !== 'string'
    || !value.attested_by.trim()
    || value.attested_by !== value.attested_by.trim()
    || new TextEncoder().encode(value.attested_by).length > 128
  ) blockers.push('weibo_oauth_capability_contract_mismatch');
  const identity = result.identity;
  if (
    value.identity_verified !== true
    || !identity
    || identity.verified !== true
    || identity.method !== 'weibo_account_get_uid'
  ) blockers.push('weibo_oauth_identity_verification_required');
  if (value.app_review_status !== 'approved') blockers.push('weibo_oauth_app_review_required');

  const verifiedTime = Date.parse(String(value.verified_at || ''));
  const attestedTime = Date.parse(String(value.attested_at || ''));
  const nowTime = now instanceof Date ? now.getTime() : Date.parse(String(now || ''));
  const fresh = Number.isFinite(verifiedTime)
    && Number.isFinite(attestedTime)
    && Number.isFinite(nowTime)
    && verifiedTime <= nowTime
    && attestedTime <= nowTime
    && attestedTime <= verifiedTime
    && nowTime - verifiedTime <= WEIBO_CAPABILITY_MAX_AGE_MS
    && nowTime - attestedTime <= WEIBO_CAPABILITY_MAX_AGE_MS;
  if (!fresh) blockers.push('weibo_oauth_capability_evidence_stale');

  const declaredActions = value.actions;
  const actionsValid = declaredActions
    && typeof declaredActions === 'object'
    && !Array.isArray(declaredActions)
    && Object.keys(declaredActions).length === WEIBO_ACTIONS.length
    && WEIBO_ACTIONS.every(action => {
      const declared = declaredActions[action];
      const [endpoint, permission] = WEIBO_CAPABILITY_ENDPOINTS[action];
      return declared
        && hasExactKeys(declared, WEIBO_ACTION_CAPABILITY_KEYS)
        && declared.endpoint === endpoint
        && declared.permission === permission
        && typeof declared.granted === 'boolean';
    });
  if (!actionsValid) blockers.push('weibo_oauth_capability_contract_mismatch');
  const grantedActions = WEIBO_ACTIONS.filter(action => (
    actionsValid && declaredActions[action]?.granted === true
  ));
  const deniedActions = WEIBO_ACTIONS.filter(action => !grantedActions.includes(action));
  if (grantedActions.includes('followed') && value.client_type !== 'weibo') {
    blockers.push('weibo_oauth_follow_client_type_required');
  }

  return {
    present: true,
    fresh,
    approved: value.app_review_status === 'approved',
    identityVerified: value.identity_verified === true,
    clientType: ['weibo', 'other'].includes(value.client_type) ? value.client_type : '',
    verifiedAt: typeof value.verified_at === 'string' ? value.verified_at : '',
    attestedAt: typeof value.attested_at === 'string' ? value.attested_at : '',
    attestedBy: typeof value.attested_by === 'string' ? value.attested_by : '',
    grantedActions,
    deniedActions,
    blockers: [...new Set(blockers)],
  };
}
