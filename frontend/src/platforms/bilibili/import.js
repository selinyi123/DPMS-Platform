import {
  TARGET_IMPORT_PASSTHROUGH_MAX_BYTES,
  sanitizeGenericTargetUrl,
} from '../importShared.js';

// The generic line importer predates platform modules and exposed XHS-prefixed
// CSV errors. Keep those codes stable while Bilibili owns the compatibility
// policy that selects them.
export const bilibiliImportPolicy = Object.freeze({
  id: 'bilibili',
  maxBytes: TARGET_IMPORT_PASSTHROUGH_MAX_BYTES,
  tooLargeErrorCode: 'target_import_too_large',
  contentTooLargeErrorCode: 'target_import_content_too_large',
  delimitedErrorPrefix: 'xiaohongshu',
  fallbackStructuredErrorCode: 'target_import_structured_requires_platform',
  compatibilityAllowedPlatformIds: Object.freeze(['xiaohongshu']),
  shortLinkHosts: Object.freeze(['b23.tv']),
  shortLinkLimit: 1,
  // Preserve the legacy generic-import error code exposed by this policy.
  shortLinkErrorCode: 'xiaohongshu_import_short_link_batch_unsupported',
  structuredTargetImport: false,
  normalizeUrl: sanitizeGenericTargetUrl,
  looksLikeStructuredExport() {
    return false;
  },
  looksLikeDelimitedExport() {
    return false;
  },
});
