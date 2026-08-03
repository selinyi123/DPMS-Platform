import {
  boundedNormalizedTarget,
  MAX_IMPORT_DEPTH,
  MAX_IMPORT_NODES,
  MAX_IMPORT_TARGETS,
  MAX_NORMALIZED_CONTENT_LENGTH,
  TargetImportError,
  directStringValues,
  isSensitiveKey,
  isGenericUrlKey,
  normalizedKey,
  parseDelimitedRecords,
  parseSecureHttpsTarget,
  parseStructuredExport,
} from '../importShared.js';
import { XIAOHONGSHU_IMPORT_MAX_BYTES } from '../catalog.js';

export { XIAOHONGSHU_IMPORT_MAX_BYTES };

const NOTE_ID_PATTERN = /^[0-9a-f]{24}$/i;
const URL_PATTERN = /https:\/\/(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)\/[^\s"'<>]+/gi;
const URL_DETECT_PATTERN = /https:\/\/(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)\//i;
const NOTE_ID_KEYS = new Set(['noteid', 'feedid', 'sourcenoteid']);
const SPECIFIC_URL_KEYS = new Set(['noteurl', 'shareurl']);
const ENVELOPE_KEYS = new Set(['data', 'feeds', 'items', 'records', 'results']);
const TRUSTED_RECORD_COLLECTION_KEYS = new Set(['feeds', 'items', 'records', 'results']);
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

function noteRecordHasContext(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return false;
  const keys = new Set(Object.keys(record).map(normalizedKey));
  return keys.has('notecard')
    || (keys.has('xsectoken') && (keys.has('title') || keys.has('displaytitle')));
}

function noteUrlFromId(value) {
  const noteId = String(value || '').trim();
  if (!NOTE_ID_PATTERN.test(noteId)) return null;
  return `https://www.xiaohongshu.com/explore/${noteId.toLowerCase()}`;
}

export function normalizeXiaohongshuUrl(value) {
  const parsed = parseSecureHttpsTarget(value);
  if (!parsed) return null;
  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (host === 'xiaohongshu.com' || host === 'www.xiaohongshu.com') {
    const noteId = parts.length === 2 && parts[0] === 'explore'
      ? parts[1]
      : parts.length === 3 && parts[0] === 'discovery' && parts[1] === 'item'
        ? parts[2]
        : '';
    return noteUrlFromId(noteId);
  }
  if (host === 'xhslink.com' && parts.length) {
    const normalized = `https://xhslink.com/${parts.join('/')}`;
    return boundedNormalizedTarget(normalized);
  }
  return null;
}

function* urlCandidates(value) {
  const text = String(value || '').trim();
  if (!text) return;
  yield text;
  for (const match of text.matchAll(URL_PATTERN)) yield match[0];
}

function providerCsvDescriptor(headerKeys) {
  const specificUrlIndex = headerKeys.findIndex(key => SPECIFIC_URL_KEYS.has(key));
  const genericUrlIndex = headerKeys.findIndex(isGenericUrlKey);
  const urlIndex = specificUrlIndex >= 0 ? specificUrlIndex : genericUrlIndex;
  const contentIdIndex = headerKeys.findIndex(key => NOTE_ID_KEYS.has(key));
  return urlIndex >= 0 || contentIdIndex >= 0 ? { urlIndex, contentIdIndex } : null;
}

function normalizeProviderCsvRow(fields, descriptor) {
  return normalizeXiaohongshuUrl(fields[descriptor.urlIndex])
    || noteUrlFromId(fields[descriptor.contentIdIndex]);
}

function looksLikeDelimitedExport(source) {
  if (URL_DETECT_PATTERN.test(source)) return true;
  let firstFields;
  try {
    [firstFields] = parseDelimitedRecords(source, 'xiaohongshu_import_invalid_csv');
  } catch {
    firstFields = source.split(/\r?\n/, 1)[0].split(/[\t,]/);
  }
  const headerKeys = (firstFields || []).map(normalizedKey);
  return headerKeys.some(key => (
    NOTE_ID_KEYS.has(key)
    || SPECIFIC_URL_KEYS.has(key)
    || isSensitiveKey(key)
  ));
}

function normalizeStructuredImport(source) {
  const payload = parseStructuredExport(source, 'xiaohongshu_import_invalid_json');
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
      throw new TargetImportError('xiaohongshu_import_too_many_targets');
    }
    return true;
  };

  const visit = (value, key = '', parent = null, depth = 0, genericUrlAllowed = false) => {
    visitedNodes += 1;
    if (depth > MAX_IMPORT_DEPTH || visitedNodes > MAX_IMPORT_NODES) {
      throw new TargetImportError('xiaohongshu_import_too_complex');
    }
    if (typeof value === 'string') {
      const keyName = normalizedKey(key);
      if (NOTE_ID_KEYS.has(keyName) || (keyName === 'id' && noteRecordHasContext(parent))) {
        const target = noteUrlFromId(value);
        if (target) addTarget(target);
        else discardedRows += 1;
      }
      if (!keyName) {
        const target = normalizeXiaohongshuUrl(value);
        if (target) addTarget(target);
        else discardedRows += 1;
      } else if (isGenericUrlKey(keyName) && genericUrlAllowed) {
        let recognized = false;
        for (const candidate of urlCandidates(value)) {
          const target = normalizeXiaohongshuUrl(candidate);
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

    const objectKeys = new Set(Object.keys(value).map(normalizedKey));
    const rootLooksLikeRecord = depth === 0
      && ![...objectKeys].some(keyName => ENVELOPE_KEYS.has(keyName));
    const objectAllowsGenericUrl = noteRecordHasContext(value)
      || [...NOTE_ID_KEYS].some(keyName => objectKeys.has(keyName))
      || rootLooksLikeRecord
      || TRUSTED_RECORD_COLLECTION_KEYS.has(normalizedKey(key))
      || (depth === 1 && parent === payload && Array.isArray(parent));
    if (objectAllowsGenericUrl) {
      const directTargets = new Set();
      for (const [childKey, childValue] of Object.entries(value)) {
        if (isSensitiveKey(childKey)) continue;
        const childKeyName = normalizedKey(childKey);
        for (const directValue of directStringValues(childValue, 0, 'xiaohongshu_import_too_complex')) {
          if (NOTE_ID_KEYS.has(childKeyName) || (childKeyName === 'id' && noteRecordHasContext(value))) {
            const target = noteUrlFromId(directValue);
            if (target) directTargets.add(target);
          }
          if (isGenericUrlKey(childKeyName)) {
            for (const candidate of urlCandidates(directValue)) {
              const target = normalizeXiaohongshuUrl(candidate);
              if (target) directTargets.add(target);
            }
          }
        }
      }
      if (directTargets.size > 1) {
        throw new TargetImportError('xiaohongshu_import_conflicting_target');
      }
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
  if (!targets.size) throw new TargetImportError('xiaohongshu_import_no_targets');
  const normalizedContent = [...targets].join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new TargetImportError('xiaohongshu_import_output_too_large');
  }
  const shortLinkCount = [...targets].filter(target => target.startsWith('https://xhslink.com/')).length;
  const shortLinkBlocked = shortLinkCount > 1;
  return {
    content: normalizedContent,
    converted: true,
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount: shortLinkBlocked ? 0 : shortLinkCount,
    shortLinkCountsByPlatform: Object.freeze(
      !shortLinkBlocked && shortLinkCount ? { xiaohongshu: shortLinkCount } : {},
    ),
    shortLinkErrorsByPlatform: Object.freeze(
      shortLinkBlocked
        ? { xiaohongshu: 'xiaohongshu_import_short_link_batch_unsupported' }
        : {},
    ),
    blockedShortLinkCount: shortLinkBlocked ? shortLinkCount : 0,
    targetCount: targets.size,
  };
}

export const xiaohongshuImportPolicy = Object.freeze({
  id: 'xiaohongshu',
  maxBytes: XIAOHONGSHU_IMPORT_MAX_BYTES,
  tooLargeErrorCode: 'xiaohongshu_import_too_large',
  contentTooLargeErrorCode: 'xiaohongshu_import_content_too_large',
  delimitedErrorPrefix: 'xiaohongshu',
  requiresPlatformErrorCode: 'target_import_xiaohongshu_requires_platform',
  structuredDetectionPriority: 30,
  compatibilityAllowedPlatformIds: Object.freeze(['xiaohongshu']),
  shortLinkHosts: Object.freeze(['xhslink.com']),
  shortLinkLimit: 1,
  shortLinkErrorCode: 'xiaohongshu_import_short_link_batch_unsupported',
  structuredTargetImport: true,
  normalizeUrl: normalizeXiaohongshuUrl,
  normalizeStructuredImport,
  normalizeProviderCsvRow,
  providerCsvDescriptor,
  looksLikeStructuredExport(source) {
    return URL_DETECT_PATTERN.test(source)
      || /["'](?:note[_-]?id|feed[_-]?id|source[_-]?note[_-]?id)["']\s*:/i.test(source);
  },
  looksLikeDelimitedExport,
});
