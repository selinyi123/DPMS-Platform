import {
  boundedNormalizedTarget,
  MAX_IMPORT_DEPTH,
  MAX_IMPORT_NODES,
  MAX_IMPORT_TARGETS,
  MAX_NORMALIZED_CONTENT_LENGTH,
  TargetImportError,
  directStringValues,
  isSensitiveKey,
  normalizedKey,
  parseSecureHttpsTarget,
  parseStructuredExport,
} from '../importShared.js';
import { DOUYIN_IMPORT_MAX_BYTES } from '../catalog.js';

export { DOUYIN_IMPORT_MAX_BYTES };

const AWEME_ID_PATTERN = /^\d{8,32}$/;
const NOTE_ID_PATTERN = /^\d{19}$/;
const URL_PATTERN = /https:\/\/(?:www\.)?(?:douyin\.com|iesdouyin\.com|v\.douyin\.com)\/[^\s"'<>]+/gi;
const URL_DETECT_PATTERN = /https:\/\/(?:www\.)?(?:douyin\.com|iesdouyin\.com|v\.douyin\.com)\//i;
const ID_KEYS = new Set(['awemeid', 'videoid']);
const URL_KEYS = new Set(['awemeurl', 'shareurl', 'videourl', 'weburl']);
const RECORD_COLLECTION_KEYS = new Set(['awemelist', 'contents', 'items', 'records', 'results']);
const CONTENT_CONTEXT_KEYS = new Set([
  'awemetype',
  'caption',
  'createtime',
  'desc',
  'images',
  'statistics',
  'title',
  'video',
]);
const IGNORED_SUBTREE_KEYS = new Set([
  'ads',
  'author',
  'commentlist',
  'commentinfo',
  'comments',
  'creator',
  'recommend',
  'recommendations',
  'recommendlist',
  'related',
  'replies',
  'replylist',
  'user',
]);

function recordHasContext(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return false;
  const keys = new Set(Object.keys(record).map(normalizedKey));
  return !keys.has('commentid')
    && [...ID_KEYS].some(key => keys.has(key))
    && [...CONTENT_CONTEXT_KEYS].some(key => keys.has(key));
}

function declaredContentRoute(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return null;
  for (const [key, value] of Object.entries(record)) {
    if (normalizedKey(key) === 'awemetype') return Number(value) === 68 ? 'note' : 'video';
  }
  return null;
}

function contentRoute(record) {
  return declaredContentRoute(record) || 'video';
}

function urlFromId(value, route = 'video') {
  const awemeId = String(value || '').trim();
  if (!AWEME_ID_PATTERN.test(awemeId)) return null;
  const normalizedRoute = route === 'note' ? 'note' : 'video';
  if (normalizedRoute === 'note' && !NOTE_ID_PATTERN.test(awemeId)) return null;
  return `https://www.douyin.com/${normalizedRoute}/${awemeId}`;
}

export function normalizeDouyinUrl(value) {
  const parsed = parseSecureHttpsTarget(value);
  if (!parsed) return null;
  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (host === 'douyin.com' || host === 'www.douyin.com') {
    if (parts.length !== 2 || !['note', 'video'].includes(parts[0])) return null;
    if (!AWEME_ID_PATTERN.test(parts[1])) return null;
    if (parts[0] === 'note' && !NOTE_ID_PATTERN.test(parts[1])) return null;
    return `https://www.douyin.com/${parts[0]}/${parts[1]}`;
  }
  if (host === 'iesdouyin.com' || host === 'www.iesdouyin.com') {
    if (parts.length !== 3 || parts[0] !== 'share' || parts[1] !== 'video') return null;
    return urlFromId(parts[2]);
  }
  if (host === 'v.douyin.com' && parts.length) {
    const normalized = `https://v.douyin.com/${parts.join('/')}`;
    return boundedNormalizedTarget(normalized);
  }
  return null;
}

function canonicalTargetIdentity(target) {
  const match = /^https:\/\/www\.douyin\.com\/(note|video)\/(\d{8,32})$/.exec(String(target || ''));
  return match ? { route: match[1], id: match[2] } : null;
}

function assertTargetConsistency(target, directIds, declaredRoute = null) {
  if (!target) return;
  const identity = canonicalTargetIdentity(target);
  const ids = directIds instanceof Set ? directIds : new Set(directIds || []);
  if (
    ids.size > 1
    || ((ids.size || declaredRoute) && !identity)
    || (identity && ids.size === 1 && !ids.has(identity.id))
    || (identity && declaredRoute && identity.route !== declaredRoute)
  ) throw new TargetImportError('douyin_import_conflicting_target');
}

function* urlCandidates(value) {
  const text = String(value || '').trim();
  if (!text) return;
  yield text;
  for (const match of text.matchAll(URL_PATTERN)) yield match[0];
}

function explicitRecordTargets(record) {
  const targets = new Set();
  if (!record || typeof record !== 'object' || Array.isArray(record)) return targets;
  for (const [key, value] of Object.entries(record)) {
    if (!URL_KEYS.has(normalizedKey(key)) || isSensitiveKey(key)) continue;
    for (const directValue of directStringValues(value, 0, 'douyin_import_too_complex')) {
      for (const candidate of urlCandidates(directValue)) {
        const target = normalizeDouyinUrl(candidate);
        if (target) targets.add(target);
      }
    }
  }
  return targets;
}

function providerCsvDescriptor(headerKeys) {
  const urlIndex = headerKeys.findIndex(key => URL_KEYS.has(key));
  const contentIdIndex = headerKeys.findIndex(key => ID_KEYS.has(key));
  const contentTypeIndex = headerKeys.findIndex(key => key === 'awemetype');
  return urlIndex >= 0 || contentIdIndex >= 0
    ? { urlIndex, contentIdIndex, contentTypeIndex }
    : null;
}

function normalizeProviderCsvRow(fields, descriptor) {
  const rawId = descriptor.contentIdIndex >= 0
    ? String(fields[descriptor.contentIdIndex] || '').trim()
    : '';
  const directIds = AWEME_ID_PATTERN.test(rawId) ? new Set([rawId]) : new Set();
  const hasDeclaredType = descriptor.contentTypeIndex >= 0
    && String(fields[descriptor.contentTypeIndex] || '').trim() !== '';
  const route = hasDeclaredType
    ? (Number(fields[descriptor.contentTypeIndex]) === 68 ? 'note' : 'video')
    : null;
  const urlTarget = normalizeDouyinUrl(
    descriptor.urlIndex >= 0 ? fields[descriptor.urlIndex] : '',
  );
  assertTargetConsistency(urlTarget, directIds, route);
  return urlTarget || urlFromId(rawId, route || 'video');
}

function normalizeStructuredImport(source) {
  const payload = parseStructuredExport(source, 'douyin_import_invalid_json');
  const targets = new Set();
  let discardedSensitiveFields = 0;
  let discardedRows = 0;
  let visitedNodes = 0;

  const addTarget = target => {
    if (!target) return false;
    if (targets.has(target)) {
      discardedRows += 1;
      return false;
    }
    targets.add(target);
    if (targets.size > MAX_IMPORT_TARGETS) {
      throw new TargetImportError('douyin_import_too_many_targets');
    }
    return true;
  };

  const visit = (value, key = '', parent = null, depth = 0, genericUrlAllowed = false) => {
    visitedNodes += 1;
    if (depth > MAX_IMPORT_DEPTH || visitedNodes > MAX_IMPORT_NODES) {
      throw new TargetImportError('douyin_import_too_complex');
    }
    if (typeof value === 'string') {
      const keyName = normalizedKey(key);
      if (ID_KEYS.has(keyName) && recordHasContext(parent)) {
        const explicitTargets = explicitRecordTargets(parent);
        if (!explicitTargets.size) {
          const target = urlFromId(value, contentRoute(parent));
          if (target) addTarget(target);
          else discardedRows += 1;
        }
      }
      if (!keyName) {
        const target = normalizeDouyinUrl(value);
        if (target) addTarget(target);
        else discardedRows += 1;
      } else if (URL_KEYS.has(keyName) && genericUrlAllowed) {
        let recognized = false;
        for (const candidate of urlCandidates(value)) {
          const target = normalizeDouyinUrl(candidate);
          if (!target) continue;
          recognized = true;
          addTarget(target);
        }
        if (!recognized) discardedRows += 1;
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) visit(item, key, value, depth + 1, genericUrlAllowed);
      return;
    }
    if (!value || typeof value !== 'object') return;

    const keyName = normalizedKey(key);
    const objectAllowsGenericUrl = recordHasContext(value)
      || RECORD_COLLECTION_KEYS.has(keyName)
      || (depth === 1 && parent === payload && Array.isArray(parent));
    if (recordHasContext(value)) {
      const directTargets = explicitRecordTargets(value);
      const directIds = new Set();
      for (const [childKey, childValue] of Object.entries(value)) {
        if (isSensitiveKey(childKey)) continue;
        const childKeyName = normalizedKey(childKey);
        for (const directValue of directStringValues(childValue, 0, 'douyin_import_too_complex')) {
          if (ID_KEYS.has(childKeyName) && AWEME_ID_PATTERN.test(directValue)) {
            directIds.add(directValue);
          }
        }
      }
      const [explicitTarget] = directTargets;
      if (directTargets.size > 1 || directIds.size > 1) {
        throw new TargetImportError('douyin_import_conflicting_target');
      }
      assertTargetConsistency(explicitTarget, directIds, declaredContentRoute(value));
    }
    for (const [childKey, childValue] of Object.entries(value)) {
      if (isSensitiveKey(childKey)) {
        discardedSensitiveFields += 1;
        continue;
      }
      if (IGNORED_SUBTREE_KEYS.has(normalizedKey(childKey))) continue;
      visit(childValue, childKey, value, depth + 1, objectAllowsGenericUrl);
    }
  };

  visit(payload);
  if (!targets.size) throw new TargetImportError('douyin_import_no_targets');
  const normalizedContent = [...targets].join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new TargetImportError('douyin_import_output_too_large');
  }
  const shortLinkCount = [...targets].filter(target => target.startsWith('https://v.douyin.com/')).length;
  const shortLinkBlocked = shortLinkCount > 1;
  return {
    content: normalizedContent,
    converted: true,
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount: shortLinkBlocked ? 0 : shortLinkCount,
    shortLinkCountsByPlatform: Object.freeze(
      !shortLinkBlocked && shortLinkCount ? { douyin: shortLinkCount } : {},
    ),
    shortLinkErrorsByPlatform: Object.freeze(
      shortLinkBlocked
        ? { douyin: 'douyin_import_short_link_batch_unsupported' }
        : {},
    ),
    blockedShortLinkCount: shortLinkBlocked ? shortLinkCount : 0,
    targetCount: targets.size,
  };
}

export const douyinImportPolicy = Object.freeze({
  id: 'douyin',
  maxBytes: DOUYIN_IMPORT_MAX_BYTES,
  tooLargeErrorCode: 'douyin_import_too_large',
  contentTooLargeErrorCode: 'douyin_import_content_too_large',
  delimitedErrorPrefix: 'douyin',
  requiresPlatformErrorCode: 'target_import_douyin_requires_platform',
  structuredDetectionPriority: 10,
  compatibilityAllowedPlatformIds: Object.freeze(['xiaohongshu']),
  shortLinkHosts: Object.freeze(['v.douyin.com']),
  shortLinkLimit: 1,
  shortLinkErrorCode: 'douyin_import_short_link_batch_unsupported',
  structuredTargetImport: true,
  normalizeUrl: normalizeDouyinUrl,
  normalizeStructuredImport,
  normalizeProviderCsvRow,
  providerCsvDescriptor,
  looksLikeStructuredExport(source) {
    return URL_DETECT_PATTERN.test(source)
      || /["'](?:aweme[_-]?id|aweme[_-]?list|video[_-]?id)["']\s*:/i.test(source);
  },
  looksLikeDelimitedExport() {
    return false;
  },
});
