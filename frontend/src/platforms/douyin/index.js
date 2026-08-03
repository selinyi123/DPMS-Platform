import {
  DOUYIN_DEVICE_EXECUTION_PATH_ID,
  DOUYIN_EXECUTION_PATH_ID,
  DOUYIN_IMPORT_MAX_BYTES,
  DOUYIN_MANUAL_EXECUTION_PATH_ID,
} from '../catalog.js';
import { normalizeDescriptorTargetImport } from '../descriptorImportRuntime.js';

export {
  DOUYIN_DEVICE_EXECUTION_PATH_ID,
  DOUYIN_EXECUTION_PATH_ID,
  DOUYIN_MANUAL_EXECUTION_PATH_ID,
};

const ACTIONS = Object.freeze(['followed', 'liked', 'commented', 'favorited']);
const DISCOVERY_SOURCE_TYPES = Object.freeze(['url_list']);
const TARGET_KINDS = Object.freeze(['video', 'note']);
const REAL_TARGET_KINDS = TARGET_KINDS;
const EXECUTION_PATHS = Object.freeze([
  DOUYIN_DEVICE_EXECUTION_PATH_ID,
  DOUYIN_MANUAL_EXECUTION_PATH_ID,
]);
const BROWSER_SESSION_KINDS = Object.freeze(['browser_session']);
const DEVICE_AGENT_KINDS = Object.freeze(['device_agent']);
const EXECUTION_PATH_PRESENTATION = Object.freeze({
  [DOUYIN_DEVICE_EXECUTION_PATH_ID]: Object.freeze({
    labelKey: 'lotteries.douyinDeviceExecutionPath',
    hintKey: 'lotteries.douyinDeviceExecutionPathHint',
  }),
  [DOUYIN_MANUAL_EXECUTION_PATH_ID]: Object.freeze({
    labelKey: 'lotteries.douyinManualExecutionPath',
    hintKey: 'lotteries.douyinManualExecutionPathHint',
  }),
});

export async function normalizeDouyinTargetImport(content, options = {}) {
  return normalizeDescriptorTargetImport(
    'douyin',
    content,
    options,
  );
}
const EXPECTED_MANUAL_BLOCKERS = Object.freeze([
  'douyin_no_official_interaction_api',
  'douyin_manual_only',
]);

const STRATEGY = Object.freeze({
  manualChecklistAllActions: false,
  manualReview: Object.freeze({
    requiredCapabilityBlocker: 'douyin_no_official_interaction_api',
    expectedCapabilityBlockers: EXPECTED_MANUAL_BLOCKERS,
  }),

  normalizeUpdatePayload(_action, payload) {
    return payload;
  },

  validatePlan({ plan, executionPathId }) {
    return (
      executionPathId === DOUYIN_MANUAL_EXECUTION_PATH_ID
      && plan.executable !== false
    ) ? ['douyin_manual_plan_must_be_non_executable'] : [];
  },

  allowsEmptyTextPayload() {
    return false;
  },

  validateTextPayload() {
    return [];
  },

  validateBindings() {
    return [];
  },
});

export const douyinPlatformModule = Object.freeze({
  id: 'douyin',
  actions: ACTIONS,
  discoverySourceTypes: DISCOVERY_SOURCE_TYPES,
  targetKinds: TARGET_KINDS,
  realTargetKinds: REAL_TARGET_KINDS,
  executionPaths: EXECUTION_PATHS,
  executionPathPresentation: EXECUTION_PATH_PRESENTATION,
  defaultExecutionPathId: DOUYIN_EXECUTION_PATH_ID,
  strategy: STRATEGY,
  fixedManualActions: false,
  importMaxBytes: DOUYIN_IMPORT_MAX_BYTES,
  importTooLargeErrorCode: 'douyin_import_too_large',
  structuredTargetImport: true,
  resolveExecutionPath(currentExecutionPathId = '') {
    const current = String(currentExecutionPathId || '').trim();
    return current || DOUYIN_EXECUTION_PATH_ID;
  },

  isManualAssisted(executionPathId = '') {
    return String(executionPathId || '').trim() === DOUYIN_MANUAL_EXECUTION_PATH_ID;
  },

  dispatchBlocker(mode, executionPathId = '') {
    if (
      String(executionPathId || '').trim()
      !== DOUYIN_MANUAL_EXECUTION_PATH_ID
    ) return null;
    const normalizedMode = String(mode || '').trim().toLowerCase();
    if (normalizedMode === 'real_run') return 'douyin_manual_only';
    if (normalizedMode === 'dry_run') return 'douyin_manual_shadow_only';
    return null;
  },

  accountCredentialKinds({ executionPathId } = {}) {
    const requestedExecutionPath = String(executionPathId || '').trim();
    if (requestedExecutionPath === DOUYIN_DEVICE_EXECUTION_PATH_ID) {
      return DEVICE_AGENT_KINDS;
    }
    if (requestedExecutionPath === DOUYIN_MANUAL_EXECUTION_PATH_ID) {
      return BROWSER_SESSION_KINDS;
    }
    return [];
  },

  sourceRuleCorrectionPath() {
    return 'unavailable';
  },

  async normalizeTargetImport(content, options = {}) {
    return normalizeDouyinTargetImport(content, options);
  },
});
