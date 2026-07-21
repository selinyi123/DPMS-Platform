const TARGET_ERROR_CODES = new Set([
  'https_required',
]);

const DISPATCH_MODES = new Set(['dry_run', 'shadow_run', 'real_run']);
const LOTTERY_ACTIONS = ['followed', 'liked', 'commented', 'reposted'];
const TEXT_ACTIONS = new Set(['commented', 'reposted']);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const HANDLE_PATTERN = /^@[\w\u4e00-\u9fff-]{1,64}$/u;
const CONTENT_REQUIREMENT_ACTIONS = ['commented', 'reposted'];
const CONTENT_REQUIREMENT_FIELDS = ['topic_tags', 'mentions'];
export const BILIBILI_EXECUTION_PATH_ID = 'bilibili_api_v2';
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

export function buildActionPlanV2Update({
  requiredActions,
  actionPayloads,
  executionPathId,
  ruleText,
  ruleCompleteConfirmed,
  reviewed,
}) {
  const actions = LOTTERY_ACTIONS.filter(action => requiredActions?.includes(action));
  const sourcePayloads = actionPayloads && typeof actionPayloads === 'object' ? actionPayloads : {};
  return {
    required_actions: actions,
    action_payloads: Object.fromEntries(actions.map(action => [action, sourcePayloads[action] || {}])),
    execution_path_id: executionPathId,
    rule_text: ruleText,
    reviewed: reviewed === true,
    rule_complete_confirmed: ruleCompleteConfirmed === true,
  };
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
  const normalizedPlatform = String(platform || '').trim().toLowerCase();
  const normalizedSource = String(sourceType || '').trim().toLowerCase();
  if (normalizedPlatform === 'bilibili' && ['up', 'keyword'].includes(normalizedSource)) {
    return 'discovery_refresh';
  }
  return 'unavailable';
}

export function dispatchSafetyBlocker({ lottery, mode, gate, safeAccountAvailable, accountScopeBound = false }) {
  const normalizedMode = String(mode || '').trim().toLowerCase();
  if (!lottery?.id) return 'lottery_missing';
  if (!DISPATCH_MODES.has(normalizedMode)) return 'mode_blocked';
  if (
    normalizedMode !== 'dry_run'
    && targetTransportCompatibilityIssue(lottery.platform, lottery.raw_url)
  ) {
    return 'legacy_http_target';
  }
  if (safeAccountAvailable !== true) return 'no_safe_account';
  if (normalizedMode !== 'dry_run' && accountScopeBound !== true) return 'account_scope_required';
  if (normalizedMode !== 'dry_run' && actionPlanV2Blockers(lottery.action_plan, lottery.platform).length) {
    return 'action_plan_v2';
  }
  if (normalizedMode === 'real_run' && gate?.allowed !== true) return 'real_run_gate';
  return null;
}

export function actionPlanV2Blockers(plan, platform = 'bilibili') {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) return ['action_plan_v2_required'];
  const blockers = [];
  const normalizedPlatform = String(platform || '').trim().toLowerCase();
  if (plan.version !== 2) blockers.push('action_plan_v2_required');
  if (!normalizedPlatform || String(plan.platform || '').trim().toLowerCase() !== normalizedPlatform) {
    blockers.push('action_plan_platform_mismatch');
  }
  const executionPathId = String(plan.execution_path_id || '').trim();
  if (
    !executionPathId
    || (String(platform || '').trim().toLowerCase() === 'bilibili'
      && executionPathId !== BILIBILI_EXECUTION_PATH_ID)
  ) blockers.push('execution_path_mismatch');
  if (plan.review_required !== false) blockers.push('action_plan_not_reviewed');
  if (plan.rule_complete_confirmed !== true) blockers.push('rule_completion_not_attested');
  if (plan.executable !== true) blockers.push('action_plan_not_executable');
  if (!Number.isInteger(plan.rule_snapshot_id) || plan.rule_snapshot_id <= 0) blockers.push('rule_snapshot_missing');
  if (!SHA256_PATTERN.test(String(plan.rule_hash || ''))) blockers.push('rule_hash_missing');
  if (!SHA256_PATTERN.test(String(plan.plan_hash || ''))) blockers.push('action_plan_hash_missing');

  const actions = plan.required_actions;
  const actionsValid = Array.isArray(actions)
    && actions.length > 0
    && actions.every((action, index) => (
      LOTTERY_ACTIONS.includes(action)
      && actions.indexOf(action) === index
      && LOTTERY_ACTIONS.indexOf(action) >= (index ? LOTTERY_ACTIONS.indexOf(actions[index - 1]) : 0)
    ));
  if (!actionsValid) blockers.push('required_actions_invalid');

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
      } else if (TEXT_ACTIONS.has(action) && !String(payload.text || '').trim()) {
        blockers.push(`action_payload_${action}_text_required`);
      } else if (!TEXT_ACTIONS.has(action) && Object.keys(payload).length) {
        blockers.push(`action_payload_${action}_must_be_empty`);
      } else if (TEXT_ACTIONS.has(action)) {
        const text = String(payload.text || '');
        const unknownFields = Object.keys(payload).filter(field => ![
          'text', 'topic_tags', 'mentions', 'media_refs', 'translation',
        ].includes(field));
        if (unknownFields.length) blockers.push('action_payload_unknown_field');
        if (utf8ByteLength(text) > 4096) blockers.push(`action_payload_${action}_text_too_large`);
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
              || utf8ByteLength(item) > 512
              || value.indexOf(item) !== index
            ))
          )) blockers.push(`action_payload_${field}_invalid`);
        }
        const topics = Array.isArray(payload.topic_tags) ? payload.topic_tags : [];
        const mentions = Array.isArray(payload.mentions) ? payload.mentions : [];
        for (const token of [...topics, ...mentions]) {
          if (!text.includes(token)) blockers.push('action_payload_required_token_missing');
        }
        if (payload.translation !== undefined) {
          if (typeof payload.translation !== 'string' || !payload.translation.trim()) {
            blockers.push('action_payload_translation_invalid');
          } else if (!text.includes(payload.translation)) {
            blockers.push('action_payload_translation_missing');
          }
        }
        if (
          normalizedPlatform === 'bilibili'
          && Array.isArray(payload.media_refs)
          && payload.media_refs.length
        ) blockers.push('action_payload_media_unsupported');
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
  return [...new Set(blockers)];
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(String(value || '')).length;
}

export function actionPlanV2Ready(plan, platform = 'bilibili') {
  return actionPlanV2Blockers(plan, platform).length === 0;
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
  const hasListValue = field => textPayloads.some(payload => (
    Array.isArray(payload?.[field]) && payload[field].length > 0
  ));
  const exactActionTokens = field => {
    let anyRequired = false;
    for (const action of CONTENT_REQUIREMENT_ACTIONS) {
      const required = contentRequirements[action][field];
      if (required.length) anyRequired = true;
      const declared = Array.isArray(payloads[action]?.[field]) ? payloads[action][field] : [];
      if (!sameOrderedList(declared, required)) return false;
    }
    return anyRequired;
  };
  const resolved = requirement => {
    if (requirement === 'topic_tag') return exactActionTokens('topic_tags');
    if (requirement === 'mention_account') return exactActionTokens('mentions');
    if (requirement === 'mention_friends') return false;
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
      return !sameOrderedList(declared, contentRequirements[action][field]);
    })) unresolved.push(requirement);
  }
  if (payloads.followed) {
    if (
      contentRequirements.follow_targets.length !== 1
      || payloads.followed.target_handle !== contentRequirements.follow_targets[0]
    ) unresolved.push('follow_target');
  } else if (contentRequirements.follow_targets.length) {
    unresolved.push('follow_target');
  }
  if (Array.isArray(rulePlan.ambiguity_patterns) && rulePlan.ambiguity_patterns.length) {
    unresolved.push('ambiguous_rule');
  }
  return [...new Set(unresolved)];
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
      || !CONTENT_REQUIREMENT_FIELDS.every(field => validMetadataList(actionRequirements[field]))
    ) return null;
    normalized[action] = actionRequirements;
  }
  return normalized;
}

function sameKeySet(value, expected) {
  return JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort());
}

function sameOrderedList(left, right) {
  return Array.isArray(left)
    && Array.isArray(right)
    && left.length === right.length
    && left.every((item, index) => item === right[index]);
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
