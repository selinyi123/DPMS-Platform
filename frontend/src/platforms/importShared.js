const KEY_VALUE_MARKER_PATTERN = /(?:^|[^a-z0-9])([a-z][a-z0-9_-]*)\s*(?=[=:,])/gim;

const GENERIC_URL_KEYS = new Set([
  'link',
  'links',
  'noteurl',
  'rawurl',
  'shareurl',
  'url',
  'urls',
  'weburl',
]);

export function isGenericUrlKey(value) {
  return GENERIC_URL_KEYS.has(normalizedKey(value));
}

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

export const MAX_IMPORT_DEPTH = 32;
export const MAX_IMPORT_NODES = 100_000;
export const MAX_IMPORT_TARGETS = 1_000;
// Match lotteries.raw_url VARCHAR(512); accepting a longer target here only
// defers a deterministic storage failure to the Core import endpoint.
export const MAX_TARGET_LENGTH = 512;
export const MAX_NORMALIZED_CONTENT_LENGTH = 200_000;
export const TARGET_IMPORT_PASSTHROUGH_MAX_BYTES = 200_000;
// Browser-side safety ceiling for a single source file. Platform-owned limits
// are still enforced below this ceiling after delimited rows have been
// attributed to their declared platform.
export const TARGET_IMPORT_FILE_MAX_BYTES = 10_000_000;

export class TargetImportError extends Error {
  constructor(code, details = {}) {
    super(code);
    this.name = 'TargetImportError';
    this.code = code;
    if (details.platformId) this.platformId = details.platformId;
    if (Array.isArray(details.platformIds)) {
      this.platformIds = Object.freeze([...details.platformIds]);
    }
    if (Number.isSafeInteger(details.byteLength)) this.byteLength = details.byteLength;
    if (Number.isSafeInteger(details.maxBytes)) this.maxBytes = details.maxBytes;
  }
}

export function normalizedKey(value) {
  return String(value || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
}

export function parseStructuredExport(content, invalidCode) {
  try {
    return JSON.parse(content);
  } catch {
    const lines = content.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.length < 2) throw new TargetImportError(invalidCode);
    try {
      return lines.map(line => JSON.parse(line));
    } catch {
      throw new TargetImportError(invalidCode);
    }
  }
}

export function looksLikeStructuredTargetExport(content) {
  const trimmed = String(content || '').trim();
  return trimmed.startsWith('{') || trimmed.startsWith('[');
}

export function isSensitiveKey(value) {
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

export function trimUrlPunctuation(value) {
  return String(value || '').replace(/[),.;!?\]}，。；！？）】]+$/u, '');
}

export function containsSensitiveExportMarker(source) {
  for (const match of String(source || '').matchAll(KEY_VALUE_MARKER_PATTERN)) {
    if (isSensitiveKey(match[1])) return true;
  }
  return false;
}

export function boundedNormalizedTarget(value) {
  const normalized = String(value || '');
  return normalized.length <= MAX_TARGET_LENGTH && !/[,\t\r\n]/.test(normalized)
    ? normalized
    : null;
}

export function containsSensitiveUrlMaterial(parsed) {
  let path = String(parsed?.pathname || '');
  for (let pass = 0; pass < 16; pass += 1) {
    if (containsSensitiveExportMarker(path)) return true;
    try {
      const decoded = decodeURIComponent(path);
      if (decoded === path) return false;
      path = decoded;
    } catch {
      return true;
    }
  }
  return true;
}

export function parseSecureHttpsTarget(value) {
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
  if (containsSensitiveUrlMaterial(parsed)) {
    throw new TargetImportError('target_import_sensitive_content_rejected');
  }
  return parsed;
}

export function sanitizeGenericTargetUrl(value) {
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
    throw new TargetImportError('target_import_sensitive_content_rejected');
  }
  for (const key of [...parsed.searchParams.keys()]) {
    if (isSensitiveKey(key)) parsed.searchParams.delete(key);
  }
  parsed.search = '';
  parsed.hash = '';
  const normalized = parsed.toString();
  if (containsSensitiveExportMarker(normalized)) {
    throw new TargetImportError('target_import_sensitive_content_rejected');
  }
  return boundedNormalizedTarget(normalized);
}

export function* directStringValues(value, depth = 0, complexityCode) {
  if (typeof value === 'string') {
    yield value;
    return;
  }
  if (!Array.isArray(value)) return;
  if (depth >= MAX_IMPORT_DEPTH) throw new TargetImportError(complexityCode);
  for (const item of value) yield* directStringValues(item, depth + 1, complexityCode);
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

function utf8ByteLengthOfRange(source, start, end) {
  let byteLength = 0;
  for (let index = start; index < end; index += 1) {
    const unit = source.charCodeAt(index);
    if (unit <= 0x7f) byteLength += 1;
    else if (unit <= 0x7ff) byteLength += 2;
    else if (
      unit >= 0xd800
      && unit <= 0xdbff
      && index + 1 < end
      && source.charCodeAt(index + 1) >= 0xdc00
      && source.charCodeAt(index + 1) <= 0xdfff
    ) {
      byteLength += 4;
      index += 1;
    } else {
      // TextEncoder replaces unpaired surrogates with U+FFFD (three bytes).
      byteLength += 3;
    }
  }
  return byteLength;
}

function parseDelimitedRecordEntries(source, invalidCsvCode) {
  const delimiter = delimiterForExport(source);
  const records = [];
  const fields = [];
  let current = '';
  let quoted = false;
  let recordStart = 0;
  const finishRecord = (recordEnd) => {
    fields.push(current.trim());
    current = '';
    if (fields.some(field => field)) {
      records.push({
        fields: [...fields],
        byteLength: utf8ByteLengthOfRange(source, recordStart, recordEnd),
      });
    }
    fields.length = 0;
    recordStart = recordEnd;
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
      const isCrLf = char === '\r' && source[index + 1] === '\n';
      const recordEnd = index + (isCrLf ? 2 : 1);
      finishRecord(recordEnd);
      if (isCrLf) index += 1;
      continue;
    }
    if (char === '\r' && quoted && source[index + 1] === '\n') {
      current += '\n';
      index += 1;
      continue;
    }
    current += char;
  }
  if (quoted) throw new TargetImportError(invalidCsvCode);
  if (current || fields.length) finishRecord(source.length);
  return records;
}

export function parseDelimitedRecords(source, invalidCsvCode) {
  return parseDelimitedRecordEntries(source, invalidCsvCode)
    .map(entry => entry.fields);
}

export function dataRecordEntriesForImport(source, invalidCsvCode) {
  return parseDelimitedRecordEntries(source, invalidCsvCode)
    .filter(entry => (
      entry.fields.some(field => field)
      && !String(entry.fields[0] || '').trim().startsWith('#')
    ));
}

export function dataRecordsForImport(source, invalidCsvCode) {
  return dataRecordEntriesForImport(source, invalidCsvCode)
    .map(entry => entry.fields);
}

export function isDpmsImportHeader(fields) {
  const keys = (fields || []).map(normalizedKey);
  return keys[0] === 'platform' && isGenericUrlKey(keys[1]);
}

function isValidScore(value) {
  return /^\d{1,3}$/.test(value) && Number(value) <= 100;
}

function isValidExpiry(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/.exec(value);
  if (!match) return false;
  const [, year, month, day, hour = '0', minute = '0', second = '0'] = match;
  const [yyyy, mm, dd, hh, min, sec] = [year, month, day, hour, minute, second].map(Number);
  if (mm < 1 || mm > 12 || hh > 23 || min > 59 || sec > 59) return false;
  return dd >= 1 && dd <= new Date(Date.UTC(yyyy, mm, 0)).getUTCDate();
}

function normalizedDpmsFields(fields, explicitPlatform, target, invalidMetadataCode) {
  const targetIndex = explicitPlatform ? 1 : 0;
  const scoreIndex = targetIndex + 1;
  const expiryIndex = targetIndex + 2;
  const output = explicitPlatform ? [explicitPlatform, target] : [target];
  const score = String(fields[scoreIndex] || '').trim();
  const expiry = String(fields[expiryIndex] || '').trim();
  if (score && !isValidScore(score)) throw new TargetImportError(invalidMetadataCode);
  if (expiry && (!score || !isValidExpiry(expiry))) {
    throw new TargetImportError(invalidMetadataCode);
  }
  if (score) output.push(score);
  if (expiry) output.push(expiry);
  return output;
}

function supportedPlatformSet(defaultPolicy, allowedPlatformIds) {
  const supported = new Set(
    [...(allowedPlatformIds || [])].map(value => String(value).trim().toLowerCase()).filter(Boolean),
  );
  supported.add(defaultPolicy.id);
  for (const platform of defaultPolicy.compatibilityAllowedPlatformIds || []) supported.add(platform);
  return supported;
}

function positivePolicyMaxBytes(policy) {
  const maxBytes = Number(policy?.maxBytes);
  return Number.isSafeInteger(maxBytes) && maxBytes > 0 ? maxBytes : 0;
}

function throwImportTooLarge(policy, platformId, byteLength) {
  const maxBytes = positivePolicyMaxBytes(policy);
  throw new TargetImportError(
    policy?.tooLargeErrorCode || 'target_import_too_large',
    {
      platformId,
      byteLength,
      maxBytes,
    },
  );
}

function delimitedSizeOwners(
  defaultPolicy,
  options,
  policies,
  recordEntries,
) {
  if (!recordEntries.length) return new Map();
  const firstFields = recordEntries[0].fields;
  const isDpmsCsv = isDpmsImportHeader(firstFields);
  const providerDescriptor = !isDpmsCsv
    ? defaultPolicy.providerCsvDescriptor?.(firstFields.map(normalizedKey))
    : null;
  if (providerDescriptor) {
    return new Map([[
      defaultPolicy.id,
      recordEntries.reduce((total, entry) => total + entry.byteLength, 0),
    ]]);
  }

  const supported = supportedPlatformSet(defaultPolicy, options.allowedPlatformIds);
  const owners = new Map();
  const targetEntries = isDpmsCsv ? recordEntries.slice(1) : recordEntries;
  for (const entry of targetEntries) {
    const fields = entry.fields;
    const candidatePlatform = fields.length >= 2 && !/^https?:\/\//i.test(fields[0])
      ? String(fields[0] || '').trim().toLowerCase()
      : '';
    const explicitPlatform = supported.has(candidatePlatform) ? candidatePlatform : '';
    // Unknown explicit platform rows are discarded by normalizeDelimitedExport
    // and therefore cannot borrow any registered platform's row budget.
    if (candidatePlatform && !explicitPlatform) continue;
    const rowPlatform = explicitPlatform || defaultPolicy.id;
    const policy = policies[rowPlatform]
      || (rowPlatform === defaultPolicy.id ? defaultPolicy : null);
    if (!policy) {
      // A missing peer policy is fail-closed under the selected platform's
      // smaller/equal budget. The async runtime loads every declared peer
      // before reaching this path, so this only protects legacy direct calls.
      owners.set(
        defaultPolicy.id,
        (owners.get(defaultPolicy.id) || 0) + entry.byteLength,
      );
      continue;
    }
    owners.set(rowPlatform, (owners.get(rowPlatform) || 0) + entry.byteLength);
  }
  return owners;
}

export function validateImportSourceSize(
  defaultPolicy,
  content,
  options = {},
  policies = {},
  prepared = {},
) {
  const source = String(content || '');
  const sourceByteLength = Number.isSafeInteger(prepared.sourceByteLength)
    ? prepared.sourceByteLength
    : new TextEncoder().encode(source).byteLength;
  if (sourceByteLength > TARGET_IMPORT_FILE_MAX_BYTES) {
    // Direct compatibility-facade callers historically received the selected
    // platform's error code at this boundary. The async file runtime applies
    // the selection-independent shared ceiling before calling this helper.
    throwImportTooLarge(defaultPolicy, defaultPolicy.id, sourceByteLength);
  }

  if (looksLikeStructuredTargetExport(source)) {
    const maxBytes = positivePolicyMaxBytes(defaultPolicy);
    if (!maxBytes || sourceByteLength > maxBytes) {
      throwImportTooLarge(defaultPolicy, defaultPolicy.id, sourceByteLength);
    }
    return;
  }

  let recordEntries = prepared.delimitedRecordEntries;
  if (!Array.isArray(recordEntries)) {
    try {
      recordEntries = dataRecordEntriesForImport(
        source,
        `${defaultPolicy.delimitedErrorPrefix}_import_invalid_csv`,
      );
    } catch {
      // Keep malformed single-platform files on their owning platform's
      // historical size boundary. The normal parser will surface its stable
      // syntax error after this bounded check.
      recordEntries = null;
    }
  }
  if (!recordEntries) {
    const maxBytes = positivePolicyMaxBytes(defaultPolicy);
    if (!maxBytes || sourceByteLength > maxBytes) {
      throwImportTooLarge(defaultPolicy, defaultPolicy.id, sourceByteLength);
    }
    return;
  }

  const owners = delimitedSizeOwners(
    defaultPolicy,
    options,
    policies,
    recordEntries,
  );
  if (!owners.size) {
    const maxBytes = positivePolicyMaxBytes(defaultPolicy);
    if (!maxBytes || sourceByteLength > maxBytes) {
      throwImportTooLarge(defaultPolicy, defaultPolicy.id, sourceByteLength);
    }
    return;
  }

  if (owners.size === 1) {
    const [platformId] = owners.keys();
    const policy = policies[platformId] || defaultPolicy;
    const maxBytes = positivePolicyMaxBytes(policy);
    if (!maxBytes || sourceByteLength > maxBytes) {
      throwImportTooLarge(policy, platformId, sourceByteLength);
    }
    return;
  }

  for (const [platformId, byteLength] of owners) {
    const policy = policies[platformId] || defaultPolicy;
    const maxBytes = positivePolicyMaxBytes(policy);
    if (!maxBytes || byteLength > maxBytes) {
      throwImportTooLarge(policy, platformId, byteLength);
    }
  }
}

function isShortLinkTarget(target, policy) {
  let hostname;
  try {
    hostname = new URL(target).hostname.replace(/\.+$/, '').toLowerCase();
  } catch {
    return false;
  }
  return policy?.shortLinkHosts?.includes(hostname) === true;
}

function shortLinkLimit(policy) {
  const configured = Number(policy?.shortLinkLimit);
  return Number.isSafeInteger(configured) && configured >= 0 ? configured : 1;
}

function shortLinkErrorCode(policy) {
  return policy?.shortLinkErrorCode
    || `${policy?.delimitedErrorPrefix || 'target'}_import_short_link_batch_unsupported`;
}

export function compatibleMixedRecords(
  source,
  defaultPolicy,
  allowedPlatformIds,
  policies,
  preparedRecords = null,
) {
  const supported = supportedPlatformSet(defaultPolicy, allowedPlatformIds);
  let records;
  try {
    records = Array.isArray(preparedRecords)
      ? preparedRecords
      : dataRecordsForImport(source, `${defaultPolicy.delimitedErrorPrefix}_import_invalid_csv`);
  } catch {
    return false;
  }
  if (isDpmsImportHeader(records[0])) records = records.slice(1);
  return records.length > 0 && records.every((fields) => {
    const candidatePlatform = fields.length >= 2 && !/^https?:\/\//i.test(fields[0])
      ? String(fields[0] || '').trim().toLowerCase()
      : '';
    const targetIndex = candidatePlatform ? 1 : 0;
    const rowPlatform = candidatePlatform || defaultPolicy.id;
    if (!supported.has(rowPlatform)) return false;
    const rowPolicy = policies[rowPlatform];
    const target = rowPolicy?.normalizeUrl
      ? rowPolicy.normalizeUrl(fields[targetIndex])
      : sanitizeGenericTargetUrl(fields[targetIndex]);
    return Boolean(target);
  });
}

export function normalizeDelimitedExport(
  source,
  defaultPolicy,
  options,
  policies,
  preparedRecords = null,
) {
  const errorPrefix = defaultPolicy.delimitedErrorPrefix;
  const invalidCsvCode = `${errorPrefix}_import_invalid_csv`;
  const invalidMetadataCode = `${errorPrefix}_import_invalid_metadata`;
  const supportedPlatformIds = supportedPlatformSet(defaultPolicy, options.allowedPlatformIds);
  const dataRecords = Array.isArray(preparedRecords)
    ? preparedRecords
    : dataRecordsForImport(source, invalidCsvCode);
  if (!dataRecords.length) throw new TargetImportError(`${errorPrefix}_import_no_targets`);

  const firstFields = dataRecords[0];
  const headerKeys = firstFields.map(normalizedKey);
  const isDpmsCsv = isDpmsImportHeader(firstFields);
  const providerDescriptor = !isDpmsCsv
    ? defaultPolicy.providerCsvDescriptor?.(headerKeys)
    : null;
  const isProviderCsv = Boolean(providerDescriptor);
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
    if (output.length > MAX_IMPORT_TARGETS) {
      throw new TargetImportError(`${errorPrefix}_import_too_many_targets`);
    }
  };

  if (isProviderCsv) {
    discardedSensitiveFields = headerKeys.filter(isSensitiveKey).length;
    for (const fields of dataRecords.slice(1)) {
      const target = defaultPolicy.normalizeProviderCsvRow(fields, providerDescriptor);
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
      const explicitPlatform = supportedPlatformIds.has(candidatePlatform) ? candidatePlatform : '';
      if (candidatePlatform && !explicitPlatform) {
        discardedRows += 1;
        continue;
      }
      const rowPlatform = explicitPlatform || defaultPolicy.id;
      const targetIndex = explicitPlatform ? 1 : 0;
      const rowPolicy = policies[rowPlatform];
      const target = rowPolicy?.normalizeUrl
        ? rowPolicy.normalizeUrl(fields[targetIndex])
        : sanitizeGenericTargetUrl(fields[targetIndex]);
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
      // An omitted platform means the selected default platform.  Use that
      // resolved identity for de-duplication so an implicit row and an
      // otherwise identical explicit row cannot reach Core twice.
      addLine(sanitizedLine, rowPolicy ? `${rowPlatform}:${target}` : sanitizedLine);
    }
  }

  if (!output.length) {
    throw new TargetImportError(`${errorPrefix}_import_no_targets`);
  }

  const shortLinkCountsByPlatform = {};
  const shortLinkPlatformByRow = new Map();
  output.forEach((line, rowIndex) => {
    const fields = line.split(',');
    const explicitPlatform = fields.length >= 2 && !/^https?:\/\//i.test(fields[0])
      ? String(fields[0] || '').trim().toLowerCase()
      : '';
    const rowPlatform = explicitPlatform || defaultPolicy.id;
    const target = explicitPlatform ? fields[1] : fields[0];
    const rowPolicy = policies[rowPlatform] || (rowPlatform === defaultPolicy.id
      ? defaultPolicy
      : null);
    if (!isShortLinkTarget(target, rowPolicy)) return;
    shortLinkCountsByPlatform[rowPlatform] = (
      shortLinkCountsByPlatform[rowPlatform] || 0
    ) + 1;
    shortLinkPlatformByRow.set(rowIndex, rowPlatform);
  });
  const shortLinkErrorsByPlatform = {};
  for (const [platformId, count] of Object.entries(shortLinkCountsByPlatform)) {
    const rowPolicy = policies[platformId] || (platformId === defaultPolicy.id
      ? defaultPolicy
      : null);
    if (count > shortLinkLimit(rowPolicy)) {
      shortLinkErrorsByPlatform[platformId] = shortLinkErrorCode(rowPolicy);
    }
  }
  const blockedShortLinkRowCount = output.reduce((count, _line, rowIndex) => {
    const platformId = shortLinkPlatformByRow.get(rowIndex);
    return count + (
      platformId && shortLinkErrorsByPlatform[platformId] ? 1 : 0
    );
  }, 0);
  const blockedShortLinkPlatforms = Object.keys(shortLinkErrorsByPlatform);
  for (const platformId of blockedShortLinkPlatforms) {
    delete shortLinkCountsByPlatform[platformId];
  }
  // Keep over-budget rows in the normalized request. Core owns the durable
  // per-row rejection/audit result and already guarantees that it will not
  // resolve redirects for the blocked platform. Dropping them here would
  // make a mixed import appear wholly successful and would destroy retry
  // context if the request later failed.
  const normalizedContent = output.join('\n');
  if (normalizedContent.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new TargetImportError(`${errorPrefix}_import_output_too_large`);
  }
  const shortLinkCount = Object.values(shortLinkCountsByPlatform)
    .reduce((total, count) => total + count, 0);
  return {
    content: normalizedContent,
    converted: isProviderCsv || isDpmsCsv || normalizedContent !== source.trim(),
    discardedSensitiveFields,
    discardedRows,
    shortLinkCount,
    shortLinkCountsByPlatform: Object.freeze({ ...shortLinkCountsByPlatform }),
    shortLinkErrorsByPlatform: Object.freeze({
      ...shortLinkErrorsByPlatform,
    }),
    blockedShortLinkCount: blockedShortLinkRowCount,
    targetCount: output.length,
  };
}

export function normalizeImportWithPolicy(
  policy,
  content,
  options = {},
  policies = {},
  prepared = {},
) {
  const source = String(content || '');
  validateImportSourceSize(policy, source, options, policies, prepared);
  if (policy.structuredTargetImport) {
    return looksLikeStructuredTargetExport(source)
      ? policy.normalizeStructuredImport(source.trim(), options)
      : normalizeDelimitedExport(
        source,
        policy,
        options,
        policies,
        prepared.delimitedRecords,
      );
  }

  if (looksLikeStructuredTargetExport(source)) {
    throw new TargetImportError(
      policy.fallbackStructuredErrorCode || 'target_import_structured_requires_platform',
    );
  }
  if (containsSensitiveExportMarker(source)) {
    if (compatibleMixedRecords(
      source,
      policy,
      options.allowedPlatformIds,
      policies,
      prepared.delimitedRecords,
    )) {
      return normalizeDelimitedExport(
        source,
        policy,
        options,
        policies,
        prepared.delimitedRecords,
      );
    }
    throw new TargetImportError('target_import_sensitive_content_rejected');
  }
  if (source.length > MAX_NORMALIZED_CONTENT_LENGTH) {
    throw new TargetImportError(policy.contentTooLargeErrorCode);
  }
  return normalizeDelimitedExport(
    source,
    policy,
    options,
    policies,
    prepared.delimitedRecords,
  );
}
