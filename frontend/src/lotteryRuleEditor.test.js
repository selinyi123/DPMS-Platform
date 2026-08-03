import assert from 'node:assert/strict';
import test from 'node:test';

import {
  actionRequirementValues,
  authoritativeRuleText,
  automaticFollowTarget,
  defaultRepostText,
  lotteryTargetIdentity,
  ruleEditorSaveBlockers,
  visibleRuleSnapshotParts,
} from './lotteryRuleEditor.js';

test('target identity uses the stable hydrated contract without treating a UID as a handle', () => {
  const lottery = {
    target_identity: {
      uid: '4631',
      display_name: '华硕官方UP',
      profile_url: 'https://space.bilibili.com/4631',
      verified: true,
      source: 'canonical_dynamic',
    },
  };
  assert.deepEqual(lotteryTargetIdentity(lottery), {
    uid: '4631',
    displayName: '华硕官方UP',
    profileUrl: 'https://space.bilibili.com/4631',
    verified: true,
    source: 'canonical_dynamic',
  });
  assert.equal(automaticFollowTarget(lottery, {}), '@华硕官方UP');
});

test('source follow requirement wins over author identity and malformed names remain fail closed', () => {
  assert.equal(automaticFollowTarget({
    target_identity: { uid: '9', display_name: '目标作者' },
  }, {
    content_requirements: { follow_targets: ['@规则指定账号'] },
    action_payloads: { followed: { target_handle: '@旧账号' } },
  }), '@规则指定账号');
  assert.equal(automaticFollowTarget({
    target_identity: { uid: '9', display_name: '包含 空格' },
  }, {}), '');
});

test('rule text and snapshot presentation use backend authority in a stable order', () => {
  const lottery = {
    rule_text: '完整权威原文',
    rule_snapshot: {
      body: { text: '折叠正文', present: true, trusted: true },
      expanded_body: { text: '展开正文', present: true, trusted: true },
      pinned_comment: { text: '作者置顶补充', present: true, trusted: true },
    },
  };
  assert.equal(authoritativeRuleText(lottery), '完整权威原文');
  assert.deepEqual(visibleRuleSnapshotParts(lottery).map(item => item.key), [
    'body', 'expanded_body', 'pinned_comment',
  ]);
});

test('repost defaults and exact source requirements are deterministic', () => {
  assert.equal(defaultRepostText('bilibili'), '转发动态');
  assert.equal(defaultRepostText('weibo'), '');
  assert.deepEqual(actionRequirementValues({
    content_requirements: {
      commented: { topic_tags: ['#抽奖#', '#抽奖#', ''], mentions: ['@作者'] },
    },
  }, 'commented', 'topic_tags'), ['#抽奖#']);
});

test('save blockers list every unmet condition instead of only disabling the button', () => {
  assert.deepEqual(ruleEditorSaveBlockers({
    actions: ['commented'],
    ruleText: '抽奖规则',
    executionPathId: 'bilibili_api_v1',
    executionPathValid: true,
    ruleCompleteConfirmed: false,
    reviewedConfirmed: false,
    requiredActionSetComplete: true,
    unresolvedRequirements: ['mention_friends'],
    payloadErrors: ['action_payload_commented_text_required'],
  }), [
    'rule_completion_unconfirmed',
    'plan_review_unconfirmed',
    'requirement:mention_friends',
    'payload:action_payload_commented_text_required',
  ]);
});
