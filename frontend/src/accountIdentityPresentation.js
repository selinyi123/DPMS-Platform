import { parseCalibrationResult } from './accountCalibration.js';

const IDENTITY_ID_FIELDS = {
  bilibili: ['mid', 'uid', 'user_id'],
  xiaohongshu: ['user_id', 'uid'],
  weibo: ['uid', 'user_id'],
  douyin: ['uid', 'user_id', 'sec_uid'],
};

const NICKNAME_FIELDS = ['nickname', 'uname', 'display_name', 'screen_name', 'user_name', 'name'];
const TITLE_FIELDS = ['title', 'verified_title', 'certification_title', 'official_title', 'account_title'];

function boundedText(value, maxLength = 160) {
  if (typeof value === 'number' && Number.isFinite(value)) value = String(value);
  if (typeof value !== 'string') return '';
  const normalized = value.trim();
  if (!normalized || normalized.length > maxLength || /[\u0000-\u001f\u007f]/u.test(normalized)) return '';
  return normalized;
}

function firstText(source, keys, maxLength) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return '';
  for (const key of keys) {
    const value = boundedText(source[key], maxLength);
    if (value) return value;
  }
  return '';
}

function publicTitle(identity) {
  return firstText(identity, TITLE_FIELDS, 160)
    || firstText(identity?.official, ['title', 'role'], 160)
    || firstText(identity?.certification, ['title', 'label'], 160);
}

/**
 * Present only public, authoritative platform identity evidence.
 *
 * Account IDs, credential kinds, browser fingerprints, and credential-derived
 * hashes are deliberately excluded: none of them proves which remote account
 * is logged in.
 */
export function accountIdentityPresentation(account) {
  const calibration = account?.latest_calibration;
  if (!calibration) return { state: 'not_checked', verified: false };
  if (calibration.status !== 'succeeded') {
    return { state: 'calibration_incomplete', verified: false };
  }

  const result = parseCalibrationResult(calibration.result);
  const identity = result.identity;
  if (!identity || typeof identity !== 'object' || Array.isArray(identity) || identity.verified !== true) {
    return { state: 'unverified', verified: false };
  }

  const platform = boundedText(account?.platform, 32).toLowerCase();
  const uid = firstText(identity, IDENTITY_ID_FIELDS[platform] || [], 128);
  const nickname = firstText(identity, NICKNAME_FIELDS, 160)
    || firstText(identity?.profile, NICKNAME_FIELDS, 160);
  const title = publicTitle(identity);
  const rawLevel = identity.level;
  const level = Number.isInteger(rawLevel) && rawLevel >= 0 && rawLevel <= 999
    ? String(rawLevel)
    : boundedText(rawLevel, 16);

  return {
    state: uid || nickname || title || level ? 'verified' : 'verified_without_public_profile',
    verified: true,
    uid,
    nickname,
    title,
    level,
  };
}
