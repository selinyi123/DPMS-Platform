import {
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
  actionsFollowPlatformModuleOrder,
  platformLotteryActions,
  platformModeBlocker,
  platformModule,
  platformSourceRuleCorrectionPath,
  platformSupportsExecutionPath,
  platformUsesFixedManualActions,
  platformUsesManualAssistance,
  resolvePlatformExecutionPath,
} from './platforms/index.js';

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
};

const TARGET_ERROR_CODES = new Set([
  'https_required',
]);

const DISPATCH_MODES = new Set(['dry_run', 'shadow_run', 'real_run']);
const TEXT_ACTIONS = new Set(['commented', 'reposted']);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const HANDLE_PATTERN = /^@[\p{L}\p{N}_-]{1,64}$/u;
const MENTION_IN_TEXT_PATTERN = /@[\p{L}\p{N}_-]{1,64}(?![\p{L}\p{N}_-])/gu;
const CONTENT_REQUIREMENT_ACTIONS = ['commented', 'reposted'];
const CONTENT_REQUIREMENT_FIELDS = ['topic_tags', 'mentions'];
const UNBOUND_EXECUTION_EVIDENCE_BLOCKERS = new Set([
  'api_path_probe_evidence_not_implemented',
  'selector_config_evidence_binding_not_implemented',
]);

export function targetTransportCompatibilityIssue(platform, rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || '').trim());
    return parsed.protocol === 'http:' ? 'legacy_http_target' : null;
  } catch {
    return null;
  }
}

export function lotteryActionsForPlatform(platform) {
  return platformLotteryActions(platform);
}

export function isManualAssistedPlatform(platform, executionPathId = '') {
  return platformUsesManualAssistance(platform, executionPathId);
}

export function isManualAssistedPlan(platform, planOrExecutionPath = '') {
  const executionPathId = planOrExecutionPath && typeof planOrExecutionPath === 'object'
    ? planOrExecutionPath.execution_path_id
    : planOrExecutionPath;
  return isManualAssistedPlatform(platform, executionPathId);
}

export function isFixedManualActionPlatform(platform) {
  return platformUsesFixedManualActions(platform);
}

export function platformExecutionPathId(platform, currentExecutionPathId = '') {
  return resolvePlatformExecutionPath(platform, currentExecutionPathId);
}

export function platformDispatchBlocker(platform, mode, executionPathId = '') {
  return platformModeBlocker(platform, mode, executionPathId);
}

export function exactActionPayloadErrors(actions, payloads, platform = 'bilibili') {
  const errors = [];
  const selectedActions = Array.isArray(actions) ? actions : [];
  const sourcePayloads = payloads && typeof payloads === 'object' ? payloads : {};
  const strategy = platformModule(platform)?.strategy;
  if (selectedActions.includes('followed')) {
    const target = sourcePayloads.followed?.target_handle;
    if (!validHandle(target)) errors.push('action_payload_followed_target_invalid');
  }
  for (const action of selectedActions.filter(item => TEXT_ACTIONS.has(item))) {
    const payload = sourcePayloads[action] && typeof sourcePayloads[action] === 'object'
      ? sourcePayloads[action]
      : {};
    const normalizedPolicyPayload = strategy?.normalizeUpdatePayload
      ? strategy.normalizeUpdatePayload(action, payload)
      : payload;
    const text = String(payload.text || '');
    if (
      !text.trim()
      && strategy?.allowsEmptyTextPayload?.({ action, payload: normalizedPolicyPayload }) !== true
    ) errors.push(`action_payload_${action}_text_required`);
    if (!isWellFormedUnicode(text)) errors.push(`action_payload_${action}_text_invalid`);
    if (utf8ByteLength(text) > 4096) errors.push(`action_payload_${action}_text_too_large`);
    errors.push(...(strategy?.validateTextPayload?.({ action, payload, text }) || []));
    for (const field of ['topic_tags', 'mentions', 'media_refs']) {
      const rawValues = payload[field];
      const values = Array.isArray(rawValues) ? rawValues : [];
      if (rawValues !== undefined && !Array.isArray(rawValues)) {
        errors.push(`action_payload_${field}_invalid`);
      }
      if (values.length > 32) errors.push(`action_payload_${field}_too_many`);
      if (values.some(item => (
        !isWellFormedUnicode(item)
        || utf8ByteLength(item) > 512
        || (field === 'mentions' && !validHandle(item))
      ))) errors.push(`action_payload_${field}_invalid`);
    }
    const topicTags = Array.isArray(payload.topic_tags) ? payload.topic_tags : [];
    const mentions = Array.isArray(payload.mentions) ? payload.mentions : [];
    for (const token of topicTags) {
      if (!actionTextContainsRequiredToken(text, token)) {
        errors.push('action_payload_required_token_missing');
      }
    }
    for (const mention of mentions) {
      if (!actionTextContainsRequiredToken(text, mention, { mention: true })) {
        errors.push('action_payload_required_token_missing');
      }
    }
    if (payload.translation !== undefined && payload.translation !== '') {
      if (typeof payload.translation !== 'string' || !payload.translation.trim()) {
        errors.push('action_payload_translation_invalid');
      } else if (!text.includes(payload.translation)) {
        errors.push('action_payload_translation_missing');
      }
    }
  }
  const emptyRequirements = {
    follow_targets: [],
    commented: { topic_tags: [], mentions: [] },
    reposted: { topic_tags: [], mentions: [] },
  };
  errors.push(...(strategy?.validateBindings?.({
    requirements: emptyRequirements,
    sourceRequirements: emptyRequirements,
    payloads: sourcePayloads,
    contentRequirementActions: CONTENT_REQUIREMENT_ACTIONS,
    mentionIdentityKey,
  }) || []));
  return [...new Set(errors)];
}

export function buildActionPlanV2Update({
  platform = 'bilibili',
  requiredActions,
  actionPayloads,
  executionPathId,
  ruleText,
  ruleCompleteConfirmed,
  reviewed,
}) {
  const actions = lotteryActionsForPlatform(platform).filter(action => requiredActions?.includes(action));
  const sourcePayloads = actionPayloads && typeof actionPayloads === 'object' ? actionPayloads : {};
  return {
    required_actions: actions,
    action_payloads: Object.fromEntries(actions.map(action => [
      action,
      normalizedUpdatePayload(platform, action, sourcePayloads[action]),
    ])),
    execution_path_id: executionPathId,
    rule_text: ruleText,
    reviewed: reviewed === true,
    rule_complete_confirmed: ruleCompleteConfirmed === true,
  };
}

function normalizedUpdatePayload(platform, action, value) {
  const payload = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const strategy = platformModule(platform)?.strategy;
  return strategy?.normalizeUpdatePayload
    ? strategy.normalizeUpdatePayload(action, payload)
    : payload;
}

export function realRunEvidencePath(accountId) {
  const value = String(accountId ?? '').trim();
  return value
    ? `/lotteries/real-run/evidence?account_id=${encodeURIComponent(value)}`
    : '/lotteries/real-run/evidence';
}

export function targetValidationErrorCode(value) {
  const text = String(value || '').trim();
  for (const code of TARGET_ERROR_CODES) {
    if (text === code || text.endsWith(`: ${code}`)) return code;
  }
  return null;
}

export function sourceRuleCorrectionPath(platform, sourceType) {
  return platformSourceRuleCorrectionPath(platform, sourceType);
}

export function dispatchSafetyBlocker({
  lottery,
  mode,
  gate,
  safeAccountAvailable,
  accountScopeBound = false,
  accountScopeCompatible = accountScopeBound,
}) {
  const normalizedMode = String(mode || '').trim().toLowerCase();
  if (!lottery?.id) return 'lottery_missing';
  if (!DISPATCH_MODES.has(normalizedMode)) return 'mode_blocked';
  const executionPathId = lottery?.action_plan?.execution_path_id || '';
  const manualAssisted = isManualAssistedPlatform(lottery.platform, executionPathId);
  const platformBlocker = platformDispatchBlocker(lottery.platform, normalizedMode, executionPathId);
  if (platformBlocker) return platformBlocker;
  if (
    normalizedMode !== 'dry_run'
    && targetTransportCompatibilityIssue(lottery.platform, lottery.raw_url)
  ) {
    return 'legacy_http_target';
  }
  if (safeAccountAvailable !== true) return 'no_safe_account';
  if (accountScopeBound === true && accountScopeCompatible !== true) {
    return 'account_credential_kind_mismatch';
  }
  if (normalizedMode !== 'dry_run' && accountScopeBound !== true) return 'account_scope_required';
  const planBlockers = manualAssisted
    ? actionPlanV2ReviewBlockers(lottery.action_plan, lottery.platform)
    : actionPlanV2Blockers(lottery.action_plan, lottery.platform);
  if (normalizedMode !== 'dry_run' && planBlockers.length) {
    return 'action_plan_v2';
  }
  if (normalizedMode === 'real_run' && gate?.allowed !== true) return 'real_run_gate';
  return null;
}

export function evidenceResponseMatchesAccountScope(response, expectedAccountId) {
  const expected = String(expectedAccountId ?? '').trim();
  if (String(response?.selected_account_id ?? '').trim() !== expected) return false;
  const items = Array.isArray(response?.items) ? response.items : [];
  return items.every(item => (
    String(item?.selected_account_id ?? '').trim() === expected
  ));
}

function collectActionPlanV2Blockers(plan, platform, { requireExecutable }) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) return ['action_plan_v2_required'];
  const blockers = [];
  const normalizedPlatform = normalizePlatform(platform);
  const strategy = platformModule(normalizedPlatform)?.strategy;
  if (plan.version !== 2) blockers.push('action_plan_v2_required');
  if (!normalizedPlatform || String(plan.platform || '').trim().toLowerCase() !== normalizedPlatform) {
    blockers.push('action_plan_platform_mismatch');
  }
  const executionPathId = String(plan.execution_path_id || '').trim();
  const expectedExecutionPathId = platformExecutionPathId(normalizedPlatform, executionPathId);
  if (
    !executionPathId
    || !platformSupportsExecutionPath(normalizedPlatform, executionPathId)
    || (expectedExecutionPathId && executionPathId !== expectedExecutionPathId)
  ) blockers.push('execution_path_mismatch');
  const reviewedBy = typeof plan.reviewed_by === 'string' ? plan.reviewed_by : '';
  if (
    plan.review_required !== false
    || !reviewedBy
    || reviewedBy !== reviewedBy.trim()
    || utf8ByteLength(reviewedBy) > 128
  ) blockers.push('action_plan_not_reviewed');
  if (plan.rule_complete_confirmed !== true) blockers.push('rule_completion_not_attested');
  if (requireExecutable && plan.executable !== true) blockers.push('action_plan_not_executable');
  if (!Number.isInteger(plan.rule_snapshot_id) || plan.rule_snapshot_id <= 0) blockers.push('rule_snapshot_missing');
  if (!SHA256_PATTERN.test(String(plan.rule_hash || ''))) blockers.push('rule_hash_missing');
  if (!SHA256_PATTERN.test(String(plan.plan_hash || ''))) blockers.push('action_plan_hash_missing');

  const actions = plan.required_actions;
  const actionsValid = actionsFollowPlatformModuleOrder(
    platformModule(normalizedPlatform),
    actions,
  );
  if (!actionsValid) blockers.push('required_actions_invalid');
  blockers.push(...(strategy?.validatePlan?.({
    plan,
    executionPathId,
    actions,
    actionsValid,
    sameJsonValue,
    sameOrderedList,
  }) || []));

  const payloads = plan.action_payloads;
  if (!payloads || typeof payloads !== 'object' || Array.isArray(payloads)) {
    blockers.push('action_payloads_missing');
    return blockers;
  }
  if (actionsValid) {
    const payloadKeys = Object.keys(payloads).sort();
    const requiredKeys = [...actions].sort();
    if (JSON.stringify(payloadKeys) !== JSON.stringify(requiredKeys)) {
      blockers.push('action_payload_keys_mismatch');
    }
    for (const action of actions) {
      const payload = payloads[action];
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        blockers.push(`action_payload_${action}_invalid`);
      } else if (action === 'followed') {
        const keys = Object.keys(payload);
        if (keys.length !== 1 || keys[0] !== 'target_handle') {
          blockers.push('action_payload_followed_target_required');
        } else if (!validHandle(payload.target_handle)) {
          blockers.push('action_payload_followed_target_invalid');
        }
      } else if (
        TEXT_ACTIONS.has(action)
        && strategy?.allowsEmptyTextPayload?.({ action, payload }) !== true
        && !String(payload.text || '').trim()
      ) {
        blockers.push(`action_payload_${action}_text_required`);
      } else if (!TEXT_ACTIONS.has(action) && Object.keys(payload).length) {
        blockers.push(`action_payload_${action}_must_be_empty`);
      } else if (TEXT_ACTIONS.has(action)) {
        const text = String(payload.text || '');
        const unknownFields = Object.keys(payload).filter(field => ![
          'text', 'topic_tags', 'mentions', 'media_refs', 'translation',
        ].includes(field));
        if (unknownFields.length) blockers.push('action_payload_unknown_field');
        if (!isWellFormedUnicode(text)) blockers.push(`action_payload_${action}_text_invalid`);
        if (utf8ByteLength(text) > 4096) blockers.push(`action_payload_${action}_text_too_large`);
        blockers.push(...(strategy?.validateTextPayload?.({ action, payload, text }) || []));
        for (const field of ['topic_tags', 'mentions', 'media_refs']) {
          const value = payload[field];
          if (Array.isArray(value) && value.length > 32) {
            blockers.push(`action_payload_${field}_too_many`);
          }
          if (value !== undefined && (
            !Array.isArray(value)
            || value.some((item, index) => (
              typeof item !== 'string'
              || !item
              || item !== item.trim()
              || !isWellFormedUnicode(item)
              || utf8ByteLength(item) > 512
              || value.indexOf(item) !== index
              || (field === 'mentions' && !validHandle(item))
            ))
          )) blockers.push(`action_payload_${field}_invalid`);
        }
        const topics = Array.isArray(payload.topic_tags) ? payload.topic_tags : [];
        const mentions = Array.isArray(payload.mentions) ? payload.mentions : [];
        for (const token of topics) {
          if (!actionTextContainsRequiredToken(text, token)) {
            blockers.push('action_payload_required_token_missing');
          }
        }
        for (const mention of mentions) {
          if (!actionTextContainsRequiredToken(text, mention, { mention: true })) {
            blockers.push('action_payload_required_token_missing');
          }
        }
        if (payload.translation !== undefined) {
          if (typeof payload.translation !== 'string' || !payload.translation.trim()) {
            blockers.push('action_payload_translation_invalid');
          } else if (!text.includes(payload.translation)) {
            blockers.push('action_payload_translation_missing');
          }
        }
      }
    }
  }

  const requirements = validatedContentRequirements(plan.content_requirements);
  if (!requirements) {
    blockers.push('action_plan_content_requirements_invalid');
  } else {
    for (const action of CONTENT_REQUIREMENT_ACTIONS) {
      for (const [field, mismatchCode] of [
        ['topic_tags', 'action_plan_required_topic_mismatch'],
        ['mentions', 'action_plan_required_mention_mismatch'],
      ]) {
        const declared = Array.isArray(payloads?.[action]?.[field]) ? payloads[action][field] : [];
        if (!sameOrderedList(declared, requirements[action][field])) blockers.push(mismatchCode);
      }
    }
    if (Array.isArray(actions) && actions.includes('followed')) {
      if (requirements.follow_targets.length !== 1) {
        blockers.push('action_plan_follow_target_source_ambiguous');
      } else if (payloads?.followed?.target_handle !== requirements.follow_targets[0]) {
        blockers.push('action_plan_follow_target_mismatch');
      }
    } else if (requirements.follow_targets.length) {
      blockers.push('action_plan_follow_target_without_action');
    }
  }

  const friendMentionRequirements = validatedFriendMentionRequirements(
    plan.friend_mention_requirements,
  );
  if (!friendMentionRequirements) {
    blockers.push('action_plan_friend_mention_requirements_invalid');
  } else {
    const rawSourceRequirements = plan.source_content_requirements;
    const sourceRequirements = rawSourceRequirements === undefined
      ? (Object.keys(friendMentionRequirements).length ? null : requirements)
      : validatedContentRequirements(rawSourceRequirements);
    if (!sourceRequirements) {
      blockers.push('action_plan_friend_mention_requirement_binding_mismatch');
    } else if (requirements) {
      const boundFollowKeys = new Set(requirements.follow_targets.map(mentionIdentityKey));
      if (sourceRequirements.follow_targets.some(target => (
        !boundFollowKeys.has(mentionIdentityKey(target))
      ))) blockers.push('action_plan_friend_mention_requirement_binding_mismatch');
      for (const action of CONTENT_REQUIREMENT_ACTIONS) {
        const sourceAction = sourceRequirements[action];
        const boundAction = requirements[action];
        const topicsMatch = sameOrderedList(sourceAction.topic_tags, boundAction.topic_tags);
        const boundMentionKeys = boundAction.mentions.map(mentionIdentityKey);
        const mentionsMatch = friendMentionRequirements[action]
          ? (
            new Set(boundMentionKeys).size === boundMentionKeys.length
            && sourceAction.mentions.every(mention => (
              boundMentionKeys.includes(mentionIdentityKey(mention))
            ))
          )
          : sameOrderedList(sourceAction.mentions, boundAction.mentions);
        if (!topicsMatch || !mentionsMatch) {
          blockers.push('action_plan_friend_mention_requirement_binding_mismatch');
        }
      }
      blockers.push(...(strategy?.validateBindings?.({
        requirements,
        sourceRequirements,
        payloads,
        contentRequirementActions: CONTENT_REQUIREMENT_ACTIONS,
        mentionIdentityKey,
      }) || []));
    }
    for (const [action, constraint] of Object.entries(friendMentionRequirements)) {
      if (!Array.isArray(actions) || !actions.includes(action)) {
        blockers.push('action_plan_friend_mention_action_missing');
        continue;
      }
      const mentions = Array.isArray(payloads?.[action]?.mentions)
        ? payloads[action].mentions
        : [];
      const mentionKeys = mentions.map(mentionIdentityKey);
      if (new Set(mentionKeys).size !== mentionKeys.length) {
        blockers.push('action_plan_friend_mention_requirement_binding_mismatch');
      }
      const sourceMentions = new Set(
        (sourceRequirements?.[action]?.mentions || []).map(mentionIdentityKey),
      );
      const sourceFollowTargets = new Set([
        ...(sourceRequirements?.follow_targets || []),
        ...(requirements?.follow_targets || []),
      ].map(mentionIdentityKey));
      const friendCount = mentionKeys.filter(identityKey => (
        !sourceMentions.has(identityKey) && !sourceFollowTargets.has(identityKey)
      )).length;
      if (!friendMentionCountSatisfied(constraint, friendCount)) {
        blockers.push('action_plan_friend_mention_count_mismatch');
      }
      const boundMentions = requirements?.[action]?.mentions;
      if (!Array.isArray(boundMentions) || !sameOrderedList(boundMentions, mentions)) {
        blockers.push('action_plan_friend_mention_requirement_binding_mismatch');
      }
    }
  }
  return [...new Set(blockers)];
}

export function actionPlanV2ReviewBlockers(plan, platform = 'bilibili') {
  const blockers = collectActionPlanV2Blockers(plan, platform, { requireExecutable: false });
  if (
    !isManualAssistedPlatform(platform, plan?.execution_path_id)
    || !plan
    || typeof plan !== 'object'
  ) return blockers;
  const strategy = platformModule(platform)?.strategy;
  const manualReview = strategy?.manualReview;
  if (!manualReview) return blockers;
  const payloadErrors = Array.isArray(plan.payload_validation_errors)
    ? plan.payload_validation_errors.filter(Boolean)
    : [];
  const capabilityBlockers = Array.isArray(plan.capability_blockers)
    ? plan.capability_blockers.filter(Boolean)
    : [];
  const capabilityCode = manualReview.requiredCapabilityBlocker;
  const expectedCapabilityBlockers = new Set(manualReview.expectedCapabilityBlockers);
  const missingManualCapability = capabilityBlockers.includes(capabilityCode)
    ? []
    : [capabilityCode];
  const unexpectedCapabilityBlockers = capabilityBlockers
    .filter(code => !expectedCapabilityBlockers.has(code));
  return [...new Set([
    ...blockers,
    ...payloadErrors,
    ...missingManualCapability,
    ...unexpectedCapabilityBlockers,
  ])];
}

export function actionPlanV2Blockers(plan, platform = 'bilibili') {
  return collectActionPlanV2Blockers(plan, platform, { requireExecutable: true });
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(String(value || '')).length;
}

function isWellFormedUnicode(value) {
  const text = String(value || '');
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    if (unit >= 0xD800 && unit <= 0xDBFF) {
      const next = text.charCodeAt(index + 1);
      if (!(next >= 0xDC00 && next <= 0xDFFF)) return false;
      index += 1;
    } else if (unit >= 0xDC00 && unit <= 0xDFFF) {
      return false;
    }
  }
  return true;
}

export function actionPlanV2Ready(plan, platform = 'bilibili') {
  return actionPlanV2Blockers(plan, platform).length === 0;
}

export function actionPlanV2ReviewReady(plan, platform = 'bilibili') {
  return actionPlanV2ReviewBlockers(plan, platform).length === 0;
}

export function xiaohongshuManualChecklist(plan, platform = 'xiaohongshu') {
  if (normalizePlatform(platform) !== 'xiaohongshu') return [];
  return manualAssistedChecklist(plan, platform);
}

export function manualAssistedChecklist(plan, platform) {
  if (
    !isManualAssistedPlatform(platform, plan?.execution_path_id)
    || !actionPlanV2ReviewReady(plan, platform)
  ) return [];
  const module = platformModule(platform);
  const actions = Array.isArray(plan?.required_actions) ? plan.required_actions : [];
  const payloads = plan?.action_payloads && typeof plan.action_payloads === 'object'
    ? plan.action_payloads
    : {};
  const availableActions = lotteryActionsForPlatform(platform);
  const configuredOrder = Array.isArray(module?.strategy?.manualChecklistActionOrder)
    ? module.strategy.manualChecklistActionOrder
    : [];
  const checklistOrder = [...new Set([...configuredOrder, ...availableActions])]
    .filter(action => availableActions.includes(action));
  const evidenceKeys = module?.strategy?.manualChecklistEvidenceKeys;
  const checklistActions = checklistOrder
    .filter(action => module?.strategy?.manualChecklistAllActions || actions.includes(action));
  return checklistActions.map((action) => {
    const evidenceKey = (
      evidenceKeys
      && typeof evidenceKeys === 'object'
      && typeof evidenceKeys[action] === 'string'
    ) ? evidenceKeys[action] : '';
    return {
      action,
      required: actions.includes(action),
      exactValue: action === 'followed'
        ? String(payloads.followed?.target_handle || '')
        : (TEXT_ACTIONS.has(action) ? String(payloads[action]?.text || '') : ''),
      ...(evidenceKey ? { evidenceKey } : {}),
    };
  });
}

export function manualParticipationConfirmationEnabled(platform) {
  return platformModule(platform)?.strategy?.manualParticipationConfirmation === true;
}

export function manualParticipationIsFinalized(status) {
  return ['participated', 'won', 'lost', 'expired'].includes(
    String(status || '').trim().toLowerCase(),
  );
}

export function manualParticipationCanSubmit(items, confirmedActions, status = '') {
  if (manualParticipationIsFinalized(status)) return false;
  const requiredActions = (Array.isArray(items) ? items : [])
    .filter(item => item?.required === true)
    .map(item => String(item.action || '').trim())
    .filter(Boolean);
  if (!requiredActions.length) return false;
  const confirmed = new Set(
    (Array.isArray(confirmedActions) ? confirmedActions : [])
      .map(action => String(action || '').trim())
      .filter(Boolean),
  );
  return requiredActions.every(action => confirmed.has(action));
}

export function manualParticipationResultNote(platform, items) {
  const actions = (Array.isArray(items) ? items : [])
    .filter(item => item?.required === true)
    .map(item => String(item.action || '').trim())
    .filter(Boolean);
  return [
    'manual_participation_confirmed',
    `platform=${normalizePlatform(platform)}`,
    `actions=${actions.join(',')}`,
  ].join('; ');
}

export function xiaohongshuShadowObservation(gate) {
  return manualShadowObservation(gate);
}

export function manualShadowObservation(gate) {
  return {
    complete: gate?.selector_observation_complete === true,
    taskId: String(gate?.manual_shadow_task_id || ''),
  };
}

export function actionPlanHasMediaRequirement(plan) {
  const payloads = plan?.action_payloads;
  if (!payloads || typeof payloads !== 'object') return false;
  return Object.values(payloads).some(payload => (
    payload && Array.isArray(payload.media_refs) && payload.media_refs.length > 0
  ));
}

export function unresolvedRuleRequirements(rulePlan, actionPayloads) {
  if (!rulePlan || typeof rulePlan !== 'object') return [];
  const requirements = Array.isArray(rulePlan.unsupported_actions)
    ? rulePlan.unsupported_actions
    : [];
  const payloads = actionPayloads && typeof actionPayloads === 'object' ? actionPayloads : {};
  const textPayloads = [payloads.commented, payloads.reposted].filter(Boolean);
  const contentRequirements = validatedContentRequirements(rulePlan.content_requirements);
  if (!contentRequirements) return ['content_requirements_invalid'];
  const friendRequirements = validatedFriendMentionRequirements(
    rulePlan.friend_mention_requirements,
  );
  const friendSourceRequirements = validatedContentRequirements(
    rulePlan.source_content_requirements,
  ) || (rulePlan.version === 1 ? contentRequirements : null);
  const hasListValue = field => textPayloads.some(payload => (
    Array.isArray(payload?.[field]) && payload[field].length > 0
  ));
  const exactActionTokens = field => {
    let anyRequired = false;
    for (const action of CONTENT_REQUIREMENT_ACTIONS) {
      const required = contentRequirements[action][field];
      if (required.length) anyRequired = true;
      const declared = Array.isArray(payloads[action]?.[field]) ? payloads[action][field] : [];
      const allowsBoundFriends = field === 'mentions' && Boolean(friendRequirements?.[action]);
      if (
        allowsBoundFriends
          ? required.some(token => !declared.includes(token))
          : !sameOrderedList(declared, required)
      ) return false;
    }
    return anyRequired;
  };
  const resolved = requirement => {
    if (requirement === 'topic_tag') return exactActionTokens('topic_tags');
    if (requirement === 'mention_account') return exactActionTokens('mentions');
    if (requirement === 'mention_friends') {
      if (!friendRequirements) return false;
      if (
        Array.isArray(rulePlan.unresolved_requirements)
        && rulePlan.unresolved_requirements.includes('mention_friends')
      ) return false;
      return friendMentionRequirementsSatisfied(
        friendRequirements,
        payloads,
        friendSourceRequirements,
      );
    }
    if (requirement === 'favorited') return Boolean(payloads.favorited);
    if (requirement === 'media_submission') return hasListValue('media_refs');
    if (requirement === 'translation_required') {
      return textPayloads.some(payload => payload?.translation !== undefined && payload.translation !== '');
    }
    if (requirement === 'comment_content') return Boolean(String(payloads.commented?.text || '').trim());
    if (requirement === 'repost_content') return Boolean(String(payloads.reposted?.text || '').trim());
    return false;
  };
  const unresolved = requirements.filter(requirement => !resolved(requirement));
  for (const [field, requirement] of [
    ['topic_tags', 'topic_tag'],
    ['mentions', 'mention_account'],
  ]) {
    if (CONTENT_REQUIREMENT_ACTIONS.some(action => {
      const declared = Array.isArray(payloads[action]?.[field]) ? payloads[action][field] : [];
      const required = contentRequirements[action][field];
      return field === 'mentions' && friendRequirements?.[action]
        ? required.some(token => !declared.includes(token))
        : !sameOrderedList(declared, required);
    })) unresolved.push(requirement);
  }
  if (payloads.followed) {
    const followTargets = contentRequirements.follow_targets;
    const targetHandle = payloads.followed.target_handle;
    if (
      followTargets.length > 1
      || (followTargets.length === 1 && targetHandle !== followTargets[0])
      || (followTargets.length === 0 && !validHandle(targetHandle))
    ) unresolved.push('follow_target');
  } else if (contentRequirements.follow_targets.length) {
    unresolved.push('follow_target');
  }
  if (Array.isArray(rulePlan.ambiguity_patterns) && rulePlan.ambiguity_patterns.length) {
    unresolved.push('ambiguous_rule');
  }
  return [...new Set(unresolved)];
}

function validatedFriendMentionRequirements(value) {
  if (value === undefined || value === null) return {};
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const result = {};
  for (const [action, constraint] of Object.entries(value)) {
    if (!CONTENT_REQUIREMENT_ACTIONS.includes(action)) return null;
    if (
      !constraint
      || typeof constraint !== 'object'
      || Array.isArray(constraint)
      || !sameKeySet(constraint, ['mode', 'count'])
      || !['minimum', 'exact'].includes(constraint.mode)
      || !Number.isInteger(constraint.count)
      || constraint.count < 1
      || constraint.count > 32
    ) return null;
    result[action] = { mode: constraint.mode, count: constraint.count };
  }
  return result;
}

function friendMentionCountSatisfied(constraint, count) {
  return constraint.mode === 'exact' ? count === constraint.count : count >= constraint.count;
}

function friendMentionRequirementsSatisfied(requirements, payloads, sourceRequirements = null) {
  const entries = Object.entries(requirements || {});
  if (!entries.length) return false;
  const sourceFollowTargets = new Set(
    (sourceRequirements?.follow_targets || []).map(mentionIdentityKey),
  );
  return entries.every(([action, constraint]) => {
    const mentions = Array.isArray(payloads?.[action]?.mentions)
      ? payloads[action].mentions
      : [];
    const mentionKeys = mentions.map(mentionIdentityKey);
    if (new Set(mentionKeys).size !== mentionKeys.length) return false;
    const sourceMentions = new Set(
      (sourceRequirements?.[action]?.mentions || []).map(mentionIdentityKey),
    );
    const friendCount = sourceRequirements
      ? mentionKeys.filter(identityKey => (
        !sourceMentions.has(identityKey) && !sourceFollowTargets.has(identityKey)
      )).length
      : mentionKeys.length;
    return friendMentionCountSatisfied(constraint, friendCount);
  });
}

export function mentionIdentityKey(value) {
  // ECMAScript has no String#casefold. For the handle alphabet accepted by
  // HANDLE_PATTERN, closing over Unicode upper/lower mappings produces the
  // same caseless equivalence classes as Python's str.casefold(). Dotless i is
  // the one over-merged letter, so protect it with a private-use sentinel
  // (private-use characters are not legal handles) during the closure.
  const dotlessISentinel = '\uE000';
  return String(value || '')
    .normalize('NFKC')
    .replaceAll('\u0131', dotlessISentinel)
    .toLowerCase()
    .toUpperCase()
    .toLowerCase()
    .replaceAll(dotlessISentinel, '\u0131');
}

export function actionTextContainsRequiredToken(text, token, { mention = false } = {}) {
  const content = String(text || '');
  const required = String(token || '');
  if (!mention) return content.includes(required);
  return (content.match(MENTION_IN_TEXT_PATTERN) || []).includes(required);
}

function validHandle(value) {
  return typeof value === 'string'
    && HANDLE_PATTERN.test(value)
    && utf8ByteLength(value) <= 512;
}

function validMetadataList(value, { handlesOnly = false } = {}) {
  if (!Array.isArray(value) || value.length > 32) return false;
  return value.every((item, index) => (
    typeof item === 'string'
    && item.length > 0
    && item === item.trim()
    && utf8ByteLength(item) <= 512
    && value.indexOf(item) === index
    && (!handlesOnly || validHandle(item))
  ));
}

function validatedContentRequirements(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  if (!sameKeySet(value, ['follow_targets', ...CONTENT_REQUIREMENT_ACTIONS])) return null;
  if (!validMetadataList(value.follow_targets, { handlesOnly: true })) return null;
  const normalized = { follow_targets: value.follow_targets };
  for (const action of CONTENT_REQUIREMENT_ACTIONS) {
    const actionRequirements = value[action];
    if (
      !actionRequirements
      || typeof actionRequirements !== 'object'
      || Array.isArray(actionRequirements)
      || !sameKeySet(actionRequirements, CONTENT_REQUIREMENT_FIELDS)
      || !CONTENT_REQUIREMENT_FIELDS.every(field => validMetadataList(
        actionRequirements[field],
        { handlesOnly: field === 'mentions' },
      ))
    ) return null;
    normalized[action] = actionRequirements;
  }
  return normalized;
}

function sameKeySet(value, expected) {
  return JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort());
}

function sameJsonValue(left, right) {
  if (!left || typeof left !== 'object' || Array.isArray(left)) return false;
  return JSON.stringify(sortJsonValue(left)) === JSON.stringify(sortJsonValue(right));
}

function sortJsonValue(value) {
  if (Array.isArray(value)) return value.map(sortJsonValue);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, sortJsonValue(value[key])]),
  );
}

function sameOrderedList(left, right) {
  return Array.isArray(left)
    && Array.isArray(right)
    && left.length === right.length
    && left.every((item, index) => item === right[index]);
}

function normalizePlatform(value) {
  return String(value || '').trim().toLowerCase();
}

export function executionEvidencePresentation(gate) {
  const binding = gate?.execution_evidence
    || gate?.execution_evidence_binding
    || gate?.evidence_binding
    || null;
  const bound = gate?.execution_evidence_bound === true;
  const status = String(
    binding?.status
    || gate?.execution_evidence_status
    || (bound ? 'verified' : 'unbound'),
  ).trim().toLowerCase();
  const explicitReasons = binding?.binding_reasons
    || gate?.execution_evidence_binding_reasons
    || gate?.execution_evidence_reasons;
  const reasons = Array.isArray(explicitReasons)
    ? explicitReasons.filter(Boolean)
    : (bound ? [] : (Array.isArray(gate?.blockers) ? gate.blockers : []));
  return {
    bound,
    status,
    id: binding?.id || binding?.evidence_id || gate?.execution_evidence_id || '',
    executionPathId: binding?.execution_path_id || gate?.execution_path_id || '',
    probeId: binding?.probe_id || gate?.probe_id || '',
    shadowTaskId: binding?.shadow_task_id || gate?.shadow_task_id || '',
    accountId: binding?.account_id || gate?.selected_account_id || '',
    accountScopeMatchesPlatform: gate?.account_scope_matches_platform === true,
    verifiedAt: binding?.verified_at || gate?.execution_evidence_verified_at || '',
    expiresAt: binding?.expires_at || gate?.execution_evidence_expires_at || '',
    reasons: [...new Set(reasons)],
  };
}

export function workflowActivityIdentity(platform, activeProbe, activeShadow) {
  const normalizedPlatform = String(platform || '').trim().toLowerCase() || 'unknown';
  const identities = [];
  if (activeProbe) {
    identities.push(`probe:${activeProbe.probe_id || activeProbe.id || 'unknown'}`);
  }
  if (activeShadow) {
    identities.push(`shadow:${activeShadow.task_id || activeShadow.id || 'unknown'}`);
  }
  return identities.length ? `${normalizedPlatform}:${identities.join('|')}` : '';
}

export function selectorExecutionEvidenceReady(adapter, evidence) {
  const blockers = new Set(Array.isArray(evidence?.blockers) ? evidence.blockers : []);
  if ([...UNBOUND_EXECUTION_EVIDENCE_BLOCKERS].some(code => blockers.has(code))) {
    return false;
  }
  // Readiness is an execution-evidence claim, not merely a selector-config
  // claim. Missing evidence and future/renamed blockers must therefore remain
  // fail closed; the Core's positive probe_ready bit is authoritative here.
  return Boolean(
    evidence?.execution_evidence_bound === true
    && evidence?.target_valid === true
    && evidence?.probe_ready === true
    && (adapter?.configured || evidence?.selector_ready),
  );
}

export function refreshedWorkflowBindings(previousBindings, evidenceItems, probes, taskRuns) {
  const previous = previousBindings instanceof Map ? previousBindings : new Map();
  const evidence = Array.isArray(evidenceItems) ? evidenceItems : [];
  const activeRows = [
    ...(Array.isArray(probes) ? probes : []).filter(row => (
      ['queued', 'running'].includes(row?.status) && row?.platform && row?.lottery_id
    )),
    ...(Array.isArray(taskRuns) ? taskRuns : []).filter(row => (
      row?.task_mode === 'shadow_run'
      && ['queued', 'running'].includes(row?.status)
      && row?.platform
      && row?.lottery_id
    )),
  ];
  const next = new Map();
  const normalizePlatform = value => String(value || '').trim().toLowerCase();
  const sameLottery = (left, right) => String(left) === String(right);

  // A refresh must not switch away from an operation that is still in flight.
  // Otherwise its task disappears from the selected workflow and action
  // controls can reopen against a different lottery on the same platform.
  previous.forEach((lotteryId, platformValue) => {
    const platform = normalizePlatform(platformValue);
    if (platform && activeRows.some(row => (
      normalizePlatform(row.platform) === platform
      && sameLottery(row.lottery_id, lotteryId)
    ))) {
      next.set(platform, lotteryId);
    }
  });

  activeRows.forEach(row => {
    const platform = normalizePlatform(row.platform);
    if (platform && !next.has(platform)) next.set(platform, row.lottery_id);
  });

  evidence.forEach(item => {
    const platform = normalizePlatform(item?.platform);
    if (
      platform
      && item?.lottery_id
      && item.target_valid
      && ['pending', 'claimed'].includes(item.status)
      && !next.has(platform)
    ) {
      next.set(platform, item.lottery_id);
    }
  });
  evidence.forEach(item => {
    const platform = normalizePlatform(item?.platform);
    if (platform && item?.lottery_id && !next.has(platform)) {
      next.set(platform, item.lottery_id);
    }
  });
  return next;
}

export function rulePlanHasUnrepresentableRequirements(plan) {
  if (!plan || typeof plan !== 'object') return false;
  return Boolean(
    (Array.isArray(plan.unsupported_actions) && plan.unsupported_actions.length)
    || (Array.isArray(plan.ambiguity_patterns) && plan.ambiguity_patterns.length)
  );
}
