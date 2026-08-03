import { parseDelimitedRecords } from './platforms/importShared.js';

export const XHS_TARGET_SOURCE_TYPES = Object.freeze([
  'keyword',
  'author_profile',
  'offline_search_result',
]);

export const XHS_BROWSER_SOURCE_TYPES = Object.freeze([
  'keyword',
  'author_profile',
]);

export const XHS_CANDIDATE_DECISION_STATUSES = Object.freeze([
  'accepted',
  'skipped',
  'needs_review',
]);

export const XHS_OFFLINE_MAX_BYTES = 10_000_000;
export const XHS_OFFLINE_MAX_CANDIDATES = 1_000;
export const XHS_INGEST_MAX_BYTES = 1_000_000;
export const XHS_CANDIDATE_JSON_FIELD_MAX_BYTES = 256 * 1024;
export const XHS_SOURCE_VALUE_MAX_LENGTH = 256;
export const XHS_KEYWORD_MAX_LENGTH = 64;
export const XHS_TARGET_URL_MAX_LENGTH = 512;
export const XHS_TITLE_MAX_LENGTH = 256;
export const XHS_DECISION_REASON_MAX_LENGTH = 512;

const XHS_OFFLINE_EXTENSIONS = Object.freeze(['.json', '.jsonl', '.csv']);
const NOTE_ID_PATTERN = /^[0-9a-f]{24}$/i;
const PROFILE_ID_PATTERN = /^[A-Za-z0-9_-]{5,64}$/;
const MAX_SANITIZE_DEPTH = 24;
const MAX_SANITIZE_NODES = 100_000;
const MAX_EVIDENCE_STRING_LENGTH = 64 * 1024;
const URL_IN_TEXT_PATTERN = /https?:\/\/[^\s"'<>]+/gi;
const INLINE_SECRET_PATTERN = /\b(?:a1|authorization|bearer|cookie|cookies|credential|password|secret|session|sessionid|signature|token|web_session|xsec(?:_source|_token)?)\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi;

const CANDIDATE_ENVELOPE_KEYS = Object.freeze([
  'candidates',
  'items',
  'results',
  'feeds',
  'records',
]);

const TARGET_URL_KEYS = Object.freeze([
  'raw_url',
  'candidate_url',
  'note_url',
  'share_url',
  'target_url',
  'url',
  'web_url',
]);

const NOTE_ID_KEYS = Object.freeze([
  'candidate_note_id',
  'note_id',
  'feed_id',
  'source_note_id',
  'id',
]);

const TITLE_KEYS = Object.freeze([
  'title',
  'display_title',
  'note_title',
  'name',
]);

const EVIDENCE_KEYS = Object.freeze([
  'target',
  'body',
  'body_snapshot',
  'content',
  'content_snapshots',
  'content_snapshot',
  'desc',
  'description',
  'expanded_body',
  'expanded_body_snapshot',
  'expanded_text',
  'expanded_text_snapshot',
  'note_text',
  'text',
  'text_snapshot',
  'pinned_comment',
  'pinned_comment_snapshot',
  'top_comment',
  'top_comment_snapshot',
  'author',
  'author_id',
  'author_name',
  'creator',
  'activity_window',
  'prizes',
  'complex_conditions',
  'initial_decision',
  'confidence',
  'reason_codes',
  'review_reason_codes',
  'source',
  'capture_method',
  'observed_at',
  'publish_at',
  'published_at',
  'publish_time',
  'deadline',
  'draw_at',
  'lottery_at',
  'expires_at',
]);

const RULE_KEYS = Object.freeze([
  'actions',
  'detected_actions',
  'required_actions',
  'prize',
  'prizes',
  'prize_snapshot',
  'timing',
  'conditions',
  'requirements',
]);

const CLASSIFICATION_KEYS = Object.freeze([
  'is_collection',
  'collection',
  'collection_post',
  'is_original_post',
  'original_post',
  'original_post_verified',
  'is_original',
  'author_verified',
  'author_match',
  'author_identity_verified',
  'complex_conditions',
  'complexity_flags',
  'unsupported_actions',
  'unresolved_requirements',
]);

export class XiaohongshuTargetPursuitError extends Error {
  constructor(code, details = {}) {
    super(code);
    this.name = 'XiaohongshuTargetPursuitError';
    this.code = code;
    Object.assign(this, details);
  }
}

function normalizedKey(value) {
  return String(value || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
}

function isRecord(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(String(value || '')).byteLength;
}

function jsonByteLength(value) {
  return utf8ByteLength(JSON.stringify(value));
}

function isSensitiveKey(value) {
  const key = normalizedKey(value);
  return key === 'a1'
    || key === 'authorization'
    || key === 'credential'
    || key === 'credentials'
    || key === 'password'
    || key === 'secret'
    || key === 'signature'
    || key === 'storagestate'
    || key === 'websession'
    || key.includes('cookie')
    || key.includes('session')
    || key.includes('password')
    || key.includes('secret')
    || key.includes('credential')
    || key.endsWith('token')
    || key.startsWith('xsec');
}

function isSensitiveUrlKey(value) {
  const key = normalizedKey(value);
  return isSensitiveKey(key)
    || key === 'auth'
    || key === 'sid'
    || key === 'xs'
    || key === 'xt';
}

function trimUrlPunctuation(value) {
  return String(value || '').replace(/[),.;!?\]}，。；！？）】]+$/u, '');
}

function sanitizeEmbeddedUrl(value, context) {
  const trimmed = trimUrlPunctuation(value);
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch {
    return trimmed;
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    context.discardedSensitiveFields += 1;
    return '[unsafe URL removed]';
  }
  for (const key of [...parsed.searchParams.keys()]) {
    if (!isSensitiveUrlKey(key)) continue;
    parsed.searchParams.delete(key);
    context.discardedSensitiveFields += 1;
  }
  if (parsed.hash) {
    parsed.hash = '';
    context.discardedSensitiveFields += 1;
  }
  return parsed.toString();
}

function sanitizeText(value, context) {
  const bounded = String(value || '').slice(0, MAX_EVIDENCE_STRING_LENGTH);
  const withSafeUrls = bounded.replace(
    URL_IN_TEXT_PATTERN,
    match => sanitizeEmbeddedUrl(match, context),
  );
  return withSafeUrls.replace(INLINE_SECRET_PATTERN, () => {
    context.discardedSensitiveFields += 1;
    return '[sensitive data removed]';
  });
}

function sanitizeValue(value, context, depth = 0) {
  context.visitedNodes += 1;
  if (depth > MAX_SANITIZE_DEPTH || context.visitedNodes > MAX_SANITIZE_NODES) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_too_complex');
  }
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string') return sanitizeText(value, context);
  if (Array.isArray(value)) {
    return value.map(item => sanitizeValue(item, context, depth + 1));
  }
  if (!isRecord(value)) return null;

  const output = {};
  for (const [key, child] of Object.entries(value)) {
    if (isSensitiveKey(key)) {
      context.discardedSensitiveFields += 1;
      continue;
    }
    output[key] = sanitizeValue(child, context, depth + 1);
  }
  return output;
}

function directValueByAliases(value, aliases) {
  if (!isRecord(value)) return undefined;
  const wanted = new Set(aliases.map(normalizedKey));
  for (const [key, child] of Object.entries(value)) {
    if (wanted.has(normalizedKey(key))) return child;
  }
  return undefined;
}

function directEntriesByAliases(value, aliases) {
  if (!isRecord(value)) return [];
  const wanted = new Set(aliases.map(normalizedKey));
  return Object.entries(value).filter(([key]) => wanted.has(normalizedKey(key)));
}

function objectFromKnownFields(sources, aliases) {
  const output = {};
  const seen = new Set();
  for (const source of sources) {
    for (const [key, value] of directEntriesByAliases(source, aliases)) {
      const identity = normalizedKey(key);
      if (seen.has(identity)) continue;
      seen.add(identity);
      output[key] = value;
    }
  }
  return output;
}

function parseObject(value) {
  if (isRecord(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function boundedTitle(value) {
  const title = String(value || '').trim();
  return title ? title.slice(0, XHS_TITLE_MAX_LENGTH) : '';
}

export function sanitizeXiaohongshuTargetUrl(value) {
  const source = trimUrlPunctuation(value);
  if (!source || source.length > XHS_TARGET_URL_MAX_LENGTH) return null;
  let parsed;
  try {
    parsed = new URL(source);
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
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (host === 'xhslink.com' || host === 'www.xhslink.com') {
    if (!parts.length) return null;
    return `https://xhslink.com/${parts.join('/')}`;
  }
  if (host !== 'xiaohongshu.com' && host !== 'www.xiaohongshu.com') return null;

  let noteId = '';
  if (parts.length === 2 && parts[0] === 'explore') {
    [, noteId] = parts;
  } else if (parts.length === 3 && parts[0] === 'discovery' && parts[1] === 'item') {
    noteId = parts[2];
  }
  if (!NOTE_ID_PATTERN.test(noteId)) return null;
  return `https://www.xiaohongshu.com/explore/${noteId.toLowerCase()}`;
}

export function normalizeXiaohongshuAuthorProfile(value) {
  const source = String(value || '').trim();
  if (!source || source.length > XHS_SOURCE_VALUE_MAX_LENGTH) return null;
  let parsed;
  try {
    parsed = new URL(source);
  } catch {
    return null;
  }
  const host = parsed.hostname.replace(/\.+$/, '').toLowerCase();
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (
    parsed.protocol !== 'https:'
    || parsed.username
    || parsed.password
    || (parsed.port && parsed.port !== '443')
    || (host !== 'xiaohongshu.com' && host !== 'www.xiaohongshu.com')
    || parts.length !== 3
    || parts[0] !== 'user'
    || parts[1] !== 'profile'
    || !PROFILE_ID_PATTERN.test(parts[2])
  ) return null;
  return `https://www.xiaohongshu.com/user/profile/${parts[2]}`;
}

function offlineExtension(fileName) {
  const normalized = String(fileName || '').trim().toLowerCase();
  return XHS_OFFLINE_EXTENSIONS.find(extension => normalized.endsWith(extension)) || '';
}

export function validateXiaohongshuSource(sourceType, sourceValue) {
  const normalizedType = String(sourceType || '').trim().toLowerCase();
  const value = String(sourceValue || '').trim();
  if (!XHS_TARGET_SOURCE_TYPES.includes(normalizedType)) {
    return 'xhs_target_source_type_invalid';
  }
  if (!value || value.length > XHS_SOURCE_VALUE_MAX_LENGTH) {
    return 'xhs_target_source_value_invalid';
  }
  if (
    normalizedType === 'keyword'
    && (value.length > XHS_KEYWORD_MAX_LENGTH || /[\r\n]/.test(value))
  ) {
    return 'xhs_target_keyword_invalid';
  }
  if (normalizedType === 'author_profile' && !normalizeXiaohongshuAuthorProfile(value)) {
    return 'xhs_target_author_profile_invalid';
  }
  if (normalizedType === 'offline_search_result' && !offlineExtension(value)) {
    return 'xhs_target_offline_extension_invalid';
  }
  return null;
}

export function buildXiaohongshuScanPayload(sourceType, sourceValue) {
  const normalizedType = String(sourceType || '').trim().toLowerCase();
  if (!XHS_BROWSER_SOURCE_TYPES.includes(normalizedType)) {
    throw new XiaohongshuTargetPursuitError('xhs_target_browser_source_invalid');
  }
  const errorCode = validateXiaohongshuSource(normalizedType, sourceValue);
  if (errorCode) throw new XiaohongshuTargetPursuitError(errorCode);
  return {
    source_type: normalizedType,
    source_value: normalizedType === 'author_profile'
      ? normalizeXiaohongshuAuthorProfile(sourceValue)
      : String(sourceValue).trim(),
  };
}

function extractJsonCandidateRecords(payload) {
  if (Array.isArray(payload)) return payload;
  if (!isRecord(payload)) return [];
  for (const key of CANDIDATE_ENVELOPE_KEYS) {
    const value = directValueByAliases(payload, [key]);
    if (Array.isArray(value)) return value;
  }
  const data = directValueByAliases(payload, ['data']);
  if (Array.isArray(data)) return data;
  if (isRecord(data)) {
    for (const key of CANDIDATE_ENVELOPE_KEYS) {
      const value = directValueByAliases(data, [key]);
      if (Array.isArray(value)) return value;
    }
    return [data];
  }
  return [payload];
}

function parseJsonRecords(content, extension) {
  if (extension === '.jsonl') {
    const records = [];
    const lines = String(content || '').split(/\r?\n/);
    for (const line of lines) {
      if (!line.trim()) continue;
      let parsed;
      try {
        parsed = JSON.parse(line);
      } catch {
        throw new XiaohongshuTargetPursuitError('xhs_target_offline_invalid_jsonl');
      }
      records.push(...extractJsonCandidateRecords(parsed));
    }
    return records;
  }
  let parsed;
  try {
    parsed = JSON.parse(content);
  } catch {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_invalid_json');
  }
  return extractJsonCandidateRecords(parsed);
}

function normalizedCsvHeader(value, index) {
  const header = String(value || '').trim();
  return header || `column_${index + 1}`;
}

function parseCsvRecords(content) {
  let rows;
  try {
    rows = parseDelimitedRecords(content, 'xhs_target_offline_invalid_csv');
  } catch {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_invalid_csv');
  }
  if (!rows.length) return [];
  const firstRowLooksLikeTarget = Boolean(sanitizeXiaohongshuTargetUrl(rows[0]?.[0]));
  if (firstRowLooksLikeTarget) {
    return rows.map(fields => ({
      raw_url: fields[0],
      title: fields[1] || '',
      body_snapshot: fields[2] || '',
    }));
  }
  const headers = rows[0].map(normalizedCsvHeader);
  return rows.slice(1).map((fields) => {
    const record = {};
    headers.forEach((header, index) => {
      record[header] = fields[index] ?? '';
    });
    return record;
  });
}

function nestedNoteCard(record) {
  return parseObject(directValueByAliases(record, [
    'note_card',
    'notecard',
    'note',
  ]));
}

function firstNonEmptyValue(sources, aliases) {
  for (const source of sources) {
    const value = directValueByAliases(source, aliases);
    if (value !== undefined && value !== null && String(value).trim()) return value;
  }
  return undefined;
}

function candidateUrlFromRecord(record, noteCard) {
  const target = parseObject(directValueByAliases(record, ['target']));
  const rawUrl = firstNonEmptyValue([record, noteCard, target], [
    ...TARGET_URL_KEYS,
    'note_url',
  ]);
  const fromUrl = sanitizeXiaohongshuTargetUrl(rawUrl);
  if (fromUrl) return fromUrl;
  const noteId = String(
    firstNonEmptyValue([record, noteCard, target], NOTE_ID_KEYS) || '',
  ).trim();
  return NOTE_ID_PATTERN.test(noteId)
    ? `https://www.xiaohongshu.com/explore/${noteId.toLowerCase()}`
    : null;
}

function boundedJsonField(value, field) {
  if (value === undefined || value === null) return undefined;
  if (jsonByteLength(value) > XHS_CANDIDATE_JSON_FIELD_MAX_BYTES) {
    throw new XiaohongshuTargetPursuitError(
      'xhs_target_offline_field_too_large',
      { field },
    );
  }
  return value;
}

function normalizeOfflineCandidate(rawRecord, context) {
  const inputRecord = typeof rawRecord === 'string'
    ? { raw_url: rawRecord }
    : rawRecord;
  if (!isRecord(inputRecord)) return null;
  const record = sanitizeValue(inputRecord, context);
  const noteCard = nestedNoteCard(record);
  const rawUrl = candidateUrlFromRecord(record, noteCard);
  if (!rawUrl) return null;

  const title = boundedTitle(firstNonEmptyValue([record, noteCard], TITLE_KEYS));
  const declaredEvidence = parseObject(directValueByAliases(record, ['evidence']));
  const declaredRule = parseObject(directValueByAliases(record, ['rule']));
  const declaredClassification = parseObject(
    directValueByAliases(record, ['classification']),
  );
  const inferredEvidence = objectFromKnownFields([record, noteCard], EVIDENCE_KEYS);
  const inferredRule = objectFromKnownFields([record, noteCard], RULE_KEYS);
  const inferredClassification = objectFromKnownFields(
    [record, noteCard],
    CLASSIFICATION_KEYS,
  );
  const evidence = boundedJsonField(
    {
      ...inferredEvidence,
      ...declaredEvidence,
      // Bind this sanitized offline record to the exact candidate. An imported
      // locator must never be able to redirect source verification elsewhere.
      offline_record: { raw_url: rawUrl },
    },
    'evidence',
  );
  const rule = boundedJsonField(
    { ...inferredRule, ...declaredRule },
    'rule',
  );
  const classification = boundedJsonField(
    { ...inferredClassification, ...declaredClassification },
    'classification',
  );

  return {
    raw_url: rawUrl,
    ...(title ? { title } : {}),
    ...(Object.keys(evidence).length ? { evidence } : {}),
    ...(Object.keys(rule).length ? { rule } : {}),
    ...(Object.keys(classification).length ? { classification } : {}),
  };
}

export function parseOfflineSearchResult(fileName, content) {
  const extension = offlineExtension(fileName);
  if (!extension) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_extension_invalid');
  }
  const source = String(content || '');
  const byteLength = utf8ByteLength(source);
  if (byteLength > XHS_OFFLINE_MAX_BYTES) {
    throw new XiaohongshuTargetPursuitError(
      'xhs_target_offline_too_large',
      { byteLength, maxBytes: XHS_OFFLINE_MAX_BYTES },
    );
  }

  const records = extension === '.csv'
    ? parseCsvRecords(source)
    : parseJsonRecords(source, extension);
  if (records.length > XHS_OFFLINE_MAX_CANDIDATES) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_too_many_candidates');
  }

  const context = { discardedSensitiveFields: 0, visitedNodes: 0 };
  const candidates = [];
  const identities = new Set();
  let discardedRows = 0;
  for (const record of records) {
    const candidate = normalizeOfflineCandidate(record, context);
    if (!candidate || identities.has(candidate.raw_url)) {
      discardedRows += 1;
      continue;
    }
    identities.add(candidate.raw_url);
    candidates.push(candidate);
  }
  if (!candidates.length) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_no_candidates');
  }
  return {
    candidates,
    discardedSensitiveFields: context.discardedSensitiveFields,
    discardedRows,
    targetCount: candidates.length,
    sourceByteLength: byteLength,
  };
}

function sanitizeCandidateForPayload(candidate, context) {
  if (!isRecord(candidate)) return null;
  const rawUrl = sanitizeXiaohongshuTargetUrl(candidate.raw_url);
  if (!rawUrl) return null;
  const sanitized = sanitizeValue(candidate, context);
  const title = boundedTitle(sanitized.title);
  const result = {
    raw_url: rawUrl,
    ...(title ? { title } : {}),
  };
  for (const field of ['evidence', 'rule', 'classification']) {
    const value = parseObject(sanitized[field]);
    if (!Object.keys(value).length) continue;
    result[field] = boundedJsonField(value, field);
  }
  return result;
}

export function buildCandidateIngestPayload(source, candidates) {
  const sourceType = String(source?.source_type || '').trim().toLowerCase();
  const sourceValue = String(source?.source_value || '').trim();
  const sourceError = validateXiaohongshuSource(sourceType, sourceValue);
  if (sourceError) throw new XiaohongshuTargetPursuitError(sourceError);
  if (!Array.isArray(candidates) || !candidates.length) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_no_candidates');
  }
  if (candidates.length > XHS_OFFLINE_MAX_CANDIDATES) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_too_many_candidates');
  }

  const context = { discardedSensitiveFields: 0, visitedNodes: 0 };
  const normalizedCandidates = candidates
    .map(candidate => sanitizeCandidateForPayload(candidate, context))
    .filter(Boolean);
  if (!normalizedCandidates.length) {
    throw new XiaohongshuTargetPursuitError('xhs_target_offline_no_candidates');
  }
  const normalizedSource = {
    source_type: sourceType,
    source_value: sourceType === 'author_profile'
      ? normalizeXiaohongshuAuthorProfile(sourceValue)
      : sourceValue,
  };
  if (source?.tracked_source_id !== undefined && source?.tracked_source_id !== null) {
    const trackedSourceId = Number(source.tracked_source_id);
    if (!Number.isSafeInteger(trackedSourceId) || trackedSourceId <= 0) {
      throw new XiaohongshuTargetPursuitError('xhs_target_tracked_source_invalid');
    }
    normalizedSource.tracked_source_id = trackedSourceId;
  }
  const payload = { source: normalizedSource, candidates: normalizedCandidates };
  const payloadBytes = jsonByteLength(payload);
  if (payloadBytes > XHS_INGEST_MAX_BYTES) {
    throw new XiaohongshuTargetPursuitError(
      'xhs_target_offline_output_too_large',
      { byteLength: payloadBytes, maxBytes: XHS_INGEST_MAX_BYTES },
    );
  }
  return payload;
}

export function candidateItemsFromResponse(response) {
  if (Array.isArray(response)) return response;
  return Array.isArray(response?.items) ? response.items : [];
}

function firstDefined(sources, aliases) {
  for (const source of sources) {
    if (!isRecord(source)) continue;
    const value = directValueByAliases(source, aliases);
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
}

function normalizedBoolean(value) {
  if (typeof value === 'boolean') return value;
  if (value === 1 || value === '1') return true;
  if (value === 0 || value === '0') return false;
  const normalized = String(value || '').trim().toLowerCase();
  if (['yes', 'true', 'verified', 'match', 'matched', 'original'].includes(normalized)) {
    return true;
  }
  if (['no', 'false', 'unverified', 'mismatch', 'not_original'].includes(normalized)) {
    return false;
  }
  return null;
}

function normalizedList(value) {
  if (Array.isArray(value)) return value.filter(item => item !== '' && item !== null);
  if (isRecord(value)) {
    return Object.entries(value)
      .filter(([, enabled]) => enabled !== false && enabled !== null && enabled !== '')
      .map(([key, detail]) => (
        detail === true ? key : { name: key, detail }
      ));
  }
  if (typeof value === 'string') {
    const parsed = value.trim();
    if (!parsed) return [];
    try {
      return normalizedList(JSON.parse(parsed));
    } catch {
      return parsed.split(/[,，、;\n]+/).map(item => item.trim()).filter(Boolean);
    }
  }
  return value === undefined || value === null ? [] : [value];
}

function timingSnapshot(sources) {
  const declared = firstDefined(sources, ['timing', 'timing_snapshot']);
  if (declared !== undefined) return declared;
  const fields = objectFromKnownFields(sources, [
    'publish_at',
    'published_at',
    'publish_time',
    'deadline',
    'expires_at',
    'draw_at',
    'lottery_at',
  ]);
  return Object.keys(fields).length ? fields : null;
}

export function normalizeXiaohongshuCandidate(item) {
  const evidence = parseObject(item?.evidence);
  const rule = parseObject(item?.rule);
  const classification = parseObject(item?.classification);
  const sources = [classification, rule, evidence, item];
  const target = parseObject(firstDefined(sources, ['target']));
  const author = parseObject(firstDefined(sources, ['author', 'creator']));
  const contentSnapshots = parseObject(firstDefined(sources, ['content_snapshots']));
  const activityWindow = parseObject(firstDefined(sources, ['activity_window']));
  const originalTrace = parseObject(
    firstDefined([target, classification, evidence], ['original_trace']),
  );
  const decisionStatus = String(
    item?.decision_status || item?.status || 'pending',
  ).trim().toLowerCase();
  return {
    id: item?.id ?? null,
    version: Number(item?.version),
    title: boundedTitle(firstDefined([item, target, evidence], TITLE_KEYS)),
    rawUrl: sanitizeXiaohongshuTargetUrl(
      firstDefined([item, target], ['raw_url', 'candidate_url', 'note_url', 'url']),
    ),
    canonicalUrl: sanitizeXiaohongshuTargetUrl(item?.canonical_url),
    decisionStatus,
    decisionReason: String(item?.decision_reason || ''),
    acceptedLotteryId: item?.accepted_lottery_id ?? null,
    createdAt: item?.created_at || '',
    updatedAt: item?.updated_at || '',
    firstSeenAt: item?.first_seen_at || '',
    lastSeenAt: item?.last_seen_at || '',
    decidedAt: item?.decided_at || '',
    sourceHits: Array.isArray(item?.source_hits) ? item.source_hits : [],
    analysis: {
      initialDecision: firstDefined(sources, ['initial_decision']) ?? null,
      confidence: firstDefined(sources, ['confidence']) ?? null,
      reasonCodes: normalizedList(firstDefined(sources, [
        'review_reason_codes',
        'reason_codes',
      ])),
    },
    verification: {
      collection: normalizedBoolean(firstDefined([target, ...sources], [
        'is_collection',
        'collection',
        'collection_post',
      ])),
      originalPost: normalizedBoolean(firstDefined(
        [originalTrace, target, ...sources],
        [
          'verified',
          'trace_complete',
          'is_original_post',
          'original_post',
          'original_post_verified',
          'is_original',
        ],
      )),
      author: normalizedBoolean(firstDefined([author, ...sources], [
        'verified',
        'author_verified',
        'author_match',
        'author_identity_verified',
      ])),
    },
    verificationDetails: {
      collection: Object.keys(target).length ? target : null,
      originalPost: Object.keys(originalTrace).length
        ? originalTrace
        : firstDefined([target, ...sources], ['original_trace']) ?? null,
      author: Object.keys(author).length ? author : null,
    },
    bodySnapshot: firstDefined([contentSnapshots, ...sources], [
      'body',
      'body_snapshot',
      'text_snapshot',
      'content_snapshot',
      'note_text',
      'text',
      'content',
      'desc',
      'description',
    ]) ?? null,
    expandedSnapshot: firstDefined([contentSnapshots, ...sources], [
      'expanded_body',
      'expanded_body_snapshot',
      'expanded_text_snapshot',
      'expanded_text',
    ]) ?? null,
    pinnedCommentSnapshot: firstDefined([contentSnapshots, ...sources], [
      'pinned_comment',
      'pinned_comment_snapshot',
      'top_comment_snapshot',
      'top_comment',
    ]) ?? null,
    timing: Object.keys(activityWindow).length
      ? activityWindow
      : timingSnapshot(sources),
    prize: firstDefined(sources, [
      'prize_snapshot',
      'prizes',
      'prize',
    ]) ?? null,
    actions: normalizedList(firstDefined(sources, [
      'required_actions',
      'actions',
      'detected_actions',
    ])),
    complexConditions: normalizedList(firstDefined(sources, [
      'complex_conditions',
      'complexity_flags',
      'unsupported_actions',
      'unresolved_requirements',
      'conditions',
    ])),
    evidence,
    rule,
    classification,
  };
}

export function candidateDecisionCanSubmit(candidate, decisionStatus) {
  const normalizedStatus = String(decisionStatus || '').trim().toLowerCase();
  const currentStatus = String(
    candidate?.decisionStatus || candidate?.decision_status || '',
  ).toLowerCase();
  return Boolean(candidate?.id)
    && Number.isSafeInteger(Number(candidate?.version))
    && Number(candidate.version) >= 1
    && XHS_CANDIDATE_DECISION_STATUSES.includes(normalizedStatus)
    && currentStatus !== 'accepted'
    && currentStatus !== normalizedStatus;
}

export function buildCandidateDecisionPayload(
  decisionStatus,
  decisionReason,
  expectedVersion,
) {
  const normalizedStatus = String(decisionStatus || '').trim().toLowerCase();
  const version = Number(expectedVersion);
  if (!XHS_CANDIDATE_DECISION_STATUSES.includes(normalizedStatus)) {
    throw new XiaohongshuTargetPursuitError('xhs_target_decision_status_invalid');
  }
  if (!Number.isSafeInteger(version) || version < 1) {
    throw new XiaohongshuTargetPursuitError('xhs_target_decision_version_invalid');
  }
  const reason = String(decisionReason || '').trim();
  return {
    decision_status: normalizedStatus,
    expected_version: version,
    ...(reason ? { decision_reason: reason.slice(0, XHS_DECISION_REASON_MAX_LENGTH) } : {}),
  };
}
