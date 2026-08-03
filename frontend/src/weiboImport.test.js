import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TargetImportError,
  WEIBO_IMPORT_MAX_BYTES,
  normalizeTargetImportForPlatform,
  normalizeWeiboTargetImport,
} from './xiaohongshuImport.js';

const OPTIONS = {
  allowedPlatformIds: ['bilibili', 'douyin', 'weibo', 'xiaohongshu'],
};

function errorCode(fn) {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof TargetImportError);
    return error.code;
  }
  assert.fail('expected target import to fail');
}

test('sanitizes official-style Weibo status exports into one canonical target', () => {
  const result = normalizeWeiboTargetImport(JSON.stringify({
    mblogid: 'PCAGRFqKj',
    mid: '4890123456789012',
    text_raw: '转发抽奖',
    access_token: 'must-not-leave-browser',
    user: { idstr: '3937348351', screen_name: 'creator' },
    comments: [{ idstr: '4890123456789999', text: 'not-a-target' }],
    retweeted_status: { mblogid: 'OtherPost', text: 'not-a-target' },
  }), OPTIONS);

  assert.equal(result.content, 'https://weibo.com/detail/PCAGRFqKj');
  assert.equal(result.targetCount, 1);
  assert.equal(result.discardedSensitiveFields, 1);
  assert.doesNotMatch(result.content, /token|4890123456789999|OtherPost/);
});

test('accepts a trusted root status URL and removes share query context', () => {
  const result = normalizeWeiboTargetImport(JSON.stringify({
    status_url: 'https://m.weibo.cn/status/4890123456789012?jumpfrom=weibocom&access_token=secret',
    text: '抽奖活动',
  }), OPTIONS);

  assert.equal(result.content, 'https://m.weibo.cn/detail/4890123456789012');
  assert.doesNotMatch(result.content, /jumpfrom|access_token|secret/);
});

test('accepts canonical positive int64 Weibo status ids including the official 10-digit example', () => {
  for (const mid of ['7987885345', '9223372036854775807']) {
    const result = normalizeWeiboTargetImport(JSON.stringify({
      mid,
      text_raw: '抽奖活动',
    }), OPTIONS);
    assert.equal(result.content, `https://weibo.com/detail/${mid}`);
  }
});

test('rejects non-canonical or out-of-range numeric Weibo status ids', () => {
  for (const mid of ['0', '07987885345', '9223372036854775808', '７９８７８８５３４５']) {
    const code = errorCode(() => normalizeWeiboTargetImport(JSON.stringify({
      mid,
      text_raw: 'invalid status id',
    }), OPTIONS));
    assert.equal(code, 'weibo_import_no_targets');
  }
});

test('sanitizes provider CSV and validates status id against URL', () => {
  const result = normalizeWeiboTargetImport([
    'mblogid,status_url,text_raw,cookie',
    'PCAGRFqKj,https://weibo.com/3937348351/PCAGRFqKj?refer_flag=share,抽奖,must-not-leave-browser',
  ].join('\n'), OPTIONS);

  assert.equal(result.content, 'https://weibo.com/3937348351/PCAGRFqKj');
  assert.equal(result.discardedSensitiveFields, 1);

  const conflict = errorCode(() => normalizeWeiboTargetImport([
    'mblogid,status_url,text_raw',
    'PCAGRFqKj,https://weibo.com/3937348351/Tampered,conflict',
  ].join('\n'), OPTIONS));
  assert.equal(conflict, 'weibo_import_conflicting_target');
});

test('rejects conflicting Weibo ids and URLs in a structured record', () => {
  const code = errorCode(() => normalizeWeiboTargetImport(JSON.stringify({
    mblogid: 'PCAGRFqKj',
    mid: '4890123456789012',
    text: 'conflict',
    status_url: 'https://weibo.com/3937348351/Tampered',
  }), OPTIONS));

  assert.equal(code, 'weibo_import_conflicting_target');
});

test('does not treat user or comment ids as Weibo lottery targets', () => {
  const code = errorCode(() => normalizeWeiboTargetImport(JSON.stringify({
    user: { idstr: '4890123456789012', screen_name: 'not-a-status' },
    comments: [{ idstr: '4890123456789999', text: 'not-a-status' }],
  }), OPTIONS));

  assert.equal(code, 'weibo_import_no_targets');
});

test('fails closed when a Weibo structured export uses another platform', () => {
  const source = JSON.stringify({
    mblogid: 'PCAGRFqKj',
    text_raw: 'wrong platform',
  });
  const code = errorCode(() => normalizeTargetImportForPlatform('bilibili', source, OPTIONS));

  assert.equal(code, 'target_import_structured_requires_platform');
});

test('retains over-budget Weibo short links for Core rejection and audit', () => {
  const result = normalizeWeiboTargetImport(JSON.stringify([
    'https://t.cn/A6abcdef?token=discarded',
    'https://t.cn/A6ghijkl',
  ]), OPTIONS);

  assert.equal(result.targetCount, 2);
  assert.equal(result.blockedShortLinkCount, 2);
  assert.equal(result.shortLinkCount, 0);
  assert.deepEqual(result.shortLinkErrorsByPlatform, {
    weibo: 'weibo_import_short_link_batch_unsupported',
  });
  assert.match(result.content, /https:\/\/t\.cn\/A6abcdef/);
  assert.doesNotMatch(result.content, /token|discarded/i);
});

test('allows one Weibo short link alongside a direct status link', () => {
  const result = normalizeWeiboTargetImport(JSON.stringify([
    'https://t.cn/A6abcdef',
    'https://weibo.com/detail/PCAGRFqKj',
  ]), OPTIONS);

  assert.equal(result.targetCount, 2);
  assert.equal(result.shortLinkCount, 1);
});

test('uses the Weibo-specific import size boundary', () => {
  const source = 'x'.repeat(WEIBO_IMPORT_MAX_BYTES + 1);
  const code = errorCode(() => normalizeTargetImportForPlatform('weibo', source, OPTIONS));
  assert.equal(code, 'weibo_import_too_large');
});
