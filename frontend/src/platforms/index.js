import {
  BILIBILI_EXECUTION_PATH_ID,
  DOUYIN_DEVICE_EXECUTION_PATH_ID,
  DOUYIN_EXECUTION_PATH_ID,
  DOUYIN_MANUAL_EXECUTION_PATH_ID,
  PLATFORM_IDS,
  WEIBO_MANUAL_EXECUTION_PATH_ID,
  WEIBO_MAX_UNIQUE_HANDLES,
  WEIBO_OAUTH_EXECUTION_PATH_ID,
  XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  XIAOHONGSHU_EXECUTION_PATH_ID,
  XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
  normalizePlatformId,
} from './catalog.js';
import { normalizeTargetImportForPlatform } from './importRuntime.js';

export {
  TARGET_IMPORT_FILE_MAX_BYTES,
  TargetImportError,
} from './importShared.js';
export {
  TARGET_IMPORT_WORKER_THRESHOLD_BYTES,
  readTargetImportFile,
} from './importWorkerClient.js';

export {
  BILIBILI_EXECUTION_PATH_ID,
  DOUYIN_DEVICE_EXECUTION_PATH_ID,
  DOUYIN_EXECUTION_PATH_ID,
  DOUYIN_MANUAL_EXECUTION_PATH_ID,
  WEIBO_MANUAL_EXECUTION_PATH_ID,
  WEIBO_MAX_UNIQUE_HANDLES,
  WEIBO_OAUTH_EXECUTION_PATH_ID,
  XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  XIAOHONGSHU_EXECUTION_PATH_ID,
  XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
  normalizePlatformId,
};

const PLATFORM_LOADERS = Object.freeze({
  bilibili: () => import('./bilibili/index.js').then(module => module.bilibiliPlatformModule),
  douyin: () => import('./douyin/index.js').then(module => module.douyinPlatformModule),
  weibo: () => import('./weibo/index.js').then(module => module.weiboPlatformModule),
  xiaohongshu: () => import('./xiaohongshu/index.js').then(module => module.xiaohongshuPlatformModule),
});

const loadedPlatformModules = new Map();
const platformModuleLoads = new Map();
const platformModuleFailures = new Map();
export const PLATFORM_MODULE_LOAD_TIMEOUT_MS = 15_000;

export function loadPlatformModuleBounded(
  platformId,
  loader,
  timeoutMs = PLATFORM_MODULE_LOAD_TIMEOUT_MS,
) {
  const boundedTimeout = (
    Number.isSafeInteger(timeoutMs)
    && timeoutMs > 0
    && timeoutMs <= 60_000
  ) ? timeoutMs : PLATFORM_MODULE_LOAD_TIMEOUT_MS;
  let timeoutId;
  const timeout = new Promise((_resolve, reject) => {
    timeoutId = setTimeout(() => {
      const error = new Error('platform_module_load_timeout');
      error.code = 'platform_module_load_timeout';
      error.platformId = platformId;
      reject(error);
    }, boundedTimeout);
  });
  return Promise.race([
    Promise.resolve().then(loader),
    timeout,
  ]).finally(() => {
    clearTimeout(timeoutId);
  });
}

function normalizedRegisteredPlatformIds(platformIds) {
  return [...new Set(
    [...platformIds].map(normalizePlatformId).filter(platformId => PLATFORM_IDS.includes(platformId)),
  )];
}

export async function loadPlatformModule(
  platform,
  {
    retry = false,
    timeoutMs = PLATFORM_MODULE_LOAD_TIMEOUT_MS,
  } = {},
) {
  const platformId = normalizePlatformId(platform);
  const loader = PLATFORM_LOADERS[platformId];
  if (!loader) throw new Error('platform_module_unsupported');
  if (loadedPlatformModules.has(platformId)) return loadedPlatformModules.get(platformId);
  if (retry) platformModuleFailures.delete(platformId);
  if (platformModuleFailures.has(platformId)) throw platformModuleFailures.get(platformId);
  if (!platformModuleLoads.has(platformId)) {
    platformModuleLoads.set(platformId, loadPlatformModuleBounded(
      platformId,
      loader,
      timeoutMs,
    ).then((module) => {
      if (!module || module.id !== platformId) {
        throw new Error(`platform_module_contract_invalid:${platformId}`);
      }
      loadedPlatformModules.set(platformId, module);
      platformModuleFailures.delete(platformId);
      return module;
    }).catch((error) => {
      platformModuleFailures.set(platformId, error);
      throw error;
    }).finally(() => {
      platformModuleLoads.delete(platformId);
    }));
  }
  return platformModuleLoads.get(platformId);
}

export function loadPlatformModulesIndependently(
  platformIds = PLATFORM_IDS,
  {
    loadModule = loadPlatformModule,
    onSettled = () => {},
  } = {},
) {
  return normalizedRegisteredPlatformIds(platformIds).map(platformId => (
    Promise.resolve()
      .then(() => loadModule(platformId))
      .then((module) => {
        const result = Object.freeze({ platformId, status: 'fulfilled', module });
        onSettled(result);
        return result;
      })
      .catch((error) => {
        const result = Object.freeze({ platformId, status: 'rejected', error });
        onSettled(result);
        return result;
      })
  ));
}

export async function settlePlatformModuleLoads(
  platformIds = PLATFORM_IDS,
  loadModule = loadPlatformModule,
) {
  const uniquePlatformIds = normalizedRegisteredPlatformIds(platformIds);
  const settled = await Promise.allSettled(
    uniquePlatformIds.map(platformId => loadModule(platformId)),
  );
  const modules = [];
  const failures = {};
  settled.forEach((result, index) => {
    const platformId = uniquePlatformIds[index];
    if (result.status === 'fulfilled') modules.push(result.value);
    else failures[platformId] = result.reason;
  });
  return Object.freeze({ modules: Object.freeze(modules), failures: Object.freeze(failures) });
}

export async function loadRegisteredPlatformModules(platformIds = PLATFORM_IDS) {
  return settlePlatformModuleLoads(platformIds);
}

export function platformModuleLoadFailures() {
  return Object.freeze(Object.fromEntries(platformModuleFailures));
}

export function platformModuleLoadState(platform) {
  const platformId = normalizePlatformId(platform);
  if (!PLATFORM_IDS.includes(platformId)) {
    return Object.freeze({ platformId, status: 'unsupported', error: null });
  }
  if (loadedPlatformModules.has(platformId)) {
    return Object.freeze({ platformId, status: 'ready', error: null });
  }
  if (platformModuleLoads.has(platformId)) {
    return Object.freeze({ platformId, status: 'loading', error: null });
  }
  if (platformModuleFailures.has(platformId)) {
    return Object.freeze({
      platformId,
      status: 'failed',
      error: platformModuleFailures.get(platformId),
    });
  }
  return Object.freeze({ platformId, status: 'not_loaded', error: null });
}

export function platformModule(platform) {
  return loadedPlatformModules.get(normalizePlatformId(platform)) || null;
}

export function registeredPlatformModules() {
  return PLATFORM_IDS.map(platformModule).filter(Boolean);
}

export function registeredPlatformIds() {
  return [...PLATFORM_IDS];
}

export function platformLotteryActions(platform) {
  return [...(platformModule(platform)?.actions || [])];
}

export function actionsFollowPlatformModuleOrder(moduleOrPlatform, actions) {
  const module = typeof moduleOrPlatform === 'string'
    ? platformModule(moduleOrPlatform)
    : moduleOrPlatform;
  if (!module || !Array.isArray(module.actions) || !Array.isArray(actions) || !actions.length) {
    return false;
  }
  if (new Set(actions).size !== actions.length || actions.some(action => !module.actions.includes(action))) {
    return false;
  }
  const expectedOrder = module.actions.filter(action => actions.includes(action));
  return expectedOrder.length === actions.length
    && expectedOrder.every((action, index) => actions[index] === action);
}

export function platformDiscoverySourceTypes(platform) {
  return [...(platformModule(platform)?.discoverySourceTypes || [])];
}

export function platformRealTargetKinds(platform) {
  return [...(platformModule(platform)?.realTargetKinds || [])];
}

export function platformSupportsDiscoverySource(platform, sourceType) {
  const normalizedSourceType = String(sourceType || '').trim().toLowerCase();
  return platformModule(platform)?.discoverySourceTypes.includes(normalizedSourceType) === true;
}

export function platformSourceRuleCorrectionPath(platform, sourceType) {
  return platformModule(platform)?.sourceRuleCorrectionPath(sourceType) || 'unavailable';
}

export function resolvePlatformExecutionPath(platform, currentExecutionPathId = '') {
  const module = platformModule(platform);
  return module
    ? module.resolveExecutionPath(currentExecutionPathId)
    : String(currentExecutionPathId || '').trim();
}

export function platformExecutionPaths(platform) {
  return [...(platformModule(platform)?.executionPaths || [])];
}

export function platformSupportsExecutionPath(platform, executionPathId) {
  const current = String(executionPathId || '').trim();
  return Boolean(current) && platformExecutionPaths(platform).includes(current);
}

export function platformExecutionPathPresentation(platform, executionPathId) {
  return platformModule(platform)?.executionPathPresentation?.[executionPathId] || null;
}

export function platformUsesManualAssistance(platform, executionPathId = '') {
  const module = platformModule(platform);
  const current = String(executionPathId || '').trim() || module?.defaultExecutionPathId || '';
  if (!module || !module.executionPaths.includes(current)) return false;
  return module.isManualAssisted(current) === true;
}

export function platformUsesFixedManualActions(platform) {
  return platformModule(platform)?.fixedManualActions === true;
}

export function platformModeBlocker(platform, mode, executionPathId = '') {
  const module = platformModule(platform);
  if (!module) return 'platform_module_unavailable';
  const current = String(executionPathId || '').trim();
  if (current && !module.executionPaths.includes(current)) return 'execution_path_mismatch';
  return module.dispatchBlocker(mode, current || module.defaultExecutionPathId) || null;
}

export function platformImportMaxBytes(platform) {
  return platformModule(platform)?.importMaxBytes || 0;
}

export function platformImportTooLargeErrorCode(platform) {
  return platformModule(platform)?.importTooLargeErrorCode || 'target_import_too_large';
}

export function platformSupportsStructuredTargetImport(platform) {
  return platformModule(platform)?.structuredTargetImport === true;
}

export async function normalizePlatformTargetImport(platform, content, options = {}) {
  const normalizedOptions = options && typeof options === 'object' ? options : {};
  // Import compatibility belongs to the frontend's compiled platform
  // registry. A transient /accounts/platforms failure must not reinterpret
  // explicit rows from a registered peer as unsupported and discard them.
  const allowedPlatformIds = [...new Set([
    ...PLATFORM_IDS,
    ...(Array.isArray(normalizedOptions.allowedPlatformIds)
      ? normalizedOptions.allowedPlatformIds
      : []),
  ])];
  return normalizeTargetImportForPlatform(platform, content, {
    ...normalizedOptions,
    allowedPlatformIds,
  });
}

export function normalizedAccountCredentialKind(account) {
  const explicit = String(account?.credential_kind || '').trim().toLowerCase();
  // Older Core responses did not include credential_kind. They only carried
  // browser sessions, so preserving that interpretation is backward-compatible.
  return explicit || 'browser_session';
}

export function platformAccountCredentialKinds(platform, mode, executionPathId = '') {
  const module = platformModule(platform);
  if (!module) return [];
  const current = String(executionPathId || '').trim();
  if (current && !module.executionPaths.includes(current)) return [];
  const resolvedExecutionPath = current || module.defaultExecutionPathId;
  if (module.dispatchBlocker(mode, resolvedExecutionPath)) return [];
  return [...(module.accountCredentialKinds({
    mode,
    executionPathId: resolvedExecutionPath,
  }) || [])];
}

export function accountMatchesPlatformDispatch(account, platform, mode, executionPathId = '') {
  if (!account || normalizePlatformId(account.platform) !== normalizePlatformId(platform)) return false;
  return platformAccountCredentialKinds(platform, mode, executionPathId)
    .includes(normalizedAccountCredentialKind(account));
}

export function eligibleAccountsForPlatformDispatch(accounts, platform, mode, executionPathId = '') {
  return (Array.isArray(accounts) ? accounts : []).filter(account => (
    accountMatchesPlatformDispatch(account, platform, mode, executionPathId)
  ));
}

function accountIndexKey(platform, credentialKind) {
  return JSON.stringify([normalizePlatformId(platform), String(credentialKind || '').trim().toLowerCase()]);
}

export function buildPlatformAccountCredentialIndex(accounts) {
  const counts = Object.create(null);
  for (const account of Array.isArray(accounts) ? accounts : []) {
    const key = accountIndexKey(account?.platform, normalizedAccountCredentialKind(account));
    counts[key] = (counts[key] || 0) + 1;
  }
  return Object.freeze(counts);
}

export function eligibleAccountCountForPlatformDispatch(
  accountIndex,
  platform,
  mode,
  executionPathId = '',
) {
  const source = accountIndex && typeof accountIndex === 'object' ? accountIndex : {};
  return platformAccountCredentialKinds(platform, mode, executionPathId)
    .reduce((total, credentialKind) => (
      total + Number(source[accountIndexKey(platform, credentialKind)] || 0)
    ), 0);
}

export function hasEligibleAccountForPlatformDispatch(
  accountIndex,
  platform,
  mode,
  executionPathId = '',
) {
  return eligibleAccountCountForPlatformDispatch(
    accountIndex,
    platform,
    mode,
    executionPathId,
  ) > 0;
}
