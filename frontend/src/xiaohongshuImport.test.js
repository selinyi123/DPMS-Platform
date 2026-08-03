import assert from 'node:assert/strict';
import test from 'node:test';

import {
  XiaohongshuImportError,
  normalizeTargetImportForPlatform,
  normalizeXiaohongshuTargetImport,
} from './xiaohongshuImport.js';

const NOTE_A = '64F1A2B3C4D5E6F7A8B9C0D1';
const NOTE_B = '65a1a2b3c4d5e6f7a8b9c0d2';
const NOTE_C = '66a1a2b3c4d5e6f7a8b9c0d3';
const ALL_PLATFORMS = {
  allowedPlatformIds: ['bilibili', 'douyin', 'weibo', 'xiaohongshu'],
};

test('sanitizes xhs-cli style envelopes before target import', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify({
    ok: true,
    schema_version: 1,
    cookie: 'a1=do-not-upload; web_session=secret',
    data: {
      items: [
        { id: NOTE_C, title: 'lottery', xsec_token: 'secret-token' },
        {
          note_id: NOTE_A.toLowerCase(),
          url: `https://www.xiaohongshu.com/explore/${NOTE_A}?xsec_token=secret-token&xsec_source=pc_search`,
        },
      ],
    },
  }));

  assert.equal(result.converted, true);
  assert.equal(result.targetCount, 2);
  assert.equal(result.discardedSensitiveFields, 2);
  assert.deepEqual(result.content.split('\n'), [
    `https://www.xiaohongshu.com/explore/${NOTE_C}`,
    `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`,
  ]);
  assert.doesNotMatch(result.content, /secret|xsec/i);
});

test('accepts MediaCrawler-like JSONL and ignores comment ids', () => {
  const content = [
    JSON.stringify({ note_id: NOTE_A, note_url: `https://www.xiaohongshu.com/discovery/item/${NOTE_A}` }),
    JSON.stringify({ note_id: NOTE_B, xsec_token: 'ephemeral', comments: [{ id: '66b1a2b3c4d5e6f7a8b9c0d3', content: 'comment' }] }),
  ].join('\n');
  const result = normalizeXiaohongshuTargetImport(content);

  assert.equal(result.targetCount, 2);
  assert.deepEqual(result.content.split('\n'), [
    `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`,
    `https://www.xiaohongshu.com/explore/${NOTE_B}`,
  ]);
});

test('accepts nested note_url-only records from trusted feed collections', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify({
    data: {
      items: [{ note_url: `https://www.xiaohongshu.com/explore/${NOTE_A}` }],
    },
  }));

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('recognizes nested MCP feed ids only when note context is present', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify({
    feeds: [{ id: NOTE_A, xsecToken: 'secret', noteCard: { title: 'giveaway' } }],
    comments: [{ id: NOTE_B, content: 'not a note id' }],
  }));

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('does not extract links from descriptions or creator-shaped ids', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify({
    note_id: NOTE_A,
    desc: `see also https://www.xiaohongshu.com/explore/${NOTE_B}`,
    creator: { id: '66b1a2b3c4d5e6f7a8b9c0d3', desc: 'creator profile' },
  }));

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('does not extract nested related, comment, or profile URLs', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify({
    note_id: NOTE_A,
    related: { url: `https://www.xiaohongshu.com/explore/${NOTE_B}` },
    comments: [{ note_url: `https://www.xiaohongshu.com/explore/${NOTE_C}` }],
    creator: { url: `https://www.xiaohongshu.com/explore/${NOTE_B}` },
    reply_list: [{ note_url: `https://www.xiaohongshu.com/explore/${NOTE_C}` }],
    recommend_list: [{ id: NOTE_C, title: 'not a target', xsec_token: 'context' }],
  }));

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('prefers provider note_url over a generic url column', () => {
  const content = [
    'url,note_url,title',
    `https://www.xiaohongshu.com/explore/${NOTE_B},https://www.xiaohongshu.com/explore/${NOTE_A},lottery`,
  ].join('\n');
  const result = normalizeXiaohongshuTargetImport(content);

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('rejects conflicting IDs and URLs in one structured note record', () => {
  for (const payload of [
    { note_id: NOTE_A, url: `https://www.xiaohongshu.com/explore/${NOTE_B}` },
    { note_id: NOTE_A, urls: [`https://www.xiaohongshu.com/explore/${NOTE_B}`] },
    { note_id: NOTE_A, links: [[[[[`https://www.xiaohongshu.com/explore/${NOTE_B}`]]]]] },
  ]) {
    assert.throws(
      () => normalizeXiaohongshuTargetImport(JSON.stringify(payload)),
      error => error instanceof XiaohongshuImportError
        && error.code === 'xiaohongshu_import_conflicting_target',
    );
  }

  const envelope = normalizeXiaohongshuTargetImport(JSON.stringify({
    url: `https://www.xiaohongshu.com/explore/${NOTE_B}`,
    data: { note_id: NOTE_A },
  }));
  assert.equal(envelope.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('keeps only the path of trusted XHS short links', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify({
    share_url: 'https://xhslink.com/a/AbC123?xsec_token=secret#fragment',
    unrelated_url: 'https://evil.example/explore/64f1a2b3c4d5e6f7a8b9c0d1',
  }));

  assert.equal(result.content, 'https://xhslink.com/a/AbC123');
});

test('preserves DPMS CSV metadata while removing XHS URL context', () => {
  const content = `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A}?xsec_token=secret,80,2026-08-01`;
  const result = normalizeXiaohongshuTargetImport(content);
  assert.equal(
    result.content,
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()},80,2026-08-01`,
  );
  assert.equal(result.targetCount, 1);
  assert.equal(result.discardedSensitiveFields, 1);
  assert.doesNotMatch(result.content, /xsec|secret/);
});

test('drops columns outside the Core CSV grammar', () => {
  const content = `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A},80,2026-08-01,cookie=a1-secret`;
  const result = normalizeXiaohongshuTargetImport(content);

  assert.equal(
    result.content,
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()},80,2026-08-01`,
  );
  assert.doesNotMatch(result.content, /cookie|secret/i);
});

test('preserves supported mixed-platform rows but rejects unknown leading fields', () => {
  const mixed = normalizeXiaohongshuTargetImport([
    'bilibili,https://t.bilibili.com/123?SESSDATA=secret&from=dpms#fragment,75,2026-08-01,ignored-secret',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A}`,
  ].join('\n'), ALL_PLATFORMS);
  assert.deepEqual(mixed.content.split('\n'), [
    'bilibili,https://t.bilibili.com/123,75,2026-08-01',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`,
  ]);
  assert.doesNotMatch(mixed.content, /secret|sessdata|from|fragment/i);

  assert.throws(
    () => normalizeXiaohongshuTargetImport('cookie,a1=do-not-upload,80'),
    error => error instanceof XiaohongshuImportError
      && error.code === 'xiaohongshu_import_no_targets',
  );
});

test('preserves an explicit mixed-platform file under a non-XHS default', () => {
  const content = [
    'platform,url,score,expires_at',
    'bilibili,https://t.bilibili.com/123?SESSDATA=secret,75,2026-08-01',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A}?xsec_token=secret,80,2026-08-02`,
  ].join('\n');
  const result = normalizeTargetImportForPlatform('bilibili', content, ALL_PLATFORMS);

  assert.deepEqual(result.content.split('\n'), [
    'bilibili,https://t.bilibili.com/123,75,2026-08-01',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()},80,2026-08-02`,
  ]);
  assert.doesNotMatch(result.content, /secret|sessdata|xsec/i);
});

test('preserves implicit-default plus explicit-platform mixed rows', () => {
  const content = [
    'https://t.bilibili.com/123?SESSDATA=secret',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A}?xsec_token=secret`,
  ].join('\n');
  const result = normalizeTargetImportForPlatform('bilibili', content, ALL_PLATFORMS);

  assert.deepEqual(result.content.split('\n'), [
    'https://t.bilibili.com/123',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`,
  ]);
  assert.doesNotMatch(result.content, /secret|sessdata|xsec/i);
});

test('deduplicates implicit and explicit forms of the default platform target', () => {
  const content = [
    'https://t.bilibili.com/123?from=implicit',
    'bilibili,https://t.bilibili.com/123?from=explicit',
  ].join('\n');
  const result = normalizeTargetImportForPlatform('bilibili', content, ALL_PLATFORMS);

  assert.equal(result.content, 'https://t.bilibili.com/123');
  assert.equal(result.targetCount, 1);
  assert.equal(result.discardedRows, 1);
});

test('rejects secrets placed inside score or expiry metadata columns', () => {
  for (const content of [
    `https://www.xiaohongshu.com/explore/${NOTE_A},50,cookie=a1-secret`,
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A},cookie=a1-secret,2026-08-01`,
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A},50,web_session=secret`,
  ]) {
    assert.throws(
      () => normalizeXiaohongshuTargetImport(content),
      error => error instanceof XiaohongshuImportError
        && error.code === 'xiaohongshu_import_invalid_metadata',
    );
  }
});

test('sanitizes MediaCrawler CSV without forwarding provider columns', () => {
  const content = [
    'note_id,note_url,title,xsec_token,cookie',
    `${NOTE_A},"https://www.xiaohongshu.com/explore/${NOTE_A}?xsec_token=secret",lottery,secret-token,a1=secret`,
  ].join('\n');
  const result = normalizeXiaohongshuTargetImport(content);

  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
  assert.equal(result.discardedSensitiveFields, 2);
  assert.doesNotMatch(result.content, /token|cookie|secret/i);
});

test('parses multiline RFC-style CSV fields without importing embedded links', () => {
  const content = [
    'note_id,note_url,title,desc,xsec_token',
    `${NOTE_A},https://www.xiaohongshu.com/explore/${NOTE_A},lottery,"first line`,
    `second line with https://www.xiaohongshu.com/explore/${NOTE_B}",secret-token`,
  ].join('\r\n');
  const result = normalizeXiaohongshuTargetImport(content);

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

test('locks TSV delimiter from the header when a quoted field contains commas', () => {
  const content = [
    'note_id\tnote_url\ttitle\txsec_token',
    `${NOTE_A}\thttps://www.xiaohongshu.com/explore/${NOTE_A}\t"lottery, gift"\tsecret-token`,
  ].join('\n');
  const result = normalizeXiaohongshuTargetImport(content);

  assert.equal(result.targetCount, 1);
  assert.equal(result.content, `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`);
});

for (const [name, content, code] of [
  ['invalid JSON', '{"feeds":', 'xiaohongshu_import_invalid_json'],
  ['JSON without targets', '{"cookie":"secret"}', 'xiaohongshu_import_no_targets'],
  ['invalid CSV', '"unterminated,field', 'xiaohongshu_import_invalid_csv'],
]) {
  test(`rejects ${name} without forwarding the raw export`, () => {
    assert.throws(
      () => normalizeXiaohongshuTargetImport(content),
      error => error instanceof XiaohongshuImportError && error.code === code,
    );
  });
}

test('bounds the number of imported targets', () => {
  const payload = Array.from({ length: 1001 }, (_, index) => ({
    note_id: index.toString(16).padStart(24, '0'),
  }));
  assert.throws(
    () => normalizeXiaohongshuTargetImport(JSON.stringify(payload)),
    error => error instanceof XiaohongshuImportError
      && error.code === 'xiaohongshu_import_too_many_targets',
  );
});

test('fails closed when structured XHS data is submitted under another platform', () => {
  const content = JSON.stringify({
    note_id: NOTE_A,
    cookie: 'a1=do-not-upload',
    xsec_token: 'do-not-upload',
  });
  assert.throws(
    () => normalizeTargetImportForPlatform('bilibili', content),
    error => error instanceof XiaohongshuImportError
      && error.code === 'target_import_structured_requires_platform',
  );
});

test('fails closed when XHS CSV is submitted under another platform', () => {
  const content = [
    'note_id,note_url,xsec_token,cookie',
    `${NOTE_A},https://www.xiaohongshu.com/explore/${NOTE_A},secret-token,a1=secret`,
  ].join('\n');
  assert.throws(
    () => normalizeTargetImportForPlatform('bilibili', content),
    error => error instanceof XiaohongshuImportError
      && error.code === 'target_import_sensitive_content_rejected',
  );
});

test('fails closed on bare credential material under another platform', () => {
  for (const content of ['a1=secret', 'authorization=Bearer-secret', 'sessionid=secret', 'sid=secret']) {
    assert.throws(
      () => normalizeTargetImportForPlatform('bilibili', content),
      error => error instanceof XiaohongshuImportError
        && error.code === 'target_import_sensitive_content_rejected',
    );
  }
});

test('sanitizes encoded query keys for every platform', () => {
  const result = normalizeTargetImportForPlatform(
    'bilibili',
    'https://t.bilibili.com/123?%73id=secret&from=feed#fragment',
    ALL_PLATFORMS,
  );

  assert.equal(result.content, 'https://t.bilibili.com/123');
  assert.doesNotMatch(result.content, /secret|%73id|from|fragment/i);
});

test('rejects sensitive credential material hidden in URL paths', () => {
  for (const content of [
    'https://xhslink.com/a/code/xsec_token=secret',
    'xiaohongshu,https://xhslink.com/a/code/SESSIONID=secret',
    'bilibili,https://www.bilibili.com/video/BV1abc/SESSDATA=secret,75',
    'https://xhslink.com/a/%ZZ/%78sec_token%3Dsecret',
    'https://xhslink.com/a/%25252578sec_token%2525253Dsecret',
  ]) {
    assert.throws(
      () => normalizeXiaohongshuTargetImport(content, ALL_PLATFORMS),
      error => error instanceof XiaohongshuImportError
        && error.code === 'target_import_sensitive_content_rejected',
    );
  }
});

test('retains multiple XHS short links for Core rejection and audit', () => {
  const result = normalizeXiaohongshuTargetImport([
    'https://xhslink.com/a/one',
    'https://xhslink.com/a/two',
  ].join('\n'));

  assert.equal(result.targetCount, 2);
  assert.equal(result.blockedShortLinkCount, 2);
  assert.equal(result.shortLinkCount, 0);
  assert.deepEqual(result.shortLinkErrorsByPlatform, {
    xiaohongshu: 'xiaohongshu_import_short_link_batch_unsupported',
  });
  assert.equal(result.content, [
    'https://xhslink.com/a/one',
    'https://xhslink.com/a/two',
  ].join('\n'));
});

test('allows one supported-platform short link alongside direct links', () => {
  const result = normalizeXiaohongshuTargetImport([
    'bilibili,https://b23.tv/one',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A}`,
  ].join('\n'), ALL_PLATFORMS);

  assert.equal(result.targetCount, 2);
  assert.equal(result.shortLinkCount, 1);
  assert.equal(result.content, [
    'bilibili,https://b23.tv/one',
    `xiaohongshu,https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`,
  ].join('\n'));
});

test('allows one XHS short link alongside a direct XHS link', () => {
  const result = normalizeXiaohongshuTargetImport(JSON.stringify([
    'https://xhslink.com/a/one',
    `https://www.xiaohongshu.com/explore/${NOTE_A}`,
  ]));

  assert.equal(result.targetCount, 2);
  assert.equal(result.shortLinkCount, 1);
});

test('detects trailing-dot hosts when reporting short-link isolation', () => {
  const result = normalizeTargetImportForPlatform('bilibili', [
    'https://b23.tv./one',
    'https://b23.tv./two',
  ].join('\n'), ALL_PLATFORMS);

  assert.equal(result.targetCount, 2);
  assert.equal(result.blockedShortLinkCount, 2);
  assert.deepEqual(result.shortLinkErrorsByPlatform, {
    bilibili: 'xiaohongshu_import_short_link_batch_unsupported',
  });
  assert.equal(result.content, [
    'https://b23.tv./one',
    'https://b23.tv./two',
  ].join('\n'));
});

test('does not count a DPMS header as a second short-link target', () => {
  const result = normalizeTargetImportForPlatform('bilibili', [
    'platform,url,score,expires_at',
    'bilibili,https://b23.tv/one,75,2026-08-01',
  ].join('\n'), ALL_PLATFORMS);

  assert.equal(result.targetCount, 1);
  assert.equal(result.shortLinkCount, 1);
  assert.equal(result.content, 'bilibili,https://b23.tv/one,75,2026-08-01');
});
