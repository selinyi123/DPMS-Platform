import assert from 'node:assert/strict';
import test from 'node:test';

import {
  XHS_INGEST_MAX_BYTES,
  XHS_OFFLINE_MAX_BYTES,
  XiaohongshuTargetPursuitError,
  buildCandidateDecisionPayload,
  buildCandidateIngestPayload,
  buildXiaohongshuScanPayload,
  candidateDecisionCanSubmit,
  candidateItemsFromResponse,
  normalizeXiaohongshuCandidate,
  parseOfflineSearchResult,
  sanitizeXiaohongshuTargetUrl,
  validateXiaohongshuSource,
} from './xiaohongshuTargetPursuit.js';

const NOTE_A = '64F1A2B3C4D5E6F7A8B9C0D1';
const NOTE_B = '65a1a2b3c4d5e6f7a8b9c0d2';
const NOTE_A_URL = `https://www.xiaohongshu.com/explore/${NOTE_A.toLowerCase()}`;
const NOTE_B_URL = `https://www.xiaohongshu.com/explore/${NOTE_B}`;

function expectCode(code) {
  return error => error instanceof XiaohongshuTargetPursuitError
    && error.code === code;
}

test('validates and canonicalizes the two browser scan source types', () => {
  assert.equal(validateXiaohongshuSource('keyword', '抽奖'), null);
  assert.equal(
    validateXiaohongshuSource('keyword', '抽'.repeat(65)),
    'xhs_target_keyword_invalid',
  );
  assert.equal(
    validateXiaohongshuSource('author_profile', 'https://www.xiaohongshu.com/explore/foo'),
    'xhs_target_author_profile_invalid',
  );
  assert.deepEqual(
    buildXiaohongshuScanPayload(
      'author_profile',
      'https://www.xiaohongshu.com/user/profile/abc123?xsec_token=secret#feed',
    ),
    {
      source_type: 'author_profile',
      source_value: 'https://www.xiaohongshu.com/user/profile/abc123',
    },
  );
  assert.throws(
    () => buildXiaohongshuScanPayload('offline_search_result', 'result.json'),
    expectCode('xhs_target_browser_source_invalid'),
  );
});

test('canonicalizes only supported Xiaohongshu note targets', () => {
  assert.equal(
    sanitizeXiaohongshuTargetUrl(
      `https://www.xiaohongshu.com/discovery/item/${NOTE_A}?xsec_token=secret#fragment`,
    ),
    NOTE_A_URL,
  );
  assert.equal(
    sanitizeXiaohongshuTargetUrl('https://evil.example/explore/64f1a2b3c4d5e6f7a8b9c0d1'),
    null,
  );
  assert.equal(
    sanitizeXiaohongshuTargetUrl('http://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1'),
    null,
  );
});

test('parses JSON evidence while removing credentials and URL context', () => {
  const result = parseOfflineSearchResult('search.json', JSON.stringify({
    candidates: [{
      raw_url: `${NOTE_A_URL}?xsec_token=do-not-upload&xsec_source=pc_search#feed`,
      title: '夏日抽奖',
      cookie: 'a1=do-not-upload',
      evidence: {
        body_snapshot: '正文中的奖品说明',
        expanded_body_snapshot: '展开后的完整说明',
        pinned_comment_snapshot: '开奖时间改为周五',
        profile_url: 'https://www.xiaohongshu.com/user/profile/abc?token=secret#fragment',
        authorization: 'Bearer secret',
      },
      rule: {
        required_actions: ['liked', 'favorited'],
        prize: '相机',
      },
      classification: {
        is_collection: false,
        author_verified: true,
      },
    }],
  }));

  assert.equal(result.targetCount, 1);
  assert.equal(result.candidates[0].raw_url, NOTE_A_URL);
  assert.equal(result.candidates[0].evidence.body_snapshot, '正文中的奖品说明');
  assert.deepEqual(
    result.candidates[0].evidence.offline_record,
    { raw_url: NOTE_A_URL },
  );
  assert.equal(
    result.candidates[0].evidence.profile_url,
    'https://www.xiaohongshu.com/user/profile/abc',
  );
  assert.equal('authorization' in result.candidates[0].evidence, false);
  assert.ok(result.discardedSensitiveFields >= 4);
  assert.doesNotMatch(JSON.stringify(result.candidates), /do-not-upload|Bearer secret/);
});

test('parses analyzer-shaped JSONL and retains review evidence snapshots', () => {
  const content = [
    JSON.stringify({
      target: {
        candidate_note_id: NOTE_A,
        candidate_url: `${NOTE_A_URL}?xsec_token=secret`,
        is_collection: true,
        trace_complete: true,
      },
      author: { stable_id: 'author-1', verified: true },
      content_snapshots: {
        body: { text: '正文', sha256: 'body-hash', trusted: true },
        expanded_body: { text: '展开正文', sha256: 'expanded-hash', trusted: true },
        pinned_comment: { text: '置顶补充', author_verified: true, trusted: true },
      },
      activity_window: { starts_at: '2026-07-01', ends_at: '2026-07-31' },
      prizes: ['相机'],
      rule: { required_actions: ['liked'], review_required: true },
      complex_conditions: ['需确认合集原帖'],
      xsecToken: 'secret',
    }),
    JSON.stringify({
      note_id: NOTE_B,
      body_snapshot: '第二条',
    }),
  ].join('\n');

  const result = parseOfflineSearchResult('search.jsonl', content);
  assert.equal(result.targetCount, 2);
  assert.equal(result.candidates[0].raw_url, NOTE_A_URL);
  assert.equal(result.candidates[0].evidence.content_snapshots.body.text, '正文');
  assert.deepEqual(result.candidates[0].rule.required_actions, ['liked']);
  assert.equal(result.candidates[1].raw_url, NOTE_B_URL);
  assert.doesNotMatch(JSON.stringify(result.candidates), /xsec|secret/i);
});

test('parses quoted multiline CSV and discards sensitive columns', () => {
  const content = [
    'note_url,title,body_snapshot,pinned_comment_snapshot,cookie,xsec_token',
    `"${NOTE_A_URL}?xsec_token=secret","抽奖","第一行`,
    `第二行","置顶说明","a1=secret","secret-token"`,
  ].join('\r\n');
  const result = parseOfflineSearchResult('search.csv', content);

  assert.equal(result.targetCount, 1);
  assert.equal(result.candidates[0].raw_url, NOTE_A_URL);
  assert.equal(result.candidates[0].title, '抽奖');
  assert.equal(result.candidates[0].evidence.body_snapshot, '第一行\n第二行');
  assert.equal(result.candidates[0].evidence.pinned_comment_snapshot, '置顶说明');
  assert.ok(result.discardedSensitiveFields >= 3);
  assert.doesNotMatch(JSON.stringify(result.candidates), /a1=|secret-token/);
});

test('deduplicates offline rows and reports invalid rows locally', () => {
  const result = parseOfflineSearchResult('search.json', JSON.stringify([
    { note_id: NOTE_A, title: 'first' },
    { raw_url: `${NOTE_A_URL}?xsec_source=pc_search`, title: 'duplicate' },
    { raw_url: 'https://evil.example/not-a-note' },
  ]));

  assert.equal(result.targetCount, 1);
  assert.equal(result.discardedRows, 2);
  assert.equal(result.candidates[0].title, 'first');
});

test('rejects unsupported, malformed, oversized, and targetless offline input', () => {
  assert.throws(
    () => parseOfflineSearchResult('search.txt', NOTE_A_URL),
    expectCode('xhs_target_offline_extension_invalid'),
  );
  assert.throws(
    () => parseOfflineSearchResult('search.json', '{broken'),
    expectCode('xhs_target_offline_invalid_json'),
  );
  assert.throws(
    () => parseOfflineSearchResult('search.json', JSON.stringify([{ title: 'no target' }])),
    expectCode('xhs_target_offline_no_candidates'),
  );
  assert.throws(
    () => parseOfflineSearchResult('search.jsonl', 'not-json'),
    expectCode('xhs_target_offline_invalid_jsonl'),
  );
  assert.throws(
    () => parseOfflineSearchResult('search.json', 'x'.repeat(XHS_OFFLINE_MAX_BYTES + 1)),
    expectCode('xhs_target_offline_too_large'),
  );
});

test('builds a bounded sanitized ingest payload with the exact API contract', () => {
  const payload = buildCandidateIngestPayload(
    {
      source_type: 'offline_search_result',
      source_value: 'results.json',
      tracked_source_id: 42,
    },
    [{
      raw_url: `${NOTE_A_URL}?xsec_token=secret`,
      title: 'A'.repeat(300),
      evidence: {
        body_snapshot: '抽奖规则',
        cookie: 'do-not-upload',
      },
    }],
  );

  assert.deepEqual(payload.source, {
    source_type: 'offline_search_result',
    source_value: 'results.json',
    tracked_source_id: 42,
  });
  assert.equal(payload.candidates[0].raw_url, NOTE_A_URL);
  assert.equal(payload.candidates[0].title.length, 256);
  assert.equal('cookie' in payload.candidates[0].evidence, false);
  assert.ok(new TextEncoder().encode(JSON.stringify(payload)).byteLength < XHS_INGEST_MAX_BYTES);
});

test('rejects a sanitized ingest payload above the API body budget', () => {
  const candidates = Array.from({ length: 20 }, (_, index) => ({
    raw_url: `https://www.xiaohongshu.com/explore/${String(index).padStart(24, 'a')}`,
    evidence: { body_snapshot: '奖'.repeat(60_000) },
  }));
  assert.throws(
    () => buildCandidateIngestPayload(
      { source_type: 'offline_search_result', source_value: 'results.json' },
      candidates,
    ),
    expectCode('xhs_target_offline_output_too_large'),
  );
});

test('normalizes analyzer evidence into the candidate review view', () => {
  const normalized = normalizeXiaohongshuCandidate({
    id: 7,
    version: 3,
    decision_status: 'needs_review',
    decision_reason: '核对原帖',
    evidence: {
      target: {
        candidate_url: NOTE_A_URL,
        is_collection: true,
        trace_complete: true,
      },
      author: { verified: true },
      content_snapshots: {
        body: { text: '正文', trusted: true },
        expanded_body: { text: '展开正文', trusted: true },
        pinned_comment: { text: '补充开奖时间', trusted: true },
      },
      activity_window: { ends_at: '2026-08-01', status: 'active' },
      prizes: ['相机'],
      complex_conditions: ['关注两个账号'],
    },
    rule: JSON.stringify({ required_actions: ['liked', 'followed'] }),
  });

  assert.equal(normalized.rawUrl, NOTE_A_URL);
  assert.deepEqual(normalized.verification, {
    collection: true,
    originalPost: true,
    author: true,
  });
  assert.equal(normalized.bodySnapshot.text, '正文');
  assert.equal(normalized.expandedSnapshot.text, '展开正文');
  assert.equal(normalized.pinnedCommentSnapshot.text, '补充开奖时间');
  assert.deepEqual(normalized.timing, { ends_at: '2026-08-01', status: 'active' });
  assert.deepEqual(normalized.prize, ['相机']);
  assert.deepEqual(normalized.actions, ['liked', 'followed']);
  assert.deepEqual(normalized.complexConditions, ['关注两个账号']);
});

test('extracts list envelopes and builds exact optimistic-lock decision payloads', () => {
  assert.deepEqual(candidateItemsFromResponse({ items: [{ id: 1 }], total: 1 }), [{ id: 1 }]);
  assert.deepEqual(candidateItemsFromResponse([{ id: 2 }]), [{ id: 2 }]);
  assert.deepEqual(candidateItemsFromResponse({ candidates: [{ id: 3 }] }), []);

  const candidate = { id: 9, version: 4, decisionStatus: 'pending' };
  assert.equal(candidateDecisionCanSubmit(candidate, 'accepted'), true);
  assert.equal(candidateDecisionCanSubmit(candidate, 'pending'), false);
  assert.equal(candidateDecisionCanSubmit(
    { ...candidate, decisionStatus: 'accepted' },
    'skipped',
  ), false);
  assert.deepEqual(
    buildCandidateDecisionPayload('needs_review', '核对作者', 4),
    {
      decision_status: 'needs_review',
      expected_version: 4,
      decision_reason: '核对作者',
    },
  );
  assert.deepEqual(
    buildCandidateDecisionPayload('skipped', '', 4),
    {
      decision_status: 'skipped',
      expected_version: 4,
    },
  );
  assert.throws(
    () => buildCandidateDecisionPayload('accepted', '', 0),
    expectCode('xhs_target_decision_version_invalid'),
  );
});
