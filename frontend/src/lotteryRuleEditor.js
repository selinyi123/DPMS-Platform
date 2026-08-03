const HANDLE_PATTERN = /^@[\p{L}\p{N}_-]{1,64}$/u;

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

export function lotteryTargetIdentity(lottery) {
  const source = object(lottery);
  const targetIdentity = object(source.target_identity);
  const author = object(source.author);
  const uid = text(targetIdentity.uid)
    || text(source.author_uid)
    || text(author.uid)
    || null;
  const displayName = text(targetIdentity.display_name)
    || text(source.author_display_name)
    || text(source.author_name)
    || text(author.display_name)
    || text(author.nickname)
    || text(author.name)
    || null;
  return {
    uid,
    displayName,
    profileUrl: text(targetIdentity.profile_url) || text(author.profile_url) || null,
    verified: targetIdentity.verified === true,
    source: text(targetIdentity.source) || null,
  };
}

export function validLotteryHandle(value) {
  return typeof value === 'string' && HANDLE_PATTERN.test(value.trim());
}

export function automaticFollowTarget(lottery, plan = lottery?.action_plan) {
  const sourcePlan = object(plan);
  const requirements = object(sourcePlan.content_requirements);
  const followTargets = Array.isArray(requirements.follow_targets)
    ? requirements.follow_targets.filter(validLotteryHandle)
    : [];
  if (followTargets.length === 1) return followTargets[0].trim();

  const payloadTarget = text(object(sourcePlan.action_payloads).followed?.target_handle);
  if (validLotteryHandle(payloadTarget)) return payloadTarget;

  const legacyTarget = text(sourcePlan.follow_target_handle);
  if (validLotteryHandle(legacyTarget)) return legacyTarget;

  const identity = lotteryTargetIdentity(lottery);
  const identityHandle = identity.displayName ? `@${identity.displayName}` : '';
  return validLotteryHandle(identityHandle) ? identityHandle : '';
}

export function authoritativeRuleText(lottery) {
  const source = object(lottery);
  const snapshot = object(source.rule_snapshot);
  return text(source.rule_text)
    || text(snapshot.rule_text)
    || snapshotText(snapshot.expanded_body)
    || snapshotText(snapshot.body);
}

export function visibleRuleSnapshotParts(lottery) {
  const snapshot = object(object(lottery).rule_snapshot);
  const parts = [];
  const body = snapshotText(snapshot.body);
  const expandedBody = snapshotText(snapshot.expanded_body);
  const pinnedComment = snapshotText(snapshot.pinned_comment);
  if (body) parts.push({ key: 'body', value: body, ...snapshotMetadata(snapshot.body) });
  if (expandedBody && expandedBody !== body) {
    parts.push({
      key: 'expanded_body',
      value: expandedBody,
      ...snapshotMetadata(snapshot.expanded_body),
    });
  }
  if (pinnedComment && pinnedComment !== body && pinnedComment !== expandedBody) {
    parts.push({
      key: 'pinned_comment',
      value: pinnedComment,
      ...snapshotMetadata(snapshot.pinned_comment),
    });
  }
  return parts;
}

function snapshotText(value) {
  return typeof value === 'string' ? text(value) : text(object(value).text);
}

function snapshotMetadata(value) {
  const item = object(value);
  return {
    trusted: item.trusted === true,
    observedAt: text(item.observed_at) || null,
    source: text(item.source) || null,
  };
}

export function defaultRepostText(platform) {
  return String(platform || '').trim().toLowerCase() === 'bilibili' ? '转发动态' : '';
}

export function sourceRequires(rulePlan, requirement) {
  return Array.isArray(rulePlan?.unsupported_actions)
    && rulePlan.unsupported_actions.includes(requirement);
}

export function actionRequirementValues(rulePlan, action, field) {
  const values = rulePlan?.content_requirements?.[action]?.[field];
  return Array.isArray(values)
    ? [...new Set(values.filter(value => typeof value === 'string' && value.trim()).map(value => value.trim()))]
    : [];
}

export function ruleEditorSaveBlockers({
  actions,
  ruleText,
  executionPathId,
  executionPathValid,
  ruleCompleteConfirmed,
  reviewedConfirmed,
  requiredActionSetComplete,
  unresolvedRequirements,
  payloadErrors,
}) {
  const blockers = [];
  if (!Array.isArray(actions) || !actions.length) blockers.push('select_action');
  if (!text(ruleText)) blockers.push('rule_text_missing');
  if (!text(executionPathId)) blockers.push('execution_path_missing');
  else if (!executionPathValid) blockers.push('execution_path_invalid');
  if (!ruleCompleteConfirmed) blockers.push('rule_completion_unconfirmed');
  if (!reviewedConfirmed) blockers.push('plan_review_unconfirmed');
  if (!requiredActionSetComplete) blockers.push('required_action_set_incomplete');
  for (const code of Array.isArray(unresolvedRequirements) ? unresolvedRequirements : []) {
    blockers.push(`requirement:${code}`);
  }
  for (const code of Array.isArray(payloadErrors) ? payloadErrors : []) {
    blockers.push(`payload:${code}`);
  }
  return [...new Set(blockers)];
}
