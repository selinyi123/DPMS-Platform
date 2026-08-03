// Backward-compatible public facade. Platform-specific import rules now live
// beside their owning platform modules under ./platforms/*/import.js.
import { bilibiliImportPolicy } from './platforms/bilibili/import.js';
import { PLATFORM_IMPORT_POLICIES } from './platforms/importPolicies.js';
import {
  TARGET_IMPORT_PASSTHROUGH_MAX_BYTES,
  TargetImportError,
  looksLikeStructuredTargetExport,
  normalizeImportWithPolicy,
} from './platforms/importShared.js';

export {
  DOUYIN_IMPORT_MAX_BYTES,
  WEIBO_IMPORT_MAX_BYTES,
  XIAOHONGSHU_IMPORT_MAX_BYTES,
} from './platforms/catalog.js';
export {
  TARGET_IMPORT_PASSTHROUGH_MAX_BYTES,
  TargetImportError,
  looksLikeStructuredTargetExport,
};

// Compatibility alias for the original Xiaohongshu-only importer API.
export const XiaohongshuImportError = TargetImportError;

function normalizeLegacyPlatformImport(platform, content, options = {}) {
  return normalizeImportWithPolicy(
    PLATFORM_IMPORT_POLICIES[platform],
    content,
    options,
    PLATFORM_IMPORT_POLICIES,
  );
}

export function normalizeBilibiliTargetImport(content, options = {}) {
  return normalizeLegacyPlatformImport('bilibili', content, options);
}

export function normalizeDouyinTargetImport(content, options = {}) {
  return normalizeLegacyPlatformImport('douyin', content, options);
}

export function normalizeWeiboTargetImport(content, options = {}) {
  return normalizeLegacyPlatformImport('weibo', content, options);
}

export function normalizeXiaohongshuTargetImport(content, options = {}) {
  return normalizeLegacyPlatformImport('xiaohongshu', content, options);
}

export function normalizeTargetImportForPlatform(platform, content, options = {}) {
  const platformKey = String(platform || '').trim().toLowerCase();
  const registeredPolicy = PLATFORM_IMPORT_POLICIES[platformKey];
  if (registeredPolicy) {
    return normalizeImportWithPolicy(
      registeredPolicy,
      content,
      options,
      PLATFORM_IMPORT_POLICIES,
    );
  }
  // Preserve the old facade's generic behavior for unknown platform strings;
  // the new platform registry itself remains fail-closed for application use.
  return normalizeImportWithPolicy(
    Object.freeze({ ...bilibiliImportPolicy, id: platformKey }),
    content,
    options,
    PLATFORM_IMPORT_POLICIES,
  );
}
