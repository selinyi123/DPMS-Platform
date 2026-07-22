import assert from 'node:assert/strict';
import test from 'node:test';

import {
  actionPlanHasMediaRequirement,
  actionPlanV2Blockers,
  actionPlanV2Ready,
  actionPlanV2ReviewBlockers,
  actionPlanV2ReviewReady,
  buildActionPlanV2Update,
  dispatchSafetyBlocker,
  executionEvidencePresentation,
  isFixedManualActionPlatform,
  isManualAssistedPlan,
  isManualAssistedPlatform,
  lotteryActionsForPlatform,
  manualAssistedChecklist,
  manualShadowObservation,
  platformDispatchBlocker,
  platformExecutionPathId,
  realRunEvidencePath,
  refreshedWorkflowBindings,
  rulePlanHasUnrepresentableRequirements,
  selectorExecutionEvidenceReady,
  sourceRuleCorrectionPath,
  targetTransportCompatibilityIssue,
  targetValidationErrorCode,
  unresolvedRuleRequirements,
  workflowActivityIdentity,
  xiaohongshuManualChecklist,
  xiaohongshuShadowObservation,
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
    reviewed_by: 'operator-1',
    rule_complete_confirmed: true,
    ...overrides,
  };
}

function xiaohongshuPlanV2(overrides = {}) {
  return {
    version: 2,
    platform: 'xiaohongshu',
    execution_path_id: 'xiaohongshu_manual_v1',
    rule_snapshot_id: 11,
    rule_hash: 'c'.repeat(64),
    plan_hash: 'd'.repeat(64),
    required_actions: ['followed', 'liked', 'commented', 'favorited'],
    action_payloads: {
      followed: { target_handle: '@creator' },
      liked: {},
      commented: { text: '#giveaway# exact comment', topic_tags: ['#giveaway#'] },
      favorited: {},
    },
    content_requirements: {
      follow_targets: ['@creator'],
      commented: { topic_tags: ['#giveaway#'], mentions: [] },
      reposted: { topic_tags: [], mentions: [] },
    },
    executable: false,
    review_required: false,
    reviewed_by: 'operator-1',
    rule_complete_confirmed: true,
    capability_blockers: ['xiaohongshu_no_official_interaction_api'],
    ...overrides,
  };
}

function douyinPlanV2(overrides = {}) {
  return {
    version: 2,
    platform: 'douyin',
    execution_path_id: 'douyin_manual_v1',
    rule_snapshot_id: 12,
    rule_hash: 'e'.repeat(64),
    plan_hash: 'f'.repeat(64),
    required_actions: ['followed', 'liked', 'commented', 'favorited'],
    action_payloads: {
      followed: { target_handle: '@creator' },
      liked: {},
      commented: { text: '#抽奖# 精确评论', topic_tags: ['#抽奖#'] },
      favorited: {},
    },
    content_requirements: {
      follow_targets: ['@creator'],
      commented: { topic_tags: ['#抽奖#'], mentions: [] },
      reposted: { topic_tags: [], mentions: [] },
    },
    executable: false,
    review_required: false,
    reviewed_by: 'operator-1',
    rule_complete_confirmed: true,
    capability_blockers: ['douyin_no_official_interaction_api'],
    ...overrides,
  };
}

function weiboPlanV2(overrides = {}) {
  const manual = overrides.execution_path_id === 'weibo_manual_v1';
  const requiredActions = overrides.required_actions || [
    'followed', 'liked', 'commented', 'favorited', 'reposted',
  ];
  return {
    version: 2,
    platform: 'weibo',
    execution_path_id: manual ? 'weibo_manual_v1' : 'weibo_oauth_v1',
    rule_snapshot_id: 13,
    rule_hash: '1'.repeat(64),
    plan_hash: '2'.repeat(64),
    required_actions: requiredActions,
    action_payloads: {
      followed: { target_handle: '@official' },
      liked: {},
      commented: {
        text: '#giveaway# @friend1 @friend2',
        topic_tags: ['#giveaway#'],
        mentions: ['@friend1', '@friend2'],
      },
      favorited: {},
      reposted: {},
    },
    content_requirements: {
      follow_targets: ['@official'],
      commented: { topic_tags: ['#giveaway#'], mentions: ['@friend1', '@friend2'] },
      reposted: { topic_tags: [], mentions: [] },
    },
    source_content_requirements: {
      follow_targets: ['@official'],
      commented: { topic_tags: ['#giveaway#'], mentions: [] },
      reposted: { topic_tags: [], mentions: [] },
    },
    friend_mention_requirements: {
      commented: { mode: 'minimum', count: 2 },
    },
    runtime_capability_requirements: manual ? {} : {
      contract_version: 1,
      actions: {
        followed: {
          endpoint: 'friendships/create', permission: 'advanced', client_type: 'weibo',
        },
        liked: { endpoint: 'attitudes/create', permission: 'advanced' },
        commented: { endpoint: 'comments/create', permission: 'standard' },
        favorited: { endpoint: 'favorites/create', permission: 'standard' },
        reposted: { endpoint: 'statuses/repost', permission: 'standard' },
      },
    },
    executable: !manual,
    review_required: false,
    reviewed_by: 'operator-1',
    rule_complete_confirmed: true,
    capability_blockers: manual ? ['weibo_manual_execution_selected'] : [],
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

test('uses the exact Xiaohongshu four-action contract and manual execution path', () => {
  assert.deepEqual(
    lotteryActionsForPlatform('xiaohongshu'),
    ['followed', 'liked', 'commented', 'favorited'],
  );
  assert.deepEqual(
    lotteryActionsForPlatform('bilibili'),
    ['followed', 'liked', 'commented', 'reposted'],
  );
  assert.equal(isManualAssistedPlatform('XIAOHONGSHU'), true);
  assert.equal(isManualAssistedPlatform('bilibili'), false);
  assert.equal(platformExecutionPathId('xiaohongshu'), 'xiaohongshu_manual_v1');
  assert.equal(platformExecutionPathId('bilibili'), 'bilibili_api_v2');
  assert.equal(platformExecutionPathId('weibo', 'weibo-browser-v1'), 'weibo_oauth_v1');
});

test('Weibo selects a capability-bound OAuth path with an explicit manual fallback', () => {
  assert.deepEqual(lotteryActionsForPlatform('weibo'), [
    'followed', 'liked', 'commented', 'favorited', 'reposted',
  ]);
  assert.equal(platformExecutionPathId('weibo'), 'weibo_oauth_v1');
  assert.equal(platformExecutionPathId('weibo', 'weibo_manual_v1'), 'weibo_manual_v1');
  assert.equal(isManualAssistedPlatform('weibo'), false);
  assert.equal(isManualAssistedPlatform('weibo', 'weibo_manual_v1'), true);
  assert.equal(isManualAssistedPlan('weibo', weiboPlanV2()), false);
  assert.equal(isManualAssistedPlan('weibo', weiboPlanV2({
    execution_path_id: 'weibo_manual_v1',
  })), true);

  assert.deepEqual(actionPlanV2Blockers(weiboPlanV2(), 'weibo'), []);
  const manual = weiboPlanV2({ execution_path_id: 'weibo_manual_v1' });
  assert.deepEqual(actionPlanV2ReviewBlockers(manual, 'weibo'), []);
  assert.equal(actionPlanV2ReviewReady(manual, 'weibo'), true);
  assert.equal(platformDispatchBlocker('weibo', 'real_run', 'weibo_manual_v1'), 'weibo_manual_only');
  assert.equal(platformDispatchBlocker('weibo', 'shadow_run', 'weibo_manual_v1'), null);
  assert.ok(actionPlanV2ReviewBlockers({
    ...manual,
    executable: true,
  }, 'weibo').includes('weibo_manual_plan_must_be_non_executable'));
});

test('enforces Weibo 140 UTF-16-unit text limits without truncating emoji', () => {
  const base = weiboPlanV2();
  const exactly140 = weiboPlanV2({
    action_payloads: {
      ...base.action_payloads,
      commented: { text: 'a'.repeat(140) },
    },
    content_requirements: {
      ...base.content_requirements,
      commented: { topic_tags: [], mentions: [] },
    },
    source_content_requirements: {
      ...base.source_content_requirements,
      commented: { topic_tags: [], mentions: [] },
    },
    friend_mention_requirements: {},
  });
  assert.equal(
    actionPlanV2Blockers(exactly140, 'weibo').includes('weibo_commented_text_too_long'),
    false,
  );

  const exactly141 = weiboPlanV2({
    ...exactly140,
    action_payloads: {
      ...exactly140.action_payloads,
      commented: { text: 'a'.repeat(141) },
    },
  });
  assert.ok(actionPlanV2Blockers(exactly141, 'weibo')
    .includes('weibo_commented_text_too_long'));

  const emojiBoundary = weiboPlanV2({
    ...exactly140,
    action_payloads: {
      ...exactly140.action_payloads,
      commented: { text: `${'a'.repeat(139)}🎉` },
    },
  });
  assert.equal(emojiBoundary.action_payloads.commented.text.length, 141);
  assert.ok(actionPlanV2Blockers(emojiBoundary, 'weibo')
    .includes('weibo_commented_text_too_long'));

  const malformedUnicode = weiboPlanV2({
    ...exactly140,
    action_payloads: {
      ...exactly140.action_payloads,
      commented: { text: '\uD800' },
    },
  });
  assert.ok(actionPlanV2Blockers(malformedUnicode, 'weibo')
    .includes('action_payload_commented_text_invalid'));
});

test('Weibo plan validation keeps friend-count and OAuth capability contracts fail closed', () => {
  const missingFriend = weiboPlanV2({
    action_payloads: {
      ...weiboPlanV2().action_payloads,
      commented: {
        text: '#giveaway# @friend1',
        topic_tags: ['#giveaway#'],
        mentions: ['@friend1'],
      },
    },
    content_requirements: {
      ...weiboPlanV2().content_requirements,
      commented: { topic_tags: ['#giveaway#'], mentions: ['@friend1'] },
    },
  });
  assert.ok(actionPlanV2Blockers(missingFriend, 'weibo')
    .includes('action_plan_friend_mention_count_mismatch'));

  const exactTooMany = weiboPlanV2({
    friend_mention_requirements: { commented: { mode: 'exact', count: 1 } },
  });
  assert.ok(actionPlanV2Blockers(exactTooMany, 'weibo')
    .includes('action_plan_friend_mention_count_mismatch'));

  const malformed = weiboPlanV2({
    friend_mention_requirements: { commented: { mode: 'at_least', count: 2 } },
  });
  assert.ok(actionPlanV2Blockers(malformed, 'weibo')
    .includes('action_plan_friend_mention_requirements_invalid'));

  const wrongCapability = weiboPlanV2({ runtime_capability_requirements: {} });
  assert.ok(actionPlanV2Blockers(wrongCapability, 'weibo')
    .includes('weibo_oauth_capability_contract_mismatch'));

  const brandPlusExactFriends = weiboPlanV2({
    required_actions: ['commented'],
    action_payloads: {
      commented: {
        text: '@brand @friend1 @friend2 @friend3',
        mentions: ['@brand', '@friend1', '@friend2', '@friend3'],
      },
    },
    content_requirements: {
      follow_targets: [],
      commented: { topic_tags: [], mentions: ['@brand', '@friend1', '@friend2', '@friend3'] },
      reposted: { topic_tags: [], mentions: [] },
    },
    source_content_requirements: {
      follow_targets: [],
      commented: { topic_tags: [], mentions: ['@brand'] },
      reposted: { topic_tags: [], mentions: [] },
    },
    friend_mention_requirements: { commented: { mode: 'exact', count: 3 } },
    runtime_capability_requirements: {
      contract_version: 1,
      actions: {
        commented: { endpoint: 'comments/create', permission: 'standard' },
      },
    },
  });
  assert.deepEqual(actionPlanV2Blockers(brandPlusExactFriends, 'weibo'), []);

  const missingSourceBinding = weiboPlanV2();
  delete missingSourceBinding.source_content_requirements;
  assert.ok(actionPlanV2Blockers(missingSourceBinding, 'weibo')
    .includes('action_plan_friend_mention_requirement_binding_mismatch'));

  const duplicateIdentity = weiboPlanV2({
    action_payloads: {
      ...weiboPlanV2().action_payloads,
      commented: {
        text: '@Alice @alice',
        mentions: ['@Alice', '@alice'],
      },
    },
    content_requirements: {
      ...weiboPlanV2().content_requirements,
      commented: { topic_tags: [], mentions: ['@Alice', '@alice'] },
    },
    source_content_requirements: {
      ...weiboPlanV2().source_content_requirements,
      commented: { topic_tags: [], mentions: [] },
    },
    friend_mention_requirements: { commented: { mode: 'exact', count: 2 } },
  });
  assert.ok(actionPlanV2Blockers(duplicateIdentity, 'weibo')
    .includes('action_plan_friend_mention_requirement_binding_mismatch'));

  const fullwidthAt = weiboPlanV2({
    action_payloads: {
      ...weiboPlanV2().action_payloads,
      commented: { text: '＠friend', mentions: ['＠friend'] },
    },
    content_requirements: {
      ...weiboPlanV2().content_requirements,
      commented: { topic_tags: [], mentions: ['＠friend'] },
    },
    source_content_requirements: {
      ...weiboPlanV2().source_content_requirements,
      commented: { topic_tags: [], mentions: [] },
    },
    friend_mention_requirements: { commented: { mode: 'exact', count: 1 } },
  });
  assert.ok(actionPlanV2Blockers(fullwidthAt, 'weibo')
    .includes('action_payload_mentions_invalid'));
});

test('Weibo friend rules become represented only after action-scoped handles are bound', () => {
  const suggestion = {
    version: 1,
    content_requirements: {
      follow_targets: ['@official'],
      commented: { topic_tags: [], mentions: ['@official'] },
      reposted: { topic_tags: [], mentions: [] },
    },
    friend_mention_requirements: { commented: { mode: 'minimum', count: 2 } },
    unsupported_actions: ['mention_friends'],
  };
  assert.deepEqual(unresolvedRuleRequirements(suggestion, {
    followed: { target_handle: '@official' },
    commented: { text: '@official @friend1', mentions: ['@official', '@friend1'] },
  }), ['mention_friends']);
  assert.deepEqual(unresolvedRuleRequirements(suggestion, {
    followed: { target_handle: '@official' },
    commented: {
      text: '@official @friend1 @friend2',
      mentions: ['@official', '@friend1', '@friend2'],
    },
  }), []);
});

test('Weibo mention requirements reject handle-prefix collisions in reviewed text', () => {
  const base = weiboPlanV2();
  const prefixCollision = weiboPlanV2({
    action_payloads: {
      ...base.action_payloads,
      commented: {
        ...base.action_payloads.commented,
        text: '#giveaway# @friend12 @friend2',
      },
    },
  });
  assert.ok(actionPlanV2Blockers(prefixCollision, 'weibo')
    .includes('action_payload_required_token_missing'));

  const exactTokens = weiboPlanV2({
    action_payloads: {
      ...base.action_payloads,
      commented: {
        ...base.action_payloads.commented,
        text: '#giveaway# @friend1，@friend2。',
      },
    },
  });
  assert.equal(actionPlanV2Blockers(exactTokens, 'weibo')
    .includes('action_payload_required_token_missing'), false);
});

test('Weibo plans cap distinct resolved handles across all actions', () => {
  const base = weiboPlanV2();
  const commentMentions = Array.from({ length: 16 }, (_, index) => `@c${index}`);
  const repostMentions = Array.from({ length: 16 }, (_, index) => `@r${index}`);
  const overLimit = weiboPlanV2({
    action_payloads: {
      ...base.action_payloads,
      commented: { text: commentMentions.join(' '), mentions: commentMentions },
      reposted: { text: repostMentions.join(' '), mentions: repostMentions },
    },
    content_requirements: {
      follow_targets: ['@official'],
      commented: { topic_tags: [], mentions: commentMentions },
      reposted: { topic_tags: [], mentions: repostMentions },
    },
    source_content_requirements: {
      follow_targets: ['@official'],
      commented: { topic_tags: [], mentions: commentMentions },
      reposted: { topic_tags: [], mentions: repostMentions },
    },
    friend_mention_requirements: {},
  });
  assert.ok(actionPlanV2Blockers(overLimit, 'weibo')
    .includes('weibo_preflight_unique_handle_limit_exceeded'));
});

test('uses a variable, semantically distinct Douyin manual action contract', () => {
  assert.deepEqual(lotteryActionsForPlatform('douyin'), [
    'followed',
    'liked',
    'commented',
    'favorited',
    'reposted',
  ]);
  assert.equal(isManualAssistedPlatform('DOUYIN'), true);
  assert.equal(isFixedManualActionPlatform('douyin'), false);
  assert.equal(isFixedManualActionPlatform('xiaohongshu'), true);
  assert.equal(platformExecutionPathId('douyin', 'bilibili_api_v2'), 'douyin_manual_v1');

  const update = buildActionPlanV2Update({
    platform: 'douyin',
    requiredActions: ['favorited', 'reposted', 'commented'],
    actionPayloads: {
      commented: { text: '精确评论' },
      favorited: {},
      reposted: { text: '精确分享文案' },
    },
    executionPathId: 'douyin_manual_v1',
    ruleText: '评论、收藏并转发',
    ruleCompleteConfirmed: true,
    reviewed: true,
  });
  assert.deepEqual(update.required_actions, ['commented', 'favorited', 'reposted']);
  assert.deepEqual(Object.keys(update.action_payloads), ['commented', 'favorited', 'reposted']);
});

test('Douyin is reviewable but automatic dispatch remains permanently unavailable', () => {
  const plan = douyinPlanV2();
  assert.equal(actionPlanV2ReviewReady(plan, 'douyin'), true);
  assert.equal(actionPlanV2Ready(plan, 'douyin'), false);
  assert.deepEqual(actionPlanV2ReviewBlockers(plan, 'douyin'), []);
  assert.ok(actionPlanV2Blockers(plan, 'douyin').includes('action_plan_not_executable'));
  assert.ok(
    actionPlanV2ReviewBlockers(
      douyinPlanV2({ executable: true }),
      'douyin',
    ).includes('douyin_manual_plan_must_be_non_executable'),
  );
  assert.ok(
    actionPlanV2ReviewBlockers(
      douyinPlanV2({ execution_path_id: 'bilibili_api_v2' }),
      'douyin',
    ).includes('execution_path_mismatch'),
  );
  assert.ok(
    actionPlanV2ReviewBlockers(
      douyinPlanV2({ capability_blockers: [] }),
      'douyin',
    ).includes('douyin_no_official_interaction_api'),
  );

  const lottery = {
    id: 10,
    platform: 'douyin',
    raw_url: 'https://www.douyin.com/video/7300000000000000000',
    action_plan: plan,
  };
  assert.equal(platformDispatchBlocker('douyin', 'dry_run'), 'douyin_manual_shadow_only');
  assert.equal(platformDispatchBlocker('douyin', 'real_run'), 'douyin_manual_only');
  assert.equal(platformDispatchBlocker('douyin', 'shadow_run'), null);
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'shadow_run', safeAccountAvailable: true, accountScopeBound: true }),
    null,
  );
  assert.equal(
    dispatchSafetyBlocker({
      lottery,
      mode: 'real_run',
      gate: { allowed: true },
      safeAccountAvailable: true,
      accountScopeBound: true,
    }),
    'douyin_manual_only',
  );
});

test('builds Douyin manual checklist only from reviewed required actions', () => {
  assert.deepEqual(manualAssistedChecklist(douyinPlanV2(), 'douyin'), [
    { action: 'followed', required: true, exactValue: '@creator' },
    { action: 'liked', required: true, exactValue: '' },
    { action: 'commented', required: true, exactValue: '#抽奖# 精确评论' },
    { action: 'favorited', required: true, exactValue: '' },
  ]);
  assert.deepEqual(manualShadowObservation({
    selector_observation_complete: true,
    manual_shadow_task_id: 'dy-shadow-1',
  }), { complete: true, taskId: 'dy-shadow-1' });
  const plainRepost = douyinPlanV2({
    required_actions: ['reposted'],
    action_payloads: { reposted: {} },
    content_requirements: {
      follow_targets: [],
      commented: { topic_tags: [], mentions: [] },
      reposted: { topic_tags: [], mentions: [] },
    },
  });
  assert.deepEqual(actionPlanV2ReviewBlockers(plainRepost, 'douyin'), []);
  assert.deepEqual(manualAssistedChecklist(plainRepost, 'douyin'), [
    { action: 'reposted', required: true, exactValue: '' },
  ]);
  assert.deepEqual(buildActionPlanV2Update({
    platform: 'douyin',
    requiredActions: ['reposted'],
    actionPayloads: { reposted: { text: '' } },
    executionPathId: 'douyin_manual_v1',
    ruleText: '转发本视频参与抽奖',
    ruleCompleteConfirmed: true,
    reviewed: true,
  }).action_payloads, { reposted: {} });
  assert.deepEqual(manualAssistedChecklist(
    douyinPlanV2({ review_required: true }),
    'douyin',
  ), []);
  assert.deepEqual(manualAssistedChecklist(
    douyinPlanV2({ reviewed_by: '' }),
    'douyin',
  ), []);
});

test('builds Xiaohongshu updates without leaking repost or unsupported actions', () => {
  assert.deepEqual(buildActionPlanV2Update({
    platform: 'xiaohongshu',
    requiredActions: ['reposted', 'favorited', 'commented', 'liked', 'followed'],
    actionPayloads: {
      followed: { target_handle: '@creator' },
      liked: {},
      commented: { text: 'exact comment' },
      favorited: {},
      reposted: { text: 'must not leak' },
    },
    executionPathId: 'xiaohongshu_manual_v1',
    ruleText: 'complete source rule',
    ruleCompleteConfirmed: true,
    reviewed: true,
  }), {
    required_actions: ['followed', 'liked', 'commented', 'favorited'],
    action_payloads: {
      followed: { target_handle: '@creator' },
      liked: {},
      commented: { text: 'exact comment' },
      favorited: {},
    },
    execution_path_id: 'xiaohongshu_manual_v1',
    rule_text: 'complete source rule',
    reviewed: true,
    rule_complete_confirmed: true,
  });
});

test('separates Xiaohongshu semantic review from unavailable automatic real-run', () => {
  const plan = xiaohongshuPlanV2();
  assert.equal(actionPlanV2ReviewReady(plan, 'xiaohongshu'), true);
  assert.equal(actionPlanV2Ready(plan, 'xiaohongshu'), false);
  assert.deepEqual(actionPlanV2ReviewBlockers(plan, 'xiaohongshu'), []);
  assert.ok(actionPlanV2Blockers(plan, 'xiaohongshu').includes('action_plan_not_executable'));
  assert.ok(
    actionPlanV2ReviewBlockers(
      xiaohongshuPlanV2({ executable: true }),
      'xiaohongshu',
    ).includes('xiaohongshu_manual_plan_must_be_non_executable'),
  );
  assert.equal(
    actionPlanV2ReviewReady(xiaohongshuPlanV2({
      payload_validation_errors: ['comment_payload_invalid'],
    }), 'xiaohongshu'),
    false,
  );
  assert.equal(
    actionPlanV2ReviewReady(xiaohongshuPlanV2({
      capability_blockers: ['xiaohongshu_no_official_interaction_api', 'unexpected_blocker'],
    }), 'xiaohongshu'),
    false,
  );
  assert.ok(
    actionPlanV2ReviewBlockers(
      xiaohongshuPlanV2({ capability_blockers: [] }),
      'xiaohongshu',
    ).includes('xiaohongshu_no_official_interaction_api'),
  );

  const missingFavorite = xiaohongshuPlanV2({
    required_actions: ['followed', 'liked', 'commented'],
    action_payloads: {
      followed: { target_handle: '@creator' },
      liked: {},
      commented: { text: '#giveaway# exact comment', topic_tags: ['#giveaway#'] },
    },
  });
  assert.ok(
    actionPlanV2ReviewBlockers(missingFavorite, 'xiaohongshu')
      .includes('xiaohongshu_four_action_plan_required'),
  );
  assert.ok(
    actionPlanV2ReviewBlockers(
      xiaohongshuPlanV2({ execution_path_id: 'bilibili_api_v2' }),
      'xiaohongshu',
    ).includes('execution_path_mismatch'),
  );
  const favoriteWithPayload = xiaohongshuPlanV2();
  favoriteWithPayload.action_payloads.favorited = { text: 'not allowed' };
  assert.ok(
    actionPlanV2ReviewBlockers(favoriteWithPayload, 'xiaohongshu')
      .includes('action_payload_favorited_must_be_empty'),
  );
});

test('Xiaohongshu allows only shadow tasks and cannot trust gate.allowed for real-run', () => {
  const lottery = {
    id: 9,
    platform: 'xiaohongshu',
    raw_url: 'https://www.xiaohongshu.com/explore/example',
    action_plan: xiaohongshuPlanV2(),
  };
  assert.equal(platformDispatchBlocker('xiaohongshu', 'dry_run'), 'xiaohongshu_manual_shadow_only');
  assert.equal(platformDispatchBlocker('xiaohongshu', 'real_run'), 'xiaohongshu_manual_only');
  assert.equal(platformDispatchBlocker('xiaohongshu', 'shadow_run'), null);
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'dry_run', safeAccountAvailable: true }),
    'xiaohongshu_manual_shadow_only',
  );
  assert.equal(
    dispatchSafetyBlocker({ lottery, mode: 'shadow_run', safeAccountAvailable: true, accountScopeBound: true }),
    null,
  );
  assert.equal(
    dispatchSafetyBlocker({
      lottery,
      mode: 'real_run',
      gate: { allowed: true },
      safeAccountAvailable: true,
      accountScopeBound: true,
    }),
    'xiaohongshu_manual_only',
  );
});

test('builds a read-only Xiaohongshu manual checklist from the reviewed plan', () => {
  assert.deepEqual(xiaohongshuManualChecklist(xiaohongshuPlanV2()), [
    { action: 'followed', required: true, exactValue: '@creator' },
    { action: 'liked', required: true, exactValue: '' },
    { action: 'commented', required: true, exactValue: '#giveaway# exact comment' },
    { action: 'favorited', required: true, exactValue: '' },
  ]);
  assert.deepEqual(xiaohongshuManualChecklist(planV2(), 'bilibili'), []);
});

test('Xiaohongshu Shadow status trusts selector observation, not real-run qualification', () => {
  assert.deepEqual(xiaohongshuShadowObservation({
    selector_observation_complete: true,
    shadow_ready: false,
    manual_shadow_task_id: 'shadow-manual-1',
  }), { complete: true, taskId: 'shadow-manual-1' });
  assert.deepEqual(xiaohongshuShadowObservation({ shadow_ready: true }), {
    complete: false,
    taskId: '',
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
