import {
  PLATFORM_IDS,
  isRegisteredPlatformId,
  normalizePlatformId,
} from './catalog.js';
import {
  TARGET_IMPORT_FILE_MAX_BYTES,
  TargetImportError,
  dataRecordEntriesForImport,
  isDpmsImportHeader,
  looksLikeStructuredTargetExport,
  normalizeImportWithPolicy,
  normalizedKey,
} from './importShared.js';
import {
  normalizeTargetImportInWorker,
  targetImportNeedsWorker,
  targetImportWorkerSupported,
} from './importWorkerClient.js';

const POLICY_LOADERS = Object.freeze({
  bilibili: () => import('./bilibili/import.js').then(module => module.bilibiliImportPolicy),
  douyin: () => import('./douyin/import.js').then(module => module.douyinImportPolicy),
  weibo: () => import('./weibo/import.js').then(module => module.weiboImportPolicy),
  xiaohongshu: () => import('./xiaohongshu/import.js').then(module => module.xiaohongshuImportPolicy),
});

const policyCache = new Map();
const policyLoads = new Map();
const policyModuleLoadFailures = new WeakSet();
const POLICY_CHUNK_LOAD_ERROR_NAMES = new Set([
  'ChunkLoadError',
  'CSSChunkLoadError',
]);
const POLICY_CHUNK_LOAD_TYPE_ERROR_PATTERNS = Object.freeze([
  /^failed to fetch dynamically imported module(?::|$)/i,
  /^error loading dynamically imported module(?::|$)/i,
  /^importing a module script failed(?:\.|$)/i,
]);

function policyModuleLoadFailure(platformId) {
  const error = new Error('target_import_policy_module_load_failed');
  error.name = 'TargetImportPolicyModuleLoadError';
  error.platformId = platformId;
  policyModuleLoadFailures.add(error);
  return error;
}

export function isTargetImportPolicyModuleLoadFailure(error) {
  return (
    error !== null
    && typeof error === 'object'
    && policyModuleLoadFailures.has(error)
  );
}

export function isProvableTargetImportPolicyChunkLoadFailure(error) {
  if (!error || typeof error !== 'object') return false;
  const name = String(error.name || '');
  if (POLICY_CHUNK_LOAD_ERROR_NAMES.has(name)) return true;
  if (name !== 'TypeError') return false;
  const message = String(error.message || '').trim();
  return POLICY_CHUNK_LOAD_TYPE_ERROR_PATTERNS.some(pattern => (
    pattern.test(message)
  ));
}

export async function loadTargetImportPolicyModule(platformId, loader) {
  try {
    return await loader();
  } catch (error) {
    if (!isProvableTargetImportPolicyChunkLoadFailure(error)) throw error;
    // The original dynamic-import error can contain a deployment asset URL.
    // Keep it inside this worker and expose only a branded, finite signal to
    // the page so stale module graphs can use the bounded reload recovery.
    throw policyModuleLoadFailure(platformId);
  }
}

async function loadPlatformImportPolicy(platform) {
  const platformId = normalizePlatformId(platform);
  const loader = POLICY_LOADERS[platformId];
  if (!loader) throw new Error('target_import_platform_unsupported');
  if (policyCache.has(platformId)) return policyCache.get(platformId);
  if (!policyLoads.has(platformId)) {
    policyLoads.set(platformId, loadTargetImportPolicyModule(platformId, loader).then((policy) => {
      if (!policy || policy.id !== platformId) {
        throw new Error(`target_import_policy_contract_invalid:${platformId}`);
      }
      policyCache.set(platformId, policy);
      return policy;
    }).finally(() => {
      policyLoads.delete(platformId);
    }));
  }
  return policyLoads.get(platformId);
}

function allowedPlatformSet(selectedPlatform, allowedPlatformIds) {
  const allowed = new Set(
    [...(allowedPlatformIds || [])]
      .map(normalizePlatformId)
      .filter(isRegisteredPlatformId),
  );
  allowed.add(selectedPlatform);
  return allowed;
}

function declaredDelimitedPlatforms(
  source,
  selectedPolicy,
  allowedPlatformIds,
) {
  const selectedPlatform = selectedPolicy.id;
  const declared = new Set();
  if (looksLikeStructuredTargetExport(source)) {
    declared.add(selectedPlatform);
    return Object.freeze({ declared, records: null });
  }

  let recordEntries;
  try {
    recordEntries = dataRecordEntriesForImport(source, 'target_import_invalid_csv');
  } catch {
    // The owning policy supplies the stable platform-specific parse error.
    declared.add(selectedPlatform);
    return Object.freeze({ declared, records: null });
  }
  const records = recordEntries.map(entry => entry.fields);
  const isDpmsCsv = isDpmsImportHeader(records[0]);
  const providerDescriptor = !isDpmsCsv
    ? selectedPolicy.providerCsvDescriptor?.(
      (records[0] || []).map(normalizedKey),
    )
    : null;
  if (providerDescriptor) {
    declared.add(selectedPlatform);
    return Object.freeze({ declared, records, recordEntries });
  }
  const targetRecords = isDpmsCsv ? records.slice(1) : records;

  const allowed = allowedPlatformSet(selectedPlatform, allowedPlatformIds);
  for (const platformId of selectedPolicy.compatibilityAllowedPlatformIds || []) {
    const normalized = normalizePlatformId(platformId);
    if (isRegisteredPlatformId(normalized)) allowed.add(normalized);
  }
  for (const fields of targetRecords) {
    const candidate = fields.length >= 2 && !/^https?:\/\//i.test(fields[0])
      ? normalizePlatformId(fields[0])
      : '';
    if (candidate && allowed.has(candidate)) declared.add(candidate);
    else if (!candidate) declared.add(selectedPlatform);
  }
  if (!declared.size) declared.add(selectedPlatform);
  return Object.freeze({ declared, records, recordEntries });
}

export async function normalizeTargetImportWithPolicyLoader(
  platform,
  content,
  options = {},
  loadPolicy,
) {
  const selectedPlatform = normalizePlatformId(platform);
  if (!isRegisteredPlatformId(selectedPlatform)) {
    throw new Error('target_import_platform_unsupported');
  }
  const source = String(content || '');
  const selectedPolicy = await loadPolicy(selectedPlatform);
  const sourceByteLength = new TextEncoder().encode(source).byteLength;
  if (sourceByteLength > TARGET_IMPORT_FILE_MAX_BYTES) {
    throw new TargetImportError('target_import_too_large', {
      byteLength: sourceByteLength,
      maxBytes: TARGET_IMPORT_FILE_MAX_BYTES,
    });
  }
  const prepared = declaredDelimitedPlatforms(
    source,
    selectedPolicy,
    options.allowedPlatformIds,
  );
  const peerPolicyEntries = await Promise.all(
    [...prepared.declared]
      .filter(platformId => platformId !== selectedPlatform)
      .map(async platformId => [
      platformId,
      await loadPolicy(platformId),
      ]),
  );
  const policies = Object.freeze(Object.fromEntries([
    [selectedPlatform, selectedPolicy],
    ...peerPolicyEntries,
  ]));
  return normalizeImportWithPolicy(
    selectedPolicy,
    source,
    options,
    policies,
    {
      delimitedRecords: prepared.records,
      delimitedRecordEntries: prepared.recordEntries,
      sourceByteLength,
    },
  );
}

export async function normalizeTargetImportForPlatformInCurrentThread(
  platform,
  content,
  options = {},
) {
  return normalizeTargetImportWithPolicyLoader(
    platform,
    content,
    options,
    loadPlatformImportPolicy,
  );
}

export async function normalizeTargetImportForPlatform(platform, content, options = {}) {
  if (!targetImportNeedsWorker(content)) {
    return normalizeTargetImportForPlatformInCurrentThread(
      platform,
      content,
      options,
    );
  }
  // Never silently fall back to a large main-thread parse. A browser without
  // module workers receives a recoverable local error and sends nothing to
  // Core.
  if (!targetImportWorkerSupported()) {
    throw new TargetImportError('target_import_worker_unavailable');
  }
  return normalizeTargetImportInWorker(platform, content, options);
}

export function registeredTargetImportPlatformIds() {
  return [...PLATFORM_IDS];
}
