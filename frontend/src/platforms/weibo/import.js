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
import { WEIBO_IMPORT_MAX_BYTES } from '../catalog.js';

export { WEIBO_IMPORT_MAX_BYTES };

const MID_PATTERN = /^[1-9][0-9]{0,18}$/;
const MAX_STATUS_ID = 9223372036854775807n;
const MBLOGID_PATTERN = /^(?=.*[A-Za-z])[A-Za-z0-9]{6,16}$/;
const URL_PATTERN = /https:\/\/(?:www\.)?(?:weibo\.com|m\.weibo\.cn|t\.cn)\/[^\s"'<>]+/gi;
const URL_DETECT_PATTERN = /https:\/\/(?:www\.)?(?:weibo\.com|m\.weibo\.cn|t\.cn)\//i;
const ID_KEYS = new Set(['idstr', 'mblogid', 'mid']);
const URL_KEYS = new Set(['longurl', 'shareurl', 'statusurl', 'url', 'weburl', 'weibourl']);
const RECORD_COLLECTION_KEYS = new Set(['cards', 'items', 'mblogs', 'records', 'results', 'statuses']);
const CONTENT_CONTEXT_KEYS = new Set([
  'attitudescount',
  'commentscount',
  'createdat',
  'picids',
  'repostscount',
  'text',
  'textraw',
  'user',
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
  'retweetedstatus',
  'user',
]);

function isStatusId(value) {
  const statusId = String(value || '').trim();
  const numericMid = MID_PATTERN.test(statusId) && BigInt(statusId) <= MAX_STATUS_ID;
  return numericMid || MBLOGID_PATTERN.test(statusId);
}

function urlFromId(value) {
  const statusId = String(value || '').trim();
  return isStatusId(statusId) ? `https://weibo.com/detail/${statusId}` : null;
}

export function normalizeWeiboUrl(value) {
  const parsed = parseSecureHttpsTarget(value);
  if (!parsed) return null;
  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (host === 'weibo.com' || host === 'www.weibo.com') {
    if (parts.length !== 2) return null;
    if (parts[0] === 'detail') return urlFromId(parts[1]);
    if (!/^\d+$/.test(parts[0]) || !isStatusId(parts[1])) return null;
    return `https://weibo.com/${parts[0]}/${parts[1]}`;
  }
  if (host === 'm.weibo.cn') {
    if (parts.length !== 2 || !['detail', 'status'].includes(parts[0])) return null;
    return urlFromId(parts[1]) ? `https://m.weibo.cn/detail/${parts[1]}` : null;
  }
  if (host === 't.cn' && parts.length === 1) {
    const normalized = `https://t.cn/${parts[0]}`;
    return boundedNormalizedTarget(normalized);
  }
  return null;
}

function recordHasContext(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return false;
  const keys = new Set(Object.keys(record).map(normalizedKey));
  return !keys.has('commentid')
    && ([...ID_KEYS].some(key => keys.has(key)) || [...URL_KEYS].some(key => keys.has(key)))
    && [...CONTENT_CONTEXT_KEYS].some(key => keys.has(key));
}

function canonicalTargetId(target) {
  const match = /^https:\/\/(?:weibo\.com\/(?:detail|\d+)|m\.weibo\.cn\/detail)\/([A-Za-z0-9]+)$/.exec(
    String(target || ''),
  );
  return match && isStatusId(match[1]) ? match[1] : null;
}

function assertTargetConsistency(target, directIds) {
  if (!target) return;
  const ids = directIds instanceof Set ? directIds : new Set(directIds || []);
  const targetId = canonicalTargetId(target);
  if (ids.size && (!targetId || !ids.has(targetId))) {
    throw new TargetImportError('weibo_import_conflicting_target');
  }
}

function directRecordIds(record) {
  const byKey = new Map();
  const all = new Set();
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    return { all, preferred: null, ambiguous: false };
  }
  for (const [key, value] of Object.entries(record)) {
    const keyName = normalizedKey(key);
    if (!ID_KEYS.has(keyName) || isSensitiveKey(key)) continue;
    for (const candidate of directStringValues(value, 0, 'weibo_import_too_complex')) {
      if (!isStatusId(candidate)) continue;
      if (!byKey.has(keyName)) byKey.set(keyName, new Set());
      byKey.get(keyName).add(candidate);
      all.add(candidate);
    }
  }
  const first = key => [...(byKey.get(key) || [])][0] || null;
  return {
    all,
    preferred: first('mblogid') || first('mid') || first('idstr'),
    ambiguous: [...byKey.values()].some(values => values.size > 1),
  };
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
    for (const directValue of directStringValues(value, 0, 'weibo_import_too_complex')) {
      for (const candidate of urlCandidates(directValue)) {
        const target = normalizeWeiboUrl(candidate);
        if (target) targets.add(target);
      }
    }
  }
  return targets;
}

function providerCsvDescriptor(headerKeys) {
  const urlIndex = headerKeys.findIndex(key => URL_KEYS.has(key));
  const contentIdIndex = ['mblogid', 'mid', 'idstr']
    .map(key => headerKeys.indexOf(key))
    .find(index => index >= 0) ?? -1;
  return urlIndex >= 0 || contentIdIndex >= 0 ? { urlIndex, contentIdIndex } : null;
}

function normalizeProviderCsvRow(fields, descriptor) {
  const rawId = descriptor.contentIdIndex >= 0
    ? String(fields[descriptor.contentIdIndex] || '').trim()
    : '';
  const directIds = isStatusId(rawId) ? new Set([rawId]) : new Set();
  const urlTarget = normalizeWeiboUrl(descriptor.urlIndex >= 0 ? fields[descriptor.urlIndex] : '');
  assertTargetConsistency(urlTarget, directIds);
  return urlTarget || urlFromId(rawId);
}

function normalizeStructuredImport(source) {
  const payload = parseStructuredExport(source, 'weibo_import_invalid_json');
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
      throw new TargetImportError('weibo_import_too_many_targets');
    }
    return true;
  };

  const visit = (value, key = '', parent = null, depth = 0, genericUrlAllowed = false) => {
    visitedNodes += 1;
    if (depth > MAX_IMPORT_DEPTH || visitedNodes > MAX_IMPORT_NODES) {
      throw new TargetImportError('weibo_import_too_complex');
    }
    if (typeof value === 'string') {
      const keyName = normalizedKey(key);
      if (ID_KEYS.has(keyName) && recordHasContext(parent)) {
        const explicitTargets = explicitRecordTargets(parent);
        const ids = directRecordIds(parent);
        if (!explicitTargets.size && value === ids.preferred) {
          const target = urlFromId(value);
          if (target) addTarget(target);
          else discardedRows += 1;
        }
      }
      if (!keyName) {
        const target = normalizeWeiboUrl(value);
        if (target) addTarget(target);
        else discardedRows += 1;
      } else if (URL_KEYS.has(keyName) && genericUrlAllowed) {
        let recognized = false;
        for (const candidate of urlCandidates(value)) {
          const target = normalizeWeiboUrl(candidate);
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
      const ids = directRecordIds(value);
      if (directTargets.size > 1 || ids.ambiguous) {
        throw new TargetImportError('weibo_import_conflicting_target');
      }
      const [explicitTarget] = directTargets;
      assertTargetConsistency(explicitTarget, ids.all);
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
  if (!targets.size) throw new TargetImportError('weibo_import_no_targets');
  const normalizedContent = [...targets].join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new TargetImportError('weibo_import_output_too_large');
  }
  const shortLinkCount = [...targets].filter(target => target.startsWith('https://t.cn/')).length;
  const shortLinkBlocked = shortLinkCount > 1;
  return {
    content: normalizedContent,
    converted: true,
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount: shortLinkBlocked ? 0 : shortLinkCount,
    shortLinkCountsByPlatform: Object.freeze(
      !shortLinkBlocked && shortLinkCount ? { weibo: shortLinkCount } : {},
    ),
    shortLinkErrorsByPlatform: Object.freeze(
      shortLinkBlocked
        ? { weibo: 'weibo_import_short_link_batch_unsupported' }
        : {},
    ),
    blockedShortLinkCount: shortLinkBlocked ? shortLinkCount : 0,
    targetCount: targets.size,
  };
}

export const weiboImportPolicy = Object.freeze({
  id: 'weibo',
  maxBytes: WEIBO_IMPORT_MAX_BYTES,
  tooLargeErrorCode: 'weibo_import_too_large',
  contentTooLargeErrorCode: 'weibo_import_content_too_large',
  delimitedErrorPrefix: 'weibo',
  requiresPlatformErrorCode: 'target_import_weibo_requires_platform',
  structuredDetectionPriority: 20,
  compatibilityAllowedPlatformIds: Object.freeze(['xiaohongshu']),
  shortLinkHosts: Object.freeze(['t.cn']),
  shortLinkLimit: 1,
  shortLinkErrorCode: 'weibo_import_short_link_batch_unsupported',
  structuredTargetImport: true,
  normalizeUrl: normalizeWeiboUrl,
  normalizeStructuredImport,
  normalizeProviderCsvRow,
  providerCsvDescriptor,
  looksLikeStructuredExport(source) {
    return URL_DETECT_PATTERN.test(source)
      || /["'](?:mblog[_-]?id|reposts[_-]?count|statuses|status[_-]?url|text[_-]?raw)["']\s*:/i.test(source);
  },
  looksLikeDelimitedExport() {
    return false;
  },
});
