import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DOUYIN_IMPORT_MAX_BYTES,
  TargetImportError,
  normalizeDouyinTargetImport,
  normalizeTargetImportForPlatform,
} from './xiaohongshuImport.js';

const OPTIONS = {
  allowedPlatformIds: ['bilibili', 'douyin', 'weibo', 'xiaohongshu'],
};

function errorCode(fn) {
  assert.throws(fn, error => {
    assert.ok(error instanceof TargetImportError);
    return true;
  });
  try {
    fn();
  } catch (error) {
    return error.code;
  }
  return '';
}

test('sanitizes MediaCrawler-style Douyin content exports into canonical targets', () => {
  const result = normalizeDouyinTargetImport(JSON.stringify([
    {
      aweme_id: '7300000000000000000',
      aweme_type: 0,
      title: '抽奖活动',
      cookie: 'must-not-leave-browser',
      author: { uid: 'not-a-target' },
      comments: [{ comment_id: '7311111111111111111', text: 'not-a-target' }],
    },
  ]), OPTIONS);

  assert.equal(result.content, 'https://www.douyin.com/video/7300000000000000000');
  assert.equal(result.targetCount, 1);
  assert.equal(result.discardedSensitiveFields, 1);
  assert.doesNotMatch(result.content, /cookie|7311111111111111111/);
});

test('accepts F2-style aweme lists and preserves explicit note routes', () => {
  const result = normalizeDouyinTargetImport(JSON.stringify({
    aweme_list: [
      {
        aweme_id: '7300000000000000001',
        aweme_type: 68,
        desc: '图文抽奖',
        share_url: 'https://www.douyin.com/note/7300000000000000001?previous_page=web_code_link',
        video: {
          play_addr: {
            url_list: ['https://cdn.example.invalid/media.mp4'],
          },
        },
      },
    ],
  }), OPTIONS);

  assert.equal(result.content, 'https://www.douyin.com/note/7300000000000000001');
  assert.equal(result.targetCount, 1);
});

test('maps an id-only aweme_type 68 record to a note instead of a video', () => {
  const result = normalizeDouyinTargetImport(JSON.stringify({
    aweme_id: '7659275356428852849',
    aweme_type: 68,
    desc: '图文抽奖',
  }), OPTIONS);

  assert.equal(result.content, 'https://www.douyin.com/note/7659275356428852849');
});

test('sanitizes a provider CSV and keeps Douyin-specific routes and errors', () => {
  const result = normalizeDouyinTargetImport([
    'aweme_id,aweme_type,desc,cookie',
    '7659275356428852849,68,图文抽奖,must-not-leave-browser',
  ].join('\n'), OPTIONS);

  assert.equal(result.content, 'https://www.douyin.com/note/7659275356428852849');
  assert.equal(result.discardedSensitiveFields, 1);
  assert.equal(
    errorCode(() => normalizeDouyinTargetImport('aweme_id,desc\n"7659275356428852849,抽奖', OPTIONS)),
    'douyin_import_invalid_csv',
  );
  assert.equal(
    errorCode(() => normalizeDouyinTargetImport([
      'platform,url,value_score',
      'douyin,https://www.douyin.com/video/7300000000000000000,secret',
    ].join('\n'), OPTIONS)),
    'douyin_import_invalid_metadata',
  );
});

test('normalizes iesdouyin video shares without forwarding query context', () => {
  const result = normalizeDouyinTargetImport(JSON.stringify({
    video_id: '7300000000000000002',
    title: '视频抽奖',
    share_url: 'https://www.iesdouyin.com/share/video/7300000000000000002/?region=CN&sessionid=secret',
  }), OPTIONS);

  assert.equal(result.content, 'https://www.douyin.com/video/7300000000000000002');
  assert.doesNotMatch(result.content, /region|sessionid|secret/);
});

test('rejects conflicting Douyin ids and URLs in one content record', () => {
  const code = errorCode(() => normalizeDouyinTargetImport(JSON.stringify({
    aweme_id: '7300000000000000003',
    aweme_type: 0,
    title: 'conflict',
    share_url: 'https://www.douyin.com/video/7300000000000000004',
  }), OPTIONS));

  assert.equal(code, 'douyin_import_conflicting_target');
});

test('rejects conflicting Douyin ids and routes in provider CSV rows', () => {
  const conflictingIdCode = errorCode(() => normalizeDouyinTargetImport([
    'aweme_id,aweme_type,share_url,desc',
    '7300000000000000003,0,https://www.douyin.com/video/7300000000000000004,conflict',
  ].join('\n'), OPTIONS));
  const conflictingRouteCode = errorCode(() => normalizeDouyinTargetImport([
    'aweme_id,aweme_type,share_url,desc',
    '7300000000000000003,68,https://www.douyin.com/video/7300000000000000003,wrong-route',
  ].join('\n'), OPTIONS));

  assert.equal(conflictingIdCode, 'douyin_import_conflicting_target');
  assert.equal(conflictingRouteCode, 'douyin_import_conflicting_target');
});

test('accepts matching Douyin ids, types, and URLs in provider CSV rows', () => {
  const result = normalizeDouyinTargetImport([
    'aweme_id,aweme_type,share_url,desc',
    '7300000000000000003,68,https://www.douyin.com/note/7300000000000000003?from=share,matching',
  ].join('\n'), OPTIONS);

  assert.equal(result.content, 'https://www.douyin.com/note/7300000000000000003');
  assert.equal(result.targetCount, 1);
});

test('rejects an explicit Douyin content type that conflicts with a JSON URL route', () => {
  const code = errorCode(() => normalizeDouyinTargetImport(JSON.stringify({
    aweme_id: '7300000000000000003',
    aweme_type: 68,
    title: 'wrong route',
    share_url: 'https://www.douyin.com/video/7300000000000000003',
  }), OPTIONS));

  assert.equal(code, 'douyin_import_conflicting_target');
});

test('does not treat comment or media ids as Douyin lottery targets', () => {
  const code = errorCode(() => normalizeDouyinTargetImport(JSON.stringify({
    comments: [{ comment_id: '7300000000000000005', text: 'hello' }],
    video: { play_addr: { url_list: ['https://cdn.example.invalid/7300000000000000006'] } },
  }), OPTIONS));

  assert.equal(code, 'douyin_import_no_targets');
});

test('fails closed when a Douyin structured export is submitted under another platform', () => {
  const source = JSON.stringify({
    aweme_id: '7300000000000000007',
    aweme_type: 0,
    title: 'wrong platform',
  });
  const code = errorCode(() => normalizeTargetImportForPlatform('bilibili', source, OPTIONS));

  assert.equal(code, 'target_import_douyin_requires_platform');
});

test('rejects multiple Douyin short links in one batch', () => {
  const code = errorCode(() => normalizeDouyinTargetImport(JSON.stringify([
    'https://v.douyin.com/abc123/?cookie=discarded',
    'https://v.douyin.com/def456/',
  ]), OPTIONS));

  assert.equal(code, 'douyin_import_short_link_batch_unsupported');
});

test('uses a Douyin-specific error for malformed structured exports', () => {
  const code = errorCode(() => normalizeDouyinTargetImport('{"aweme_id":', OPTIONS));
  assert.equal(code, 'douyin_import_invalid_json');
});

test('uses the Douyin size limit and error at the generic import boundary', () => {
  const oversized = 'x'.repeat(DOUYIN_IMPORT_MAX_BYTES + 1);
  const code = errorCode(() => normalizeTargetImportForPlatform('douyin', oversized, OPTIONS));

  assert.equal(code, 'douyin_import_too_large');
});
