import {
  WEIBO_IMPORT_MAX_BYTES,
  WEIBO_MANUAL_EXECUTION_PATH_ID,
  WEIBO_MAX_UNIQUE_HANDLES,
  WEIBO_OAUTH_EXECUTION_PATH_ID,
} from '../catalog.js';
import { normalizeDescriptorTargetImport } from '../descriptorImportRuntime.js';

export {
  WEIBO_MANUAL_EXECUTION_PATH_ID,
  WEIBO_MAX_UNIQUE_HANDLES,
  WEIBO_OAUTH_EXECUTION_PATH_ID,
};

const ACTIONS = Object.freeze(['followed', 'liked', 'commented', 'favorited', 'reposted']);
const DISCOVERY_SOURCE_TYPES = Object.freeze(['url_list']);
const TARGET_KINDS = Object.freeze(['status']);
const REAL_TARGET_KINDS = TARGET_KINDS;
const EXECUTION_PATHS = Object.freeze([
  WEIBO_OAUTH_EXECUTION_PATH_ID,
  WEIBO_MANUAL_EXECUTION_PATH_ID,
]);
const BROWSER_SESSION_KINDS = Object.freeze(['browser_session']);
const OAUTH_KINDS = Object.freeze(['weibo_oauth']);
const EXPECTED_MANUAL_BLOCKERS = Object.freeze(['weibo_manual_execution_selected']);
const EXECUTION_PATH_PRESENTATION = Object.freeze({
  [WEIBO_OAUTH_EXECUTION_PATH_ID]: Object.freeze({
    labelKey: 'lotteries.weiboOAuthExecutionPath',
    hintKey: 'lotteries.weiboOAuthExecutionPathHint',
  }),
  [WEIBO_MANUAL_EXECUTION_PATH_ID]: Object.freeze({
    labelKey: 'lotteries.weiboManualExecutionPath',
    hintKey: 'lotteries.weiboManualExecutionPathHint',
  }),
});

export async function normalizeWeiboTargetImport(content, options = {}) {
  return normalizeDescriptorTargetImport(
    'weibo',
    content,
    options,
  );
}

export const WEIBO_ACTION_CAPABILITY_REQUIREMENTS = Object.freeze({
  followed: Object.freeze({ endpoint: 'friendships/create', permission: 'advanced', client_type: 'weibo' }),
  liked: Object.freeze({ endpoint: 'attitudes/create', permission: 'advanced' }),
  commented: Object.freeze({ endpoint: 'comments/create', permission: 'standard' }),
  favorited: Object.freeze({ endpoint: 'favorites/create', permission: 'standard' }),
  reposted: Object.freeze({ endpoint: 'statuses/repost', permission: 'standard' }),
});

function normalizeOptionalRepostPayload(action, payload) {
  if (action !== 'reposted') return payload;
  const hasExactText = Boolean(String(payload.text || '').trim());
  const hasMetadata = ['topic_tags', 'mentions', 'media_refs'].some(field => (
    Array.isArray(payload[field]) && payload[field].length
  )) || Boolean(String(payload.translation || '').trim());
  return hasExactText || hasMetadata ? payload : {};
}

const STRATEGY = Object.freeze({
  manualChecklistAllActions: false,
  manualReview: Object.freeze({
    requiredCapabilityBlocker: 'weibo_manual_execution_selected',
    expectedCapabilityBlockers: EXPECTED_MANUAL_BLOCKERS,
  }),

  normalizeUpdatePayload(action, payload) {
    return normalizeOptionalRepostPayload(action, payload);
  },

  validatePlan({ plan, executionPathId, actions, sameJsonValue }) {
    const blockers = [];
    if (executionPathId === WEIBO_MANUAL_EXECUTION_PATH_ID && plan.executable !== false) {
      blockers.push('weibo_manual_plan_must_be_non_executable');
    }
    const expectedRuntimeRequirements = executionPathId === WEIBO_OAUTH_EXECUTION_PATH_ID
      ? {
        contract_version: 1,
        actions: Object.fromEntries(
          (Array.isArray(actions) ? actions : [])
            .filter(action => WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action])
            .map(action => [action, WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action]]),
        ),
      }
      : {};
    if (!sameJsonValue(plan.runtime_capability_requirements ?? {}, expectedRuntimeRequirements)) {
      blockers.push('weibo_oauth_capability_contract_mismatch');
    }
    return blockers;
  },

  allowsEmptyTextPayload({ action, payload }) {
    return action === 'reposted' && Object.keys(payload).length === 0;
  },

  validateTextPayload({ action, text }) {
    return text.length > 140 ? [`weibo_${action}_text_too_long`] : [];
  },

  validateBindings({
    requirements,
    sourceRequirements,
    payloads,
    contentRequirementActions,
    mentionIdentityKey,
  }) {
    const handles = [
      ...requirements.follow_targets,
      ...sourceRequirements.follow_targets,
      ...(payloads?.followed?.target_handle ? [payloads.followed.target_handle] : []),
    ];
    for (const action of contentRequirementActions) {
      const payloadMentions = Array.isArray(payloads?.[action]?.mentions)
        ? payloads[action].mentions
        : [];
      handles.push(
        ...requirements[action].mentions,
        ...sourceRequirements[action].mentions,
        ...payloadMentions,
      );
    }
    return new Set(handles.map(mentionIdentityKey)).size > WEIBO_MAX_UNIQUE_HANDLES
      ? ['weibo_preflight_unique_handle_limit_exceeded']
      : [];
  },
});

export const weiboPlatformModule = Object.freeze({
  id: 'weibo',
  actions: ACTIONS,
  discoverySourceTypes: DISCOVERY_SOURCE_TYPES,
  targetKinds: TARGET_KINDS,
  realTargetKinds: REAL_TARGET_KINDS,
  executionPaths: EXECUTION_PATHS,
  executionPathPresentation: EXECUTION_PATH_PRESENTATION,
  defaultExecutionPathId: WEIBO_OAUTH_EXECUTION_PATH_ID,
  strategy: STRATEGY,
  fixedManualActions: false,
  importMaxBytes: WEIBO_IMPORT_MAX_BYTES,
  importTooLargeErrorCode: 'weibo_import_too_large',
  structuredTargetImport: true,
  actionCapabilityRequirements: WEIBO_ACTION_CAPABILITY_REQUIREMENTS,

  resolveExecutionPath(currentExecutionPathId = '') {
    const current = String(currentExecutionPathId || '').trim();
    return current || WEIBO_OAUTH_EXECUTION_PATH_ID;
  },

  isManualAssisted(executionPathId = '') {
    return String(executionPathId || '').trim() === WEIBO_MANUAL_EXECUTION_PATH_ID;
  },

  dispatchBlocker(mode, executionPathId = '') {
    if (String(executionPathId || '').trim() !== WEIBO_MANUAL_EXECUTION_PATH_ID) return null;
    const normalizedMode = String(mode || '').trim().toLowerCase();
    if (normalizedMode === 'real_run') return 'weibo_manual_only';
    if (normalizedMode === 'dry_run') return 'weibo_manual_shadow_only';
    return null;
  },

  accountCredentialKinds({ mode, executionPathId } = {}) {
    // Shadow observation always uses the browser adapter, even when the saved
    // execution strategy is OAuth. Dry/real OAuth work must never receive a
    // browser-session credential.
    const requestedExecutionPath = String(executionPathId || '').trim();
    if (requestedExecutionPath && !EXECUTION_PATHS.includes(requestedExecutionPath)) return [];
    if (String(mode || '').trim().toLowerCase() === 'shadow_run') {
      return BROWSER_SESSION_KINDS;
    }
    const normalizedExecutionPath = requestedExecutionPath || WEIBO_OAUTH_EXECUTION_PATH_ID;
    return normalizedExecutionPath === WEIBO_OAUTH_EXECUTION_PATH_ID
      ? OAUTH_KINDS
      : BROWSER_SESSION_KINDS;
  },

  sourceRuleCorrectionPath() {
    return 'unavailable';
  },

  async normalizeTargetImport(content, options = {}) {
    return normalizeWeiboTargetImport(content, options);
  },
});
