import assert from 'node:assert/strict';
import test from 'node:test';

import {
  actionPlanHasMediaRequirement,
  actionPlanV2Blockers,
  actionPlanV2Ready,
  buildActionPlanV2Update,
  dispatchSafetyBlocker,
  executionEvidencePresentation,
  realRunEvidencePath,
  refreshedWorkflowBindings,
  rulePlanHasUnrepresentableRequirements,
  selectorExecutionEvidenceReady,
  sourceRuleCorrectionPath,
  targetTransportCompatibilityIssue,
  targetValidationErrorCode,
  unresolvedRuleRequirements,
  workflowActivityIdentity,
} from './lotteryCompatibility.js';

function planV2(overrides = {}) {
  return {
    version: 2,
    platform: 'bilibili',
    execution_path_id: 'bilibili_api_v2',
    rule_snapshot_id: 10,
    rule_hash: 'a'.repeat(64),
    plan_hash: 'b'.repeat(64),
    required_actions: ['followed', 'liked', 'commented', 'reposted'],
    action_payloads: {
      followed: { target_handle: '@账号' },
      liked: {},
      commented: { text: '#话题# @账号 精确评论', topic_tags: ['#话题#'], mentions: ['@账号'] },
      reposted: { text: '精确转发' },
    },
    content_requirements: {
      follow_targets: ['@账号'],
      commented: { topic_tags: ['#话题#'], mentions: ['@账号'] },
      reposted: { topic_tags: [], mentions: [] },
    },
    executable: true,
    review_required: false,
    rule_complete_confirmed: true,
    ...overrides,
  };
}

test('flags HTTP targets for current and future platforms', () => {
  for (const platform of ['bilibili', 'weibo', 'xiaohongshu', 'douyin', 'kuaishou', '']) {
    assert.equal(
      targetTransportCompatibilityIssue(platform, 'HTTP://example.test/activity'),
      'legacy_http_target',
    );
  }
});

test('does not label HTTPS or malformed values as legacy HTTP', () => {
  assert.equal(
    targetTransportCompatibilityIssue('bilibili', 'https://t.bilibili.com/123456789'),
    null,
  );
  assert.equal(targetTransportCompatibilityIssue('bilibili', 'not a URL'), null);
  assert.equal(targetTransportCompatibilityIssue('', 'https://example.test'), null);
});

test('extracts the HTTPS requirement from direct and API error messages', () => {
  assert.equal(targetValidationErrorCode('https_required'), 'https_required');
  assert.equal(targetValidationErrorCode('400: https_required'), 'https_required');
  assert.equal(targetValidationErrorCode('400: invalid_url'), null);
  assert.equal(targetValidationErrorCode(null), null);
});

test('offers refresh correction only for sources that actually refetch rule text', () => {
  assert.equal(sourceRuleCorrectionPath('bilibili', 'up'), 'discovery_refresh');
  assert.equal(sourceRuleCorrectionPath('BILIBILI', 'keyword'), 'discovery_refresh');
  assert.equal(sourceRuleCorrectionPath('bilibili', 'url_list'), 'unavailable');
  assert.equal(sourceRuleCorrectionPath('bilibili', 'manual'), 'unavailable');
  assert.equal(sourceRuleCorrectionPath('weibo', 'keyword'), 'unavailable');
});

test('uses one fail-closed dispatch decision for every UI entry point', () => {
  const lottery = {
    id: 7,
    platform: 'bilibili',
    raw_url: 'https://t.bilibili.com/123456789',
    action_plan: planV2(),
  };
  assert.equal(dispatchSafetyBlocker({ lottery, mode: 'dry_run', safeAccountAvailable: true }), null);
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'real_run', gate: { allowed: true }, safeAccountAvailable: true, accountScopeBound: true }),
    null,
  );
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'real_run', gate: { allowed: false }, safeAccountAvailable: true, accountScopeBound: true }),
    'real_run_gate',
  );
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'shadow_run', safeAccountAvailable: false }),
    'no_safe_account',
  );
  assert.equal(
    dispatchSafetyBlocker({
      lottery: { ...lottery, raw_url: 'http://t.bilibili.com/123456789' },
      mode: 'shadow_run',
      safeAccountAvailable: true,
      accountScopeBound: true,
    }),
    'legacy_http_target',
  );
});

test('legacy, unreviewed, unhashed, and non-executable plans fail closed', () => {
  assert.deepEqual(actionPlanV2Blockers({ version: 1 }), [
    'action_plan_v2_required',
    'action_plan_platform_mismatch',
    'execution_path_mismatch',
    'action_plan_not_reviewed',
    'rule_completion_not_attested',
    'action_plan_not_executable',
    'rule_snapshot_missing',
    'rule_hash_missing',
    'action_plan_hash_missing',
    'required_actions_invalid',
    'action_payloads_missing',
  ]);
  assert.equal(actionPlanV2Ready(planV2()), true);
  assert.equal(actionPlanV2Ready(planV2({ review_required: true })), false);
  assert.equal(actionPlanV2Ready(planV2({ plan_hash: '' })), false);
  assert.equal(actionPlanV2Ready(planV2({ executable: false })), false);
});

test('builds an exact v2 update without carrying payloads for unselected actions', () => {
  assert.deepEqual(buildActionPlanV2Update({
    requiredActions: ['commented', 'liked'],
    actionPayloads: {
      liked: {},
      commented: { text: '#话题# 精确评论', topic_tags: ['#话题#'] },
      reposted: { text: 'must not leak' },
    },
    executionPathId: 'bilibili_api_v2',
    ruleText: '完整规则原文',
    ruleCompleteConfirmed: true,
    reviewed: true,
  }), {
    required_actions: ['liked', 'commented'],
    action_payloads: {
      liked: {},
      commented: { text: '#话题# 精确评论', topic_tags: ['#话题#'] },
    },
    execution_path_id: 'bilibili_api_v2',
    rule_text: '完整规则原文',
    reviewed: true,
    rule_complete_confirmed: true,
  });
});

test('never infers reviewed=true when the explicit review confirmation is absent', () => {
  assert.equal(buildActionPlanV2Update({
    requiredActions: ['liked'],
    actionPayloads: { liked: {} },
    executionPathId: 'bilibili_api_v2',
    ruleText: 'complete source rule',
    ruleCompleteConfirmed: true,
  }).reviewed, false);
});

test('payload keys and exact comment/repost text are part of UI readiness', () => {
  const missingPayload = planV2();
  delete missingPayload.action_payloads.reposted;
  assert.ok(actionPlanV2Blockers(missingPayload).includes('action_payload_keys_mismatch'));
  assert.ok(actionPlanV2Blockers(missingPayload).includes('action_payload_reposted_invalid'));

  const randomFallbackRisk = planV2();
  randomFallbackRisk.action_payloads.commented.text = '   ';
  assert.ok(actionPlanV2Blockers(randomFallbackRisk).includes('action_payload_commented_text_required'));

  const mediaPlan = planV2();
  mediaPlan.action_payloads.commented.media_refs = ['evidence:photo-1'];
  mediaPlan.executable = false;
  assert.equal(actionPlanHasMediaRequirement(mediaPlan), true);
  assert.equal(actionPlanV2Ready(mediaPlan), false);
  assert.ok(actionPlanV2Blockers(mediaPlan).includes('action_payload_media_unsupported'));

  const missingToken = planV2();
  missingToken.action_payloads.commented.topic_tags = ['#另一个话题#'];
  assert.ok(actionPlanV2Blockers(missingToken).includes('action_payload_required_token_missing'));

  const missingTranslation = planV2();
  missingTranslation.action_payloads.commented.translation = 'Cat means 猫';
  assert.ok(actionPlanV2Blockers(missingTranslation).includes('action_payload_translation_missing'));

  const oversized = planV2();
  oversized.action_payloads.commented.text = 'x'.repeat(4097);
  oversized.action_payloads.commented.topic_tags = Array.from({ length: 33 }, (_, index) => `#${index}#`);
  oversized.action_payloads.commented.mentions = [`@${'x'.repeat(512)}`];
  assert.ok(actionPlanV2Blockers(oversized).includes('action_payload_commented_text_too_large'));
  assert.ok(actionPlanV2Blockers(oversized).includes('action_payload_topic_tags_too_many'));
  assert.ok(actionPlanV2Blockers(oversized).includes('action_payload_mentions_invalid'));
});

test('v2 metadata can represent semantic requirements while unknown requirements stay blocked', () => {
  const payloads = planV2().action_payloads;
  payloads.commented.media_refs = ['evidence:photo-1'];
  payloads.commented.translation = 'Cat means 猫';
  assert.deepEqual(
    unresolvedRuleRequirements({
      content_requirements: planV2().content_requirements,
      unsupported_actions: [
        'topic_tag',
        'mention_account',
        'media_submission',
        'translation_required',
        'comment_content',
      ],
    }, payloads),
    [],
  );
  assert.deepEqual(
    unresolvedRuleRequirements({
      content_requirements: planV2().content_requirements,
      unsupported_actions: ['favorited'],
    }, payloads),
    ['favorited'],
  );
  assert.deepEqual(
    unresolvedRuleRequirements({
      content_requirements: planV2().content_requirements,
      ambiguity_patterns: ['任选'],
    }, payloads),
    ['ambiguous_rule'],
  );
});

test('follow and per-action source requirements are exact, not presence checks', () => {
  const wrongFollow = planV2();
  wrongFollow.action_payloads.followed.target_handle = '@另一个账号';
  assert.ok(actionPlanV2Blockers(wrongFollow).includes('action_plan_follow_target_mismatch'));

  const missingFollowTarget = planV2();
  missingFollowTarget.action_payloads.followed = {};
  assert.ok(actionPlanV2Blockers(missingFollowTarget).includes('action_payload_followed_target_required'));

  const wrongCommentTokens = planV2();
  wrongCommentTokens.action_payloads.commented.text = '#任意话题# @任意账号 评论';
  wrongCommentTokens.action_payloads.commented.topic_tags = ['#任意话题#'];
  wrongCommentTokens.action_payloads.commented.mentions = ['@任意账号'];
  const wrongTokenBlockers = actionPlanV2Blockers(wrongCommentTokens);
  assert.ok(wrongTokenBlockers.includes('action_plan_required_topic_mismatch'));
  assert.ok(wrongTokenBlockers.includes('action_plan_required_mention_mismatch'));
  assert.deepEqual(
    unresolvedRuleRequirements({
      content_requirements: wrongCommentTokens.content_requirements,
      unsupported_actions: [],
    }, wrongCommentTokens.action_payloads),
    ['topic_tag', 'mention_account'],
  );

  const wrongActionScope = planV2();
  wrongActionScope.action_payloads.commented = { text: '普通评论' };
  wrongActionScope.action_payloads.reposted = {
    text: '#话题# @账号 转发',
    topic_tags: ['#话题#'],
    mentions: ['@账号'],
  };
  const wrongScopeBlockers = actionPlanV2Blockers(wrongActionScope);
  assert.ok(wrongScopeBlockers.includes('action_plan_required_topic_mismatch'));
  assert.ok(wrongScopeBlockers.includes('action_plan_required_mention_mismatch'));
});

test('legacy or vague content requirement shapes fail closed', () => {
  const missing = planV2();
  delete missing.content_requirements;
  assert.ok(actionPlanV2Blockers(missing).includes('action_plan_content_requirements_invalid'));

  const vague = planV2({
    content_requirements: { topic_tags: ['#话题#'], mentions: ['@账号'] },
  });
  assert.ok(actionPlanV2Blockers(vague).includes('action_plan_content_requirements_invalid'));

  assert.deepEqual(
    unresolvedRuleRequirements({ unsupported_actions: ['topic_tag'] }, planV2().action_payloads),
    ['content_requirements_invalid'],
  );
});

test('real-run UI never trusts a stale allowed gate over the v2 contract', () => {
  const lottery = {
    id: 7,
    platform: 'bilibili',
    raw_url: 'https://t.bilibili.com/123456789',
    action_plan: { version: 1, required_actions: ['liked'], review_required: false },
  };
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'real_run', gate: { allowed: true }, safeAccountAvailable: true, accountScopeBound: true }),
    'action_plan_v2',
  );
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'shadow_run', safeAccountAvailable: true, accountScopeBound: true }),
    'action_plan_v2',
  );
});

test('non-dry workflows require an explicit account evidence scope', () => {
  const lottery = {
    id: 8,
    platform: 'bilibili',
    raw_url: 'https://t.bilibili.com/123456789',
    action_plan: planV2(),
  };
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'shadow_run', safeAccountAvailable: true }),
    'account_scope_required',
  );
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'dry_run', safeAccountAvailable: true }),
    null,
  );
});

test('scopes evidence refreshes to the explicitly selected account', () => {
  assert.equal(realRunEvidencePath('17'), '/lotteries/real-run/evidence?account_id=17');
  assert.equal(realRunEvidencePath(''), '/lotteries/real-run/evidence');
  assert.equal(realRunEvidencePath(null), '/lotteries/real-run/evidence');
});

test('presents verified evidence identity and unbound reasons without exposing files', () => {
  assert.deepEqual(
    executionEvidencePresentation({
      execution_evidence_bound: true,
      execution_evidence: {
        id: 'evidence-1',
        status: 'verified',
        execution_path_id: 'bilibili_api_v2',
        probe_id: 'probe-1',
        shadow_task_id: 'shadow-1',
      },
    }),
    {
      bound: true,
      status: 'verified',
      id: 'evidence-1',
      executionPathId: 'bilibili_api_v2',
      probeId: 'probe-1',
      shadowTaskId: 'shadow-1',
      accountId: '',
      accountScopeMatchesPlatform: false,
      verifiedAt: '',
      expiresAt: '',
      reasons: [],
    },
  );
  assert.deepEqual(
    executionEvidencePresentation({ execution_evidence_bound: false, blockers: ['probe_evidence_required'] }).reasons,
    ['probe_evidence_required'],
  );
});

test('tracks active workflow identity instead of a boolean edge', () => {
  const first = workflowActivityIdentity('bilibili', { probe_id: 'probe-a' }, null);
  const second = workflowActivityIdentity('bilibili', null, { task_id: 'task-b' });
  assert.equal(first, 'bilibili:probe:probe-a');
  assert.equal(second, 'bilibili:shadow:task-b');
  assert.notEqual(first, second);
  assert.equal(workflowActivityIdentity('bilibili', null, null), '');
});

test('configured selectors are not ready while execution evidence is unbound', () => {
  assert.equal(
    selectorExecutionEvidenceReady(
      { configured: true },
      {
        execution_evidence_bound: true,
        target_valid: true,
        probe_ready: true,
        selector_ready: true,
        blockers: ['selector_config_evidence_binding_not_implemented'],
      },
    ),
    false,
  );
  assert.equal(
    selectorExecutionEvidenceReady(
      { configured: true },
      {
        execution_evidence_bound: true,
        target_valid: true,
        probe_ready: true,
        selector_ready: true,
        blockers: [],
      },
    ),
    true,
  );
  assert.equal(selectorExecutionEvidenceReady({ configured: true }, null), false);
  assert.equal(
    selectorExecutionEvidenceReady(
      { configured: true },
      { target_valid: true, probe_ready: false, selector_ready: true, blockers: [] },
    ),
    false,
  );
});

test('keeps a running workflow bound when newer evidence arrives', () => {
  const previous = new Map([['bilibili', 1]]);
  const evidence = [
    { platform: 'bilibili', lottery_id: 2, target_valid: true, status: 'pending' },
    { platform: 'bilibili', lottery_id: 1, target_valid: true, status: 'claimed' },
  ];
  const running = [{
    platform: 'bilibili', lottery_id: 1, probe_id: 'probe-1', status: 'running',
  }];

  assert.equal(refreshedWorkflowBindings(previous, evidence, running, []).get('bilibili'), 1);
  assert.equal(refreshedWorkflowBindings(previous, evidence, [], []).get('bilibili'), 2);
});

test('identifies rule requirements that Action Plan v1 cannot represent', () => {
  assert.equal(rulePlanHasUnrepresentableRequirements({ unsupported_actions: ['topic_tag'] }), true);
  assert.equal(rulePlanHasUnrepresentableRequirements({ ambiguity_patterns: ['任选'] }), true);
  assert.equal(rulePlanHasUnrepresentableRequirements({ required_actions: ['liked'] }), false);
});
