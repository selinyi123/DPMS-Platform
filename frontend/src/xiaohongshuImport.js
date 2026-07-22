const XHS_NOTE_ID_PATTERN = /^[0-9a-f]{24}$/i;
const XHS_URL_PATTERN = /https:\/\/(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)\/[^\s"'<>]+/gi;
const XHS_URL_DETECT_PATTERN = /https:\/\/(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)\//i;
const DOUYIN_AWEME_ID_PATTERN = /^\d{8,32}$/;
const DOUYIN_NOTE_ID_PATTERN = /^\d{19}$/;
const DOUYIN_URL_PATTERN = /https:\/\/(?:www\.)?(?:douyin\.com|iesdouyin\.com|v\.douyin\.com)\/[^\s"'<>]+/gi;
const DOUYIN_URL_DETECT_PATTERN = /https:\/\/(?:www\.)?(?:douyin\.com|iesdouyin\.com|v\.douyin\.com)\//i;
const WEIBO_MID_PATTERN = /^[1-9][0-9]{0,18}$/;
const WEIBO_MAX_STATUS_ID = 9223372036854775807n;
const WEIBO_MBLOGID_PATTERN = /^(?=.*[A-Za-z])[A-Za-z0-9]{6,16}$/;
const WEIBO_URL_PATTERN = /https:\/\/(?:www\.)?(?:weibo\.com|m\.weibo\.cn|t\.cn)\/[^\s"'<>]+/gi;
const WEIBO_URL_DETECT_PATTERN = /https:\/\/(?:www\.)?(?:weibo\.com|m\.weibo\.cn|t\.cn)\//i;
const KEY_VALUE_MARKER_PATTERN = /(?:^|[^a-z0-9])([a-z][a-z0-9_-]*)\s*(?=[=:,])/gim;

const NOTE_ID_KEYS = new Set(['noteid', 'feedid', 'sourcenoteid']);
const URL_KEYS = new Set([
  'link',
  'links',
  'noteurl',
  'rawurl',
  'shareurl',
  'url',
  'urls',
  'weburl',
]);
const SPECIFIC_URL_KEYS = new Set(['noteurl', 'shareurl']);
const DOUYIN_ID_KEYS = new Set(['awemeid', 'videoid']);
const DOUYIN_URL_KEYS = new Set(['awemeurl', 'shareurl', 'videourl', 'weburl']);
const WEIBO_ID_KEYS = new Set(['idstr', 'mblogid', 'mid']);
const WEIBO_URL_KEYS = new Set(['longurl', 'shareurl', 'statusurl', 'url', 'weburl', 'weibourl']);
const WEIBO_RECORD_COLLECTION_KEYS = new Set([
  'cards',
  'items',
  'mblogs',
  'records',
  'results',
  'statuses',
]);
const WEIBO_CONTENT_CONTEXT_KEYS = new Set([
  'attitudescount',
  'commentscount',
  'createdat',
  'picids',
  'repostscount',
  'text',
  'textraw',
  'user',
]);
const DOUYIN_RECORD_COLLECTION_KEYS = new Set([
  'awemelist',
  'contents',
  'items',
  'records',
  'results',
]);
const DOUYIN_CONTENT_CONTEXT_KEYS = new Set([
  'awemetype',
  'caption',
  'createtime',
  'desc',
  'images',
  'statistics',
  'title',
  'video',
]);
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
  'retweetedstatus',
  'user',
]);
const SHORT_LINK_HOSTS = new Set(['b23.tv', 't.cn', 'v.douyin.com', 'xhslink.com']);
const SENSITIVE_KEYS = new Set([
  'a1',
  'authorization',
  'bilijct',
  'cookie',
  'cookies',
  'dedeuserid',
  'session',
  'sessionid',
  'sessdata',
  'sid',
  'sub',
  'token',
  'websession',
  'xs',
  'xsectoken',
  'xt',
]);
const MAX_DEPTH = 32;
const MAX_NODES = 100_000;
const MAX_TARGETS = 1_000;
const MAX_TARGET_LENGTH = 2_048;
const MAX_NORMALIZED_CONTENT_LENGTH = 200_000;
export const XIAOHONGSHU_IMPORT_MAX_BYTES = 10_000_000;
export const DOUYIN_IMPORT_MAX_BYTES = 10_000_000;
export const WEIBO_IMPORT_MAX_BYTES = 10_000_000;
export const TARGET_IMPORT_PASSTHROUGH_MAX_BYTES = 200_000;

export class TargetImportError extends Error {
  constructor(code) {
    super(code);
    this.name = 'TargetImportError';
    this.code = code;
  }
}

// Compatibility alias for the original Xiaohongshu-only importer API.
export const XiaohongshuImportError = TargetImportError;

function normalizedKey(value) {
  return String(value || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
}

function parseStructuredExport(content, invalidCode = 'xiaohongshu_import_invalid_json') {
  try {
    return JSON.parse(content);
  } catch {
    const lines = content.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.length < 2) throw new XiaohongshuImportError(invalidCode);
    try {
      return lines.map(line => JSON.parse(line));
    } catch {
      throw new XiaohongshuImportError(invalidCode);
    }
  }
}

export function looksLikeStructuredTargetExport(content) {
  const trimmed = content.trim();
  return trimmed.startsWith('{') || trimmed.startsWith('[');
}

function noteRecordHasContext(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return false;
  const keys = new Set(Object.keys(record).map(normalizedKey));
  // Community feeds use either {id, xsecToken, noteCard} or a flat
  // {id, title, xsecToken} row. Requiring one of those shapes avoids treating
  // creator/comment IDs as notes merely because they have text fields.
  return keys.has('notecard')
    || (keys.has('xsectoken') && (keys.has('title') || keys.has('displaytitle')));
}

function douyinRecordHasContext(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return false;
  const keys = new Set(Object.keys(record).map(normalizedKey));
  // MediaCrawler and F2 both expose an aweme/video id beside content fields.
  // Requiring content context avoids interpreting creator, comment or media
  // sub-record IDs as lottery targets.
  return !keys.has('commentid')
    && [...DOUYIN_ID_KEYS].some(key => keys.has(key))
    && [...DOUYIN_CONTENT_CONTEXT_KEYS].some(key => keys.has(key));
}

function weiboRecordHasContext(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return false;
  const keys = new Set(Object.keys(record).map(normalizedKey));
  return !keys.has('commentid')
    && (
      [...WEIBO_ID_KEYS].some(key => keys.has(key))
      || [...WEIBO_URL_KEYS].some(key => keys.has(key))
    )
    && [...WEIBO_CONTENT_CONTEXT_KEYS].some(key => keys.has(key));
}

function isSensitiveKey(value) {
  const key = normalizedKey(value);
  return SENSITIVE_KEYS.has(key)
    || key.endsWith('token')
    || key.includes('cookie')
    || key.includes('session')
    || key.startsWith('dedeuserid')
    || key === 'storagestate'
    || key === 'webid'
    || key.startsWith('xsec');
}

function noteUrlFromId(value) {
  const noteId = String(value || '').trim();
  if (!XHS_NOTE_ID_PATTERN.test(noteId)) return null;
  return `https://www.xiaohongshu.com/explore/${noteId.toLowerCase()}`;
}

function douyinDeclaredContentRoute(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return null;
  for (const [key, value] of Object.entries(record)) {
    if (normalizedKey(key) !== 'awemetype') continue;
    return Number(value) === 68 ? 'note' : 'video';
  }
  return null;
}

function douyinContentRoute(record) {
  return douyinDeclaredContentRoute(record) || 'video';
}

function douyinUrlFromId(value, route = 'video') {
  const awemeId = String(value || '').trim();
  if (!DOUYIN_AWEME_ID_PATTERN.test(awemeId)) return null;
  const normalizedRoute = route === 'note' ? 'note' : 'video';
  if (normalizedRoute === 'note' && !DOUYIN_NOTE_ID_PATTERN.test(awemeId)) return null;
  return `https://www.douyin.com/${normalizedRoute}/${awemeId}`;
}

function isWeiboStatusId(value) {
  const statusId = String(value || '').trim();
  const numericMid = WEIBO_MID_PATTERN.test(statusId)
    && BigInt(statusId) <= WEIBO_MAX_STATUS_ID;
  return numericMid || WEIBO_MBLOGID_PATTERN.test(statusId);
}

function weiboUrlFromId(value) {
  const statusId = String(value || '').trim();
  return isWeiboStatusId(statusId) ? `https://weibo.com/detail/${statusId}` : null;
}

function trimUrlPunctuation(value) {
  return String(value || '').replace(/[),.;!?\]}，。；！？）】]+$/u, '');
}

function normalizeXiaohongshuUrl(value) {
  if (String(value || '').length > MAX_TARGET_LENGTH) return null;
  let parsed;
  try {
    parsed = new URL(trimUrlPunctuation(value));
  } catch {
    return null;
  }
  if (
    parsed.protocol !== 'https:'
    || parsed.username
    || parsed.password
    || (parsed.port && parsed.port !== '443')
  ) return null;

  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  if (containsSensitiveUrlMaterial(parsed)) {
    throw new XiaohongshuImportError('target_import_sensitive_content_rejected');
  }
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
    // Preserve only the share-link path. Query parameters may contain short-
    // lived access context and must not be persisted by the import endpoint.
    const normalized = `https://xhslink.com/${parts.join('/')}`;
    return /[,\t\r\n]/.test(normalized) ? null : normalized;
  }
  return null;
}

function normalizeDouyinUrl(value) {
  if (String(value || '').length > MAX_TARGET_LENGTH) return null;
  let parsed;
  try {
    parsed = new URL(trimUrlPunctuation(value));
  } catch {
    return null;
  }
  if (
    parsed.protocol !== 'https:'
    || parsed.username
    || parsed.password
    || (parsed.port && parsed.port !== '443')
  ) return null;

  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  if (containsSensitiveUrlMaterial(parsed)) {
    throw new XiaohongshuImportError('target_import_sensitive_content_rejected');
  }
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (host === 'douyin.com' || host === 'www.douyin.com') {
    if (parts.length !== 2 || !['note', 'video'].includes(parts[0])) return null;
    if (!DOUYIN_AWEME_ID_PATTERN.test(parts[1])) return null;
    if (parts[0] === 'note' && !DOUYIN_NOTE_ID_PATTERN.test(parts[1])) return null;
    return `https://www.douyin.com/${parts[0]}/${parts[1]}`;
  }
  if (host === 'iesdouyin.com' || host === 'www.iesdouyin.com') {
    if (parts.length !== 3 || parts[0] !== 'share' || parts[1] !== 'video') return null;
    return douyinUrlFromId(parts[2]);
  }
  if (host === 'v.douyin.com' && parts.length) {
    const normalized = `https://v.douyin.com/${parts.join('/')}`;
    return /[,\t\r\n]/.test(normalized) ? null : normalized;
  }
  return null;
}

function normalizeWeiboUrl(value) {
  if (String(value || '').length > MAX_TARGET_LENGTH) return null;
  let parsed;
  try {
    parsed = new URL(trimUrlPunctuation(value));
  } catch {
    return null;
  }
  if (
    parsed.protocol !== 'https:'
    || parsed.username
    || parsed.password
    || (parsed.port && parsed.port !== '443')
  ) return null;

  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  if (containsSensitiveUrlMaterial(parsed)) {
    throw new XiaohongshuImportError('target_import_sensitive_content_rejected');
  }
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (host === 'weibo.com' || host === 'www.weibo.com') {
    if (parts.length !== 2) return null;
    if (parts[0] === 'detail') return weiboUrlFromId(parts[1]);
    if (!/^\d+$/.test(parts[0]) || !isWeiboStatusId(parts[1])) return null;
    return `https://weibo.com/${parts[0]}/${parts[1]}`;
  }
  if (host === 'm.weibo.cn') {
    if (parts.length !== 2 || !['detail', 'status'].includes(parts[0])) return null;
    const target = weiboUrlFromId(parts[1]);
    return target ? `https://m.weibo.cn/detail/${parts[1]}` : null;
  }
  if (host === 't.cn' && parts.length === 1) {
    const normalized = `https://t.cn/${parts[0]}`;
    return /[,\t\r\n]/.test(normalized) ? null : normalized;
  }
  return null;
}

function douyinCanonicalTargetIdentity(target) {
  const match = /^https:\/\/www\.douyin\.com\/(note|video)\/(\d{8,32})$/.exec(String(target || ''));
  return match ? { route: match[1], id: match[2] } : null;
}

function assertDouyinTargetConsistency(target, directIds, declaredRoute = null) {
  if (!target) return;
  const identity = douyinCanonicalTargetIdentity(target);
  const ids = directIds instanceof Set ? directIds : new Set(directIds || []);
  if (
    ids.size > 1
    || ((ids.size || declaredRoute) && !identity)
    || (identity && ids.size === 1 && !ids.has(identity.id))
    || (identity && declaredRoute && identity.route !== declaredRoute)
  ) {
    throw new XiaohongshuImportError('douyin_import_conflicting_target');
  }
}

function weiboCanonicalTargetId(target) {
  const value = String(target || '');
  const match = /^https:\/\/(?:weibo\.com\/(?:detail|\d+)|m\.weibo\.cn\/detail)\/([A-Za-z0-9]+)$/.exec(value);
  return match && isWeiboStatusId(match[1]) ? match[1] : null;
}

function assertWeiboTargetConsistency(target, directIds) {
  if (!target) return;
  const ids = directIds instanceof Set ? directIds : new Set(directIds || []);
  const targetId = weiboCanonicalTargetId(target);
  if (ids.size && (!targetId || !ids.has(targetId))) {
    throw new XiaohongshuImportError('weibo_import_conflicting_target');
  }
}

function directWeiboRecordIds(record) {
  const byKey = new Map();
  const all = new Set();
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    return { all, preferred: null, ambiguous: false };
  }
  for (const [key, value] of Object.entries(record)) {
    const keyName = normalizedKey(key);
    if (!WEIBO_ID_KEYS.has(keyName) || isSensitiveKey(key)) continue;
    for (const candidate of directStringValues(value, 0, 'weibo_import_too_complex')) {
      if (!isWeiboStatusId(candidate)) continue;
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

function weiboExplicitRecordTargets(record) {
  const targets = new Set();
  if (!record || typeof record !== 'object' || Array.isArray(record)) return targets;
  for (const [key, value] of Object.entries(record)) {
    if (!WEIBO_URL_KEYS.has(normalizedKey(key)) || isSensitiveKey(key)) continue;
    for (const directValue of directStringValues(value, 0, 'weibo_import_too_complex')) {
      for (const candidate of weiboUrlCandidates(directValue)) {
        const target = normalizeWeiboUrl(candidate);
        if (target) targets.add(target);
      }
    }
  }
  return targets;
}

function douyinExplicitRecordTargets(record) {
  const targets = new Set();
  if (!record || typeof record !== 'object' || Array.isArray(record)) return targets;
  for (const [key, value] of Object.entries(record)) {
    if (!DOUYIN_URL_KEYS.has(normalizedKey(key)) || isSensitiveKey(key)) continue;
    for (const directValue of directStringValues(value, 0, 'douyin_import_too_complex')) {
      for (const candidate of douyinUrlCandidates(directValue)) {
        const target = normalizeDouyinUrl(candidate);
        if (target) targets.add(target);
      }
    }
  }
  return targets;
}

function sanitizeGenericTargetUrl(value) {
  if (String(value || '').length > MAX_TARGET_LENGTH) return null;
  let parsed;
  try {
    parsed = new URL(trimUrlPunctuation(value));
  } catch {
    return null;
  }
  if (
    !['http:', 'https:'].includes(parsed.protocol)
    || !parsed.hostname
    || parsed.username
    || parsed.password
    || (parsed.port && !['80', '443'].includes(parsed.port))
  ) return null;
  if (containsSensitiveUrlMaterial(parsed)) {
    throw new XiaohongshuImportError('target_import_sensitive_content_rejected');
  }
  for (const key of [...parsed.searchParams.keys()]) {
    if (isSensitiveKey(key)) parsed.searchParams.delete(key);
  }
  // Core canonicalization identifies supported targets by host/path and drops
  // query context. Removing the entire query here prevents platform-specific
  // credential names from bypassing a generic denylist in mixed imports.
  parsed.search = '';
  parsed.hash = '';
  const normalized = parsed.toString();
  if (containsSensitiveExportMarker(normalized)) {
    throw new XiaohongshuImportError('target_import_sensitive_content_rejected');
  }
  return /[,\t\r\n]/.test(normalized) ? null : normalized;
}

function* urlCandidates(value) {
  const text = String(value || '').trim();
  if (!text) return;
  yield text;
  for (const match of text.matchAll(XHS_URL_PATTERN)) yield match[0];
}

function* douyinUrlCandidates(value) {
  const text = String(value || '').trim();
  if (!text) return;
  yield text;
  for (const match of text.matchAll(DOUYIN_URL_PATTERN)) yield match[0];
}

function* weiboUrlCandidates(value) {
  const text = String(value || '').trim();
  if (!text) return;
  yield text;
  for (const match of text.matchAll(WEIBO_URL_PATTERN)) yield match[0];
}

function looksLikeXiaohongshuDelimitedExport(source) {
  if (XHS_URL_DETECT_PATTERN.test(source)) return true;
  let firstFields;
  try {
    [firstFields] = parseDelimitedRecords(source);
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

function containsSensitiveExportMarker(source) {
  for (const match of source.matchAll(KEY_VALUE_MARKER_PATTERN)) {
    if (isSensitiveKey(match[1])) return true;
  }
  return false;
}

function containsSensitiveUrlMaterial(parsed) {
  let path = String(parsed?.pathname || '');
  for (let pass = 0; pass < 16; pass += 1) {
    if (containsSensitiveExportMarker(path)) return true;
    try {
      const decoded = decodeURIComponent(path);
      if (decoded === path) return false;
      path = decoded;
    } catch {
      // A malformed escape can hide a later valid, encoded credential marker
      // from whole-path decoding. Reject it instead of attempting recovery.
      return true;
    }
  }
  // Excessive nested encoding is not required by any supported target shape
  // and is unsafe to forward to a platform resolver.
  return true;
}

function* directStringValues(value, depth = 0, complexityCode = 'xiaohongshu_import_too_complex') {
  if (typeof value === 'string') {
    yield value;
    return;
  }
  if (!Array.isArray(value)) return;
  if (depth >= MAX_DEPTH) {
    throw new XiaohongshuImportError(complexityCode);
  }
  for (const item of value) yield* directStringValues(item, depth + 1, complexityCode);
}

function isDpmsImportHeader(fields) {
  const keys = (fields || []).map(normalizedKey);
  return keys[0] === 'platform' && URL_KEYS.has(keys[1]);
}

function dataRecordsForImport(source, invalidCsvCode = 'xiaohongshu_import_invalid_csv') {
  return parseDelimitedRecords(source, invalidCsvCode)
    .filter(fields => fields.some(field => field) && !String(fields[0] || '').trim().startsWith('#'));
}

function compatibleMixedRecords(source, allowedPlatformIds, defaultPlatform) {
  const supported = new Set(
    [...(allowedPlatformIds || [])].map(value => String(value).trim().toLowerCase()).filter(Boolean),
  );
  supported.add('xiaohongshu');
  supported.add(String(defaultPlatform || '').trim().toLowerCase());
  let records;
  try {
    records = dataRecordsForImport(source);
  } catch {
    return false;
  }
  if (isDpmsImportHeader(records[0])) records = records.slice(1);
  const defaultPlatformKey = String(defaultPlatform || '').trim().toLowerCase();
  return records.length > 0 && records.every((fields) => {
    const candidatePlatform = fields.length >= 2 && !/^https?:\/\//i.test(fields[0])
      ? String(fields[0] || '').trim().toLowerCase()
      : '';
    if (candidatePlatform) {
      return supported.has(candidatePlatform) && Boolean(sanitizeGenericTargetUrl(fields[1]));
    }
    return supported.has(defaultPlatformKey) && Boolean(sanitizeGenericTargetUrl(fields[0]));
  });
}

function delimiterForExport(source) {
  const firstDataLine = source
    .split(/\r?\n/)
    .map(line => line.trim())
    .find(line => line && !line.startsWith('#')) || '';
  let quoted = false;
  let commas = 0;
  let tabs = 0;
  for (let index = 0; index < firstDataLine.length; index += 1) {
    const char = firstDataLine[index];
    if (char === '"') {
      if (quoted && firstDataLine[index + 1] === '"') index += 1;
      else quoted = !quoted;
    } else if (!quoted && char === ',') commas += 1;
    else if (!quoted && char === '\t') tabs += 1;
  }
  return tabs > commas ? '\t' : ',';
}

function parseDelimitedRecords(source, invalidCsvCode = 'xiaohongshu_import_invalid_csv') {
  const delimiter = delimiterForExport(source);
  const records = [];
  const fields = [];
  let current = '';
  let quoted = false;
  const finishRecord = () => {
    fields.push(current.trim());
    current = '';
    if (fields.some(field => field)) records.push([...fields]);
    fields.length = 0;
  };

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"') {
      if (quoted && source[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
      continue;
    }
    if (char === delimiter && !quoted) {
      fields.push(current.trim());
      current = '';
      continue;
    }
    if ((char === '\n' || char === '\r') && !quoted) {
      finishRecord();
      if (char === '\r' && source[index + 1] === '\n') index += 1;
      continue;
    }
    if (char === '\r' && quoted && source[index + 1] === '\n') {
      current += '\n';
      index += 1;
      continue;
    }
    current += char;
  }
  if (quoted) throw new XiaohongshuImportError(invalidCsvCode);
  if (current || fields.length) finishRecord();
  return records;
}

function isValidScore(value) {
  return /^\d{1,3}$/.test(value) && Number(value) <= 100;
}

function isValidExpiry(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/.exec(value);
  if (!match) return false;
  const [, year, month, day, hour = '0', minute = '0', second = '0'] = match;
  const numeric = [year, month, day, hour, minute, second].map(Number);
  const [yyyy, mm, dd, hh, min, sec] = numeric;
  if (mm < 1 || mm > 12 || hh > 23 || min > 59 || sec > 59) return false;
  return dd >= 1 && dd <= new Date(Date.UTC(yyyy, mm, 0)).getUTCDate();
}

function normalizedDpmsFields(
  fields,
  explicitPlatform,
  target,
  invalidMetadataCode = 'xiaohongshu_import_invalid_metadata',
) {
  const targetIndex = explicitPlatform ? 1 : 0;
  const scoreIndex = targetIndex + 1;
  const expiryIndex = targetIndex + 2;
  const output = explicitPlatform ? [explicitPlatform, target] : [target];
  const score = String(fields[scoreIndex] || '').trim();
  const expiry = String(fields[expiryIndex] || '').trim();
  if (score && !isValidScore(score)) {
    throw new XiaohongshuImportError(invalidMetadataCode);
  }
  if (expiry && (!score || !isValidExpiry(expiry))) {
    throw new XiaohongshuImportError(invalidMetadataCode);
  }
  if (score) output.push(score);
  if (expiry) output.push(expiry);
  return output;
}

function normalizeDelimitedExport(source, allowedPlatformIds, defaultPlatform = 'xiaohongshu') {
  const defaultPlatformKey = String(defaultPlatform || '').trim().toLowerCase();
  const errorPrefix = ['douyin', 'weibo'].includes(defaultPlatformKey)
    ? defaultPlatformKey
    : 'xiaohongshu';
  const invalidCsvCode = `${errorPrefix}_import_invalid_csv`;
  const invalidMetadataCode = `${errorPrefix}_import_invalid_metadata`;
  const supportedPlatformIds = new Set(
    [...(allowedPlatformIds || [])].map(value => String(value).trim().toLowerCase()).filter(Boolean),
  );
  supportedPlatformIds.add('xiaohongshu');
  supportedPlatformIds.add(String(defaultPlatform || '').trim().toLowerCase());
  const dataRecords = dataRecordsForImport(source, invalidCsvCode);
  if (!dataRecords.length) throw new XiaohongshuImportError(`${errorPrefix}_import_no_targets`);

  const firstFields = dataRecords[0];
  const headerKeys = firstFields.map(normalizedKey);
  const isDpmsCsv = isDpmsImportHeader(firstFields);
  const providerUrlKeys = defaultPlatformKey === 'douyin'
    ? DOUYIN_URL_KEYS
    : (defaultPlatformKey === 'weibo' ? WEIBO_URL_KEYS : SPECIFIC_URL_KEYS);
  const providerIdKeys = defaultPlatformKey === 'douyin'
    ? DOUYIN_ID_KEYS
    : (defaultPlatformKey === 'weibo' ? WEIBO_ID_KEYS : NOTE_ID_KEYS);
  const specificUrlIndex = headerKeys.findIndex(key => providerUrlKeys.has(key));
  const genericUrlIndex = ['douyin', 'weibo'].includes(defaultPlatformKey)
    ? -1
    : headerKeys.findIndex(key => URL_KEYS.has(key));
  const urlIndex = specificUrlIndex >= 0 ? specificUrlIndex : genericUrlIndex;
  const contentIdIndex = defaultPlatformKey === 'weibo'
    ? ['mblogid', 'mid', 'idstr'].map(key => headerKeys.indexOf(key)).find(index => index >= 0) ?? -1
    : headerKeys.findIndex(key => providerIdKeys.has(key));
  const contentTypeIndex = headerKeys.findIndex(key => key === 'awemetype');
  const isProviderCsv = !isDpmsCsv && (urlIndex >= 0 || contentIdIndex >= 0);
  const targetRecords = isDpmsCsv ? dataRecords.slice(1) : dataRecords;
  const output = [];
  const dedupe = new Set();
  let discardedSensitiveFields = 0;
  let discardedRows = 0;

  const addLine = (line, identity) => {
    if (!identity || dedupe.has(identity)) {
      discardedRows += 1;
      return;
    }
    dedupe.add(identity);
    output.push(line);
    if (output.length > MAX_TARGETS) {
      throw new XiaohongshuImportError(`${errorPrefix}_import_too_many_targets`);
    }
  };

  if (isProviderCsv) {
    discardedSensitiveFields = headerKeys.filter(isSensitiveKey).length;
    for (const fields of dataRecords.slice(1)) {
      let target;
      if (defaultPlatformKey === 'douyin') {
        const rawId = contentIdIndex >= 0 ? String(fields[contentIdIndex] || '').trim() : '';
        const directIds = DOUYIN_AWEME_ID_PATTERN.test(rawId) ? new Set([rawId]) : new Set();
        const hasDeclaredType = contentTypeIndex >= 0 && String(fields[contentTypeIndex] || '').trim() !== '';
        const declaredRoute = hasDeclaredType
          ? (Number(fields[contentTypeIndex]) === 68 ? 'note' : 'video')
          : null;
        const urlTarget = normalizeDouyinUrl(urlIndex >= 0 ? fields[urlIndex] : '');
        assertDouyinTargetConsistency(urlTarget, directIds, declaredRoute);
        target = urlTarget || douyinUrlFromId(rawId, declaredRoute || 'video');
      } else if (defaultPlatformKey === 'weibo') {
        const rawId = contentIdIndex >= 0 ? String(fields[contentIdIndex] || '').trim() : '';
        const directIds = isWeiboStatusId(rawId) ? new Set([rawId]) : new Set();
        const urlTarget = normalizeWeiboUrl(urlIndex >= 0 ? fields[urlIndex] : '');
        assertWeiboTargetConsistency(urlTarget, directIds);
        target = urlTarget || weiboUrlFromId(rawId);
      } else {
        target = normalizeXiaohongshuUrl(fields[urlIndex]) || noteUrlFromId(fields[contentIdIndex]);
      }
      if (!target) {
        discardedRows += 1;
        continue;
      }
      addLine(target, target);
    }
  } else {
    for (const fields of targetRecords) {
      const candidatePlatform = fields.length >= 2 && !/^https?:\/\//i.test(fields[0])
        ? fields[0].trim().toLowerCase()
        : '';
      const explicitPlatform = supportedPlatformIds.has(candidatePlatform)
        ? candidatePlatform
        : '';
      if (candidatePlatform && !explicitPlatform) {
        discardedRows += 1;
        continue;
      }
      const rowPlatform = explicitPlatform || defaultPlatformKey;
      if (!['douyin', 'weibo', 'xiaohongshu'].includes(rowPlatform)) {
        // Preserve the existing mixed-platform CSV contract. This sanitizer
        // owns only Xiaohongshu URLs, but still strips columns that the Core
        // import grammar never consumes so provider secrets cannot hitchhike.
        const targetIndex = explicitPlatform ? 1 : 0;
        const sanitizedTarget = sanitizeGenericTargetUrl(fields[targetIndex]);
        if (!sanitizedTarget) {
          discardedRows += 1;
          continue;
        }
        const preservedFields = normalizedDpmsFields(
          fields,
          explicitPlatform,
          sanitizedTarget,
          invalidMetadataCode,
        );
        const preservedLine = preservedFields.join(',');
        if (preservedFields.length !== fields.length || sanitizedTarget !== fields[targetIndex]) {
          discardedSensitiveFields += 1;
        }
        addLine(preservedLine, preservedLine);
        continue;
      }
      const targetIndex = explicitPlatform ? 1 : 0;
      const target = rowPlatform === 'douyin'
        ? normalizeDouyinUrl(fields[targetIndex])
        : (rowPlatform === 'weibo'
            ? normalizeWeiboUrl(fields[targetIndex])
            : normalizeXiaohongshuUrl(fields[targetIndex]));
      if (!target) {
        discardedRows += 1;
        continue;
      }
      const sanitizedFields = normalizedDpmsFields(
        fields,
        explicitPlatform,
        target,
        invalidMetadataCode,
      );
      if (fields[targetIndex] !== target) discardedSensitiveFields += 1;
      if (sanitizedFields.length !== fields.length) discardedSensitiveFields += 1;
      const sanitizedLine = sanitizedFields.join(',');
      addLine(sanitizedLine, `${explicitPlatform}:${target}`);
    }
  }

  if (!output.length) throw new XiaohongshuImportError(`${errorPrefix}_import_no_targets`);
  const normalizedContent = output.join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new XiaohongshuImportError(`${errorPrefix}_import_output_too_large`);
  }
  const shortLinkCount = output.filter((line) => {
    const fields = line.split(',');
    const target = /^https?:\/\//i.test(fields[0]) ? fields[0] : fields[1];
    try {
      return SHORT_LINK_HOSTS.has(new URL(target).hostname.replace(/\.+$/, '').toLowerCase());
    } catch {
      return false;
    }
  }).length;
  if (shortLinkCount && output.length > 1) {
    throw new XiaohongshuImportError(`${errorPrefix}_import_short_link_batch_unsupported`);
  }
  return {
    content: normalizedContent,
    converted: isProviderCsv || isDpmsCsv || normalizedContent !== source.trim(),
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount,
    targetCount: output.length,
  };
}

/**
 * Convert JSON/JSONL exported by read-only XHS tools into DPMS target lines.
 *
 * Only validated XHS note/share URLs and the existing bounded DPMS CSV columns
 * survive. Cookies, sessions, xsec tokens and every other provider field
 * remain in the browser and are never submitted to Core.
 */
export function normalizeXiaohongshuTargetImport(content, options = {}) {
  const source = String(content || '');
  if (new TextEncoder().encode(source).byteLength > XIAOHONGSHU_IMPORT_MAX_BYTES) {
    throw new XiaohongshuImportError('xiaohongshu_import_too_large');
  }
  if (!looksLikeStructuredTargetExport(source)) {
    return normalizeDelimitedExport(source, options.allowedPlatformIds);
  }

  const payload = parseStructuredExport(source.trim());
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
    if (targets.size > MAX_TARGETS) {
      throw new XiaohongshuImportError('xiaohongshu_import_too_many_targets');
    }
    return true;
  };

  const visit = (value, key = '', parent = null, depth = 0, genericUrlAllowed = false) => {
    visitedNodes += 1;
    if (depth > MAX_DEPTH || visitedNodes > MAX_NODES) {
      throw new XiaohongshuImportError('xiaohongshu_import_too_complex');
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
      } else if (URL_KEYS.has(keyName) && genericUrlAllowed) {
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
        for (const directValue of directStringValues(childValue, 0, 'douyin_import_too_complex')) {
          if (NOTE_ID_KEYS.has(childKeyName) || (childKeyName === 'id' && noteRecordHasContext(value))) {
            const target = noteUrlFromId(directValue);
            if (target) directTargets.add(target);
          }
          if (URL_KEYS.has(childKeyName)) {
            for (const candidate of urlCandidates(directValue)) {
              const target = normalizeXiaohongshuUrl(candidate);
              if (target) directTargets.add(target);
            }
          }
        }
      }
      if (directTargets.size > 1) {
        throw new XiaohongshuImportError('xiaohongshu_import_conflicting_target');
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
  if (!targets.size) {
    throw new XiaohongshuImportError('xiaohongshu_import_no_targets');
  }
  const normalizedContent = [...targets].join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new XiaohongshuImportError('xiaohongshu_import_output_too_large');
  }
  const shortLinkCount = [...targets].filter(target => target.startsWith('https://xhslink.com/')).length;
  if (shortLinkCount && targets.size > 1) {
    throw new XiaohongshuImportError('xiaohongshu_import_short_link_batch_unsupported');
  }
  return {
    content: normalizedContent,
    converted: true,
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount,
    targetCount: targets.size,
  };
}

function looksLikeDouyinStructuredExport(source) {
  return DOUYIN_URL_DETECT_PATTERN.test(source)
    || /["'](?:aweme[_-]?id|aweme[_-]?list|video[_-]?id)["']\s*:/i.test(source);
}

/**
 * Convert bounded JSON/JSONL output from read-only Douyin discovery tools into
 * DPMS video targets. Provider cookies, signatures and response metadata never
 * leave the browser. We intentionally consume only stable aweme IDs and
 * canonical/share URLs instead of copying any crawler or signature code.
 */
export function normalizeDouyinTargetImport(content, options = {}) {
  const source = String(content || '');
  if (new TextEncoder().encode(source).byteLength > DOUYIN_IMPORT_MAX_BYTES) {
    throw new XiaohongshuImportError('douyin_import_too_large');
  }
  if (!looksLikeStructuredTargetExport(source)) {
    return normalizeDelimitedExport(source, options.allowedPlatformIds, 'douyin');
  }

  const payload = parseStructuredExport(source.trim(), 'douyin_import_invalid_json');
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
    if (targets.size > MAX_TARGETS) {
      throw new XiaohongshuImportError('douyin_import_too_many_targets');
    }
    return true;
  };

  const visit = (value, key = '', parent = null, depth = 0, genericUrlAllowed = false) => {
    visitedNodes += 1;
    if (depth > MAX_DEPTH || visitedNodes > MAX_NODES) {
      throw new XiaohongshuImportError('douyin_import_too_complex');
    }
    if (typeof value === 'string') {
      const keyName = normalizedKey(key);
      if (DOUYIN_ID_KEYS.has(keyName) && douyinRecordHasContext(parent)) {
        const explicitTargets = douyinExplicitRecordTargets(parent);
        if (!explicitTargets.size) {
          const target = douyinUrlFromId(value, douyinContentRoute(parent));
          if (target) addTarget(target);
          else discardedRows += 1;
        }
      }
      if (!keyName) {
        const target = normalizeDouyinUrl(value);
        if (target) addTarget(target);
        else discardedRows += 1;
      } else if (DOUYIN_URL_KEYS.has(keyName) && genericUrlAllowed) {
        let recognized = false;
        for (const candidate of douyinUrlCandidates(value)) {
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
    const objectAllowsGenericUrl = douyinRecordHasContext(value)
      || DOUYIN_RECORD_COLLECTION_KEYS.has(keyName)
      || (depth === 1 && parent === payload && Array.isArray(parent));
    if (douyinRecordHasContext(value)) {
      const directTargets = douyinExplicitRecordTargets(value);
      const directIds = new Set();
      for (const [childKey, childValue] of Object.entries(value)) {
        if (isSensitiveKey(childKey)) continue;
        const childKeyName = normalizedKey(childKey);
        for (const directValue of directStringValues(childValue)) {
          if (DOUYIN_ID_KEYS.has(childKeyName)) {
            if (DOUYIN_AWEME_ID_PATTERN.test(directValue)) directIds.add(directValue);
          }
        }
      }
      const [explicitTarget] = directTargets;
      if (
        directTargets.size > 1
        || directIds.size > 1
      ) {
        throw new XiaohongshuImportError('douyin_import_conflicting_target');
      }
      assertDouyinTargetConsistency(explicitTarget, directIds, douyinDeclaredContentRoute(value));
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
  if (!targets.size) {
    throw new XiaohongshuImportError('douyin_import_no_targets');
  }
  const normalizedContent = [...targets].join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new XiaohongshuImportError('douyin_import_output_too_large');
  }
  const shortLinkCount = [...targets].filter(target => target.startsWith('https://v.douyin.com/')).length;
  if (shortLinkCount && targets.size > 1) {
    throw new XiaohongshuImportError('douyin_import_short_link_batch_unsupported');
  }
  return {
    content: normalizedContent,
    converted: true,
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount,
    targetCount: targets.size,
  };
}

function looksLikeWeiboStructuredExport(source) {
  return WEIBO_URL_DETECT_PATTERN.test(source)
    || /["'](?:mblog[_-]?id|reposts[_-]?count|statuses|status[_-]?url|text[_-]?raw)["']\s*:/i.test(source);
}

/**
 * Convert bounded read-only Weibo status exports into canonical DPMS targets.
 * Only status identifiers and trusted status URLs survive; account, comment,
 * OAuth and provider metadata are deliberately discarded in the browser.
 */
export function normalizeWeiboTargetImport(content, options = {}) {
  const source = String(content || '');
  if (new TextEncoder().encode(source).byteLength > WEIBO_IMPORT_MAX_BYTES) {
    throw new XiaohongshuImportError('weibo_import_too_large');
  }
  if (!looksLikeStructuredTargetExport(source)) {
    return normalizeDelimitedExport(source, options.allowedPlatformIds, 'weibo');
  }

  const payload = parseStructuredExport(source.trim(), 'weibo_import_invalid_json');
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
    if (targets.size > MAX_TARGETS) {
      throw new XiaohongshuImportError('weibo_import_too_many_targets');
    }
    return true;
  };

  const visit = (value, key = '', parent = null, depth = 0, genericUrlAllowed = false) => {
    visitedNodes += 1;
    if (depth > MAX_DEPTH || visitedNodes > MAX_NODES) {
      throw new XiaohongshuImportError('weibo_import_too_complex');
    }
    if (typeof value === 'string') {
      const keyName = normalizedKey(key);
      if (WEIBO_ID_KEYS.has(keyName) && weiboRecordHasContext(parent)) {
        const explicitTargets = weiboExplicitRecordTargets(parent);
        const ids = directWeiboRecordIds(parent);
        if (!explicitTargets.size && value === ids.preferred) {
          const target = weiboUrlFromId(value);
          if (target) addTarget(target);
          else discardedRows += 1;
        }
      }
      if (!keyName) {
        const target = normalizeWeiboUrl(value);
        if (target) addTarget(target);
        else discardedRows += 1;
      } else if (WEIBO_URL_KEYS.has(keyName) && genericUrlAllowed) {
        let recognized = false;
        for (const candidate of weiboUrlCandidates(value)) {
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
    const objectAllowsGenericUrl = weiboRecordHasContext(value)
      || WEIBO_RECORD_COLLECTION_KEYS.has(keyName)
      || (depth === 1 && parent === payload && Array.isArray(parent));
    if (weiboRecordHasContext(value)) {
      const directTargets = weiboExplicitRecordTargets(value);
      const ids = directWeiboRecordIds(value);
      if (directTargets.size > 1 || ids.ambiguous) {
        throw new XiaohongshuImportError('weibo_import_conflicting_target');
      }
      const [explicitTarget] = directTargets;
      assertWeiboTargetConsistency(explicitTarget, ids.all);
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
  if (!targets.size) throw new XiaohongshuImportError('weibo_import_no_targets');
  const normalizedContent = [...targets].join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new XiaohongshuImportError('weibo_import_output_too_large');
  }
  const shortLinkCount = [...targets].filter(target => target.startsWith('https://t.cn/')).length;
  if (shortLinkCount && targets.size > 1) {
    throw new XiaohongshuImportError('weibo_import_short_link_batch_unsupported');
  }
  return {
    content: normalizedContent,
    converted: true,
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount,
    targetCount: targets.size,
  };
}

export function normalizeTargetImportForPlatform(platform, content, options = {}) {
  const platformKey = String(platform || '').trim().toLowerCase();
  const source = String(content || '');
  const importMaxBytes = platformKey === 'xiaohongshu'
    ? XIAOHONGSHU_IMPORT_MAX_BYTES
    : (platformKey === 'douyin'
        ? DOUYIN_IMPORT_MAX_BYTES
        : (platformKey === 'weibo' ? WEIBO_IMPORT_MAX_BYTES : TARGET_IMPORT_PASSTHROUGH_MAX_BYTES));
  if (new TextEncoder().encode(source).byteLength > importMaxBytes) {
    const errorCode = platformKey === 'xiaohongshu'
      ? 'xiaohongshu_import_too_large'
      : (platformKey === 'douyin'
          ? 'douyin_import_too_large'
          : (platformKey === 'weibo' ? 'weibo_import_too_large' : 'target_import_too_large'));
    throw new XiaohongshuImportError(
      errorCode,
    );
  }
  if (platformKey === 'xiaohongshu') {
    return normalizeXiaohongshuTargetImport(source, options);
  }
  if (platformKey === 'douyin') {
    return normalizeDouyinTargetImport(source, options);
  }
  if (platformKey === 'weibo') {
    return normalizeWeiboTargetImport(source, options);
  }
  if (looksLikeStructuredTargetExport(source)) {
    // JSON/JSONL is not part of Core's line protocol. Rejecting it here also
    // prevents an XHS export from bypassing sanitization when the UI is still
    // on its default platform.
    const errorCode = looksLikeDouyinStructuredExport(source)
      ? 'target_import_douyin_requires_platform'
      : (looksLikeWeiboStructuredExport(source)
          ? 'target_import_weibo_requires_platform'
          : 'target_import_xiaohongshu_requires_platform');
    throw new XiaohongshuImportError(errorCode);
  }
  if (containsSensitiveExportMarker(source)) {
    if (compatibleMixedRecords(source, options.allowedPlatformIds, platformKey)) {
      return normalizeDelimitedExport(source, options.allowedPlatformIds, platformKey);
    }
    throw new XiaohongshuImportError('target_import_sensitive_content_rejected');
  }
  if (looksLikeXiaohongshuDelimitedExport(source)) {
    if (compatibleMixedRecords(source, options.allowedPlatformIds, platformKey)) {
      return normalizeDelimitedExport(source, options.allowedPlatformIds, platformKey);
    }
    throw new XiaohongshuImportError('target_import_xiaohongshu_requires_platform');
  }
  if (source.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new XiaohongshuImportError('target_import_content_too_large');
  }
  // Use the same bounded parser and URL sanitizer for every platform. Raw
  // pass-through would allow encoded credential query keys to reach Core.
  return normalizeDelimitedExport(source, options.allowedPlatformIds, platformKey);
}
