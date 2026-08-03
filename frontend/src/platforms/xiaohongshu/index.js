import {
  XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  XIAOHONGSHU_IMPORT_MAX_BYTES,
  XIAOHONGSHU_EXECUTION_PATH_ID,
  XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
} from '../catalog.js';
import { normalizeDescriptorTargetImport } from '../descriptorImportRuntime.js';

export {
  XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  XIAOHONGSHU_EXECUTION_PATH_ID,
  XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
};

const ACTIONS = Object.freeze(['followed', 'liked', 'commented', 'favorited']);
const MANUAL_CHECKLIST_ACTION_ORDER = Object.freeze([
  'liked',
  'favorited',
  'followed',
  'commented',
]);
const MANUAL_CHECKLIST_EVIDENCE_KEYS = Object.freeze({
  liked: 'lotteries.xiaohongshuManualEvidence.liked',
  favorited: 'lotteries.xiaohongshuManualEvidence.favorited',
  followed: 'lotteries.xiaohongshuManualEvidence.followed',
  commented: 'lotteries.xiaohongshuManualEvidence.commented',
});
const DISCOVERY_SOURCE_TYPES = Object.freeze(['url_list']);
const TARGET_KINDS = Object.freeze(['note']);
const REAL_TARGET_KINDS = TARGET_KINDS;
const EXECUTION_PATHS = Object.freeze([
  XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
]);
const BROWSER_SESSION_KINDS = Object.freeze(['browser_session']);
const EXECUTION_PATH_PRESENTATION = Object.freeze({
  [XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID]: Object.freeze({
    labelKey: 'lotteries.xiaohongshuBrowserExecutionPath',
    hintKey: 'lotteries.xiaohongshuBrowserExecutionPathHint',
  }),
  [XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID]: Object.freeze({
    labelKey: 'lotteries.xiaohongshuManualExecutionPath',
    hintKey: 'lotteries.xiaohongshuManualExecutionPathHint',
  }),
});

export async function normalizeXiaohongshuTargetImport(content, options = {}) {
  return normalizeDescriptorTargetImport(
    'xiaohongshu',
    content,
    options,
  );
}
const EXPECTED_MANUAL_BLOCKERS = Object.freeze([
  'xiaohongshu_manual_execution_selected',
]);
const STRATEGY = Object.freeze({
  manualChecklistAllActions: false,
  manualChecklistActionOrder: MANUAL_CHECKLIST_ACTION_ORDER,
  manualChecklistEvidenceKeys: MANUAL_CHECKLIST_EVIDENCE_KEYS,
  manualParticipationConfirmation: true,
  manualReview: Object.freeze({
    requiredCapabilityBlocker: 'xiaohongshu_manual_execution_selected',
    expectedCapabilityBlockers: EXPECTED_MANUAL_BLOCKERS,
  }),

  normalizeUpdatePayload(_action, payload) {
    return payload;
  },

  validatePlan({ plan, executionPathId }) {
    const blockers = [];
    if (
      executionPathId === XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID
      && plan.executable !== false
    ) {
      blockers.push('xiaohongshu_manual_plan_must_be_non_executable');
    }
    return blockers;
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

export const xiaohongshuPlatformModule = Object.freeze({
  id: 'xiaohongshu',
  actions: ACTIONS,
  discoverySourceTypes: DISCOVERY_SOURCE_TYPES,
  targetKinds: TARGET_KINDS,
  realTargetKinds: REAL_TARGET_KINDS,
  executionPaths: EXECUTION_PATHS,
  executionPathPresentation: EXECUTION_PATH_PRESENTATION,
  defaultExecutionPathId: XIAOHONGSHU_EXECUTION_PATH_ID,
  strategy: STRATEGY,
  fixedManualActions: false,
  importMaxBytes: XIAOHONGSHU_IMPORT_MAX_BYTES,
  importTooLargeErrorCode: 'xiaohongshu_import_too_large',
  structuredTargetImport: true,
  resolveExecutionPath(currentExecutionPathId = '') {
    const current = String(currentExecutionPathId || '').trim();
    return current || XIAOHONGSHU_EXECUTION_PATH_ID;
  },

  isManualAssisted(executionPathId = '') {
    return String(executionPathId || '').trim() === XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID;
  },

  dispatchBlocker(mode, executionPathId = '') {
    if (
      String(executionPathId || '').trim()
      !== XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID
    ) return null;
    const normalizedMode = String(mode || '').trim().toLowerCase();
    if (normalizedMode === 'real_run') return 'xiaohongshu_manual_only';
    if (normalizedMode === 'dry_run') return 'xiaohongshu_manual_shadow_only';
    return null;
  },

  accountCredentialKinds({ executionPathId } = {}) {
    const requestedExecutionPath = String(executionPathId || '').trim();
    if (requestedExecutionPath && !EXECUTION_PATHS.includes(requestedExecutionPath)) {
      return [];
    }
    return BROWSER_SESSION_KINDS;
  },

  sourceRuleCorrectionPath() {
    return 'unavailable';
  },

  async normalizeTargetImport(content, options = {}) {
    return normalizeXiaohongshuTargetImport(content, options);
  },
});
