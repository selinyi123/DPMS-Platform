import { BILIBILI_EXECUTION_PATH_ID } from '../catalog.js';
import { normalizeDescriptorTargetImport } from '../descriptorImportRuntime.js';
import { TARGET_IMPORT_PASSTHROUGH_MAX_BYTES } from '../importShared.js';

export { BILIBILI_EXECUTION_PATH_ID };

const ACTIONS = Object.freeze(['followed', 'liked', 'commented', 'reposted']);
const DISCOVERY_SOURCE_TYPES = Object.freeze(['url_list', 'keyword', 'up']);
const TARGET_KINDS = Object.freeze(['dynamic', 'video', 'article']);
const REAL_TARGET_KINDS = Object.freeze(['dynamic']);
const EXECUTION_PATHS = Object.freeze([BILIBILI_EXECUTION_PATH_ID]);
const BROWSER_SESSION_KINDS = Object.freeze(['browser_session']);

export async function normalizeBilibiliTargetImport(content, options = {}) {
  return normalizeDescriptorTargetImport(
    'bilibili',
    content,
    options,
  );
}

const STRATEGY = Object.freeze({
  manualChecklistAllActions: false,

  normalizeUpdatePayload(_action, payload) {
    return payload;
  },

  validatePlan() {
    return [];
  },

  allowsEmptyTextPayload() {
    return false;
  },

  validateTextPayload({ payload }) {
    return Array.isArray(payload.media_refs) && payload.media_refs.length
      ? ['action_payload_media_unsupported']
      : [];
  },

  validateBindings() {
    return [];
  },
});

export const bilibiliPlatformModule = Object.freeze({
  id: 'bilibili',
  actions: ACTIONS,
  discoverySourceTypes: DISCOVERY_SOURCE_TYPES,
  targetKinds: TARGET_KINDS,
  realTargetKinds: REAL_TARGET_KINDS,
  executionPaths: EXECUTION_PATHS,
  defaultExecutionPathId: BILIBILI_EXECUTION_PATH_ID,
  strategy: STRATEGY,
  fixedManualActions: false,
  importMaxBytes: TARGET_IMPORT_PASSTHROUGH_MAX_BYTES,
  importTooLargeErrorCode: 'target_import_content_too_large',
  structuredTargetImport: false,

  resolveExecutionPath(currentExecutionPathId = '') {
    const current = String(currentExecutionPathId || '').trim();
    return current || BILIBILI_EXECUTION_PATH_ID;
  },

  isManualAssisted() {
    return false;
  },

  dispatchBlocker() {
    return null;
  },

  accountCredentialKinds() {
    return BROWSER_SESSION_KINDS;
  },

  sourceRuleCorrectionPath(sourceType) {
    return ['keyword', 'up'].includes(String(sourceType || '').trim().toLowerCase())
      ? 'discovery_refresh'
      : 'unavailable';
  },

  async normalizeTargetImport(content, options = {}) {
    return normalizeBilibiliTargetImport(content, options);
  },
});
