import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { bilibiliImportPolicy } from './bilibili/import.js';
import { PLATFORM_IMPORT_POLICIES } from './importPolicies.js';
import {
  MAX_TARGET_LENGTH,
  TARGET_IMPORT_FILE_MAX_BYTES,
  normalizeImportWithPolicy,
  parseSecureHttpsTarget,
} from './importShared.js';
import {
  isTargetImportPolicyModuleLoadFailure,
  loadTargetImportPolicyModule,
  normalizeTargetImportWithPolicyLoader,
} from './importRuntime.js';
import { normalizePlatformTargetImport } from './index.js';
import * as legacyImportFacade from '../xiaohongshuImport.js';

const OPTIONS = {
  allowedPlatformIds: ['bilibili', 'douyin', 'weibo', 'xiaohongshu'],
};

test('runtime import loads only the selected or explicitly declared platform policies', async () => {
  const directLoads = [];
  const direct = await normalizeTargetImportWithPolicyLoader(
    'bilibili',
    'https://t.bilibili.com/123?from=share',
    OPTIONS,
    async (platformId) => {
      directLoads.push(platformId);
      return PLATFORM_IMPORT_POLICIES[platformId];
    },
  );
  assert.equal(direct.content, 'https://t.bilibili.com/123');
  assert.deepEqual(directLoads, ['bilibili']);

  const mixedLoads = [];
  const mixed = await normalizeTargetImportWithPolicyLoader(
    'bilibili',
    [
      'platform,url,score',
      'bilibili,https://t.bilibili.com/123,75',
      'xiaohongshu,https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1,80',
    ].join('\n'),
    OPTIONS,
    async (platformId) => {
      mixedLoads.push(platformId);
      if (platformId === 'douyin' || platformId === 'weibo') {
        throw new Error(`unexpected_peer_policy_load:${platformId}`);
      }
      return PLATFORM_IMPORT_POLICIES[platformId];
    },
  );
  assert.deepEqual(mixedLoads.sort(), ['bilibili', 'xiaohongshu']);
  assert.deepEqual(mixed.content.split('\n'), [
    'bilibili,https://t.bilibili.com/123,75',
    'xiaohongshu,https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1,80',
  ]);
});

test('empty control-plane platform metadata cannot discard registered mixed DPMS rows', async () => {
  const content = [
    'platform,url,score',
    'bilibili,https://t.bilibili.com/123,75',
    'douyin,https://www.douyin.com/video/7300000000000000000,80',
    'weibo,https://weibo.com/detail/PCAGRFqKj,70',
    'xiaohongshu,https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1,60',
  ].join('\n');

  const normalized = await normalizePlatformTargetImport(
    'bilibili',
    content,
    { allowedPlatformIds: [] },
  );

  assert.deepEqual(normalized.content.split('\n'), [
    'bilibili,https://t.bilibili.com/123,75',
    'douyin,https://www.douyin.com/video/7300000000000000000,80',
    'weibo,https://weibo.com/detail/PCAGRFqKj,70',
    'xiaohongshu,https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1,60',
  ]);
  assert.equal(normalized.discardedRows, 0);
  assert.equal(normalized.targetCount, 4);
});

test('explicit single-platform DPMS files use their declared platform size limit', async () => {
  const policies = {
    bilibili: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.bilibili,
      maxBytes: 80,
      tooLargeErrorCode: 'bilibili_import_too_large_test',
    }),
    douyin: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.douyin,
      maxBytes: 90,
      tooLargeErrorCode: 'douyin_import_too_large_test',
    }),
    xiaohongshu: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.xiaohongshu,
      maxBytes: 1_000,
    }),
  };
  const content = [
    'platform,url,score',
    'xiaohongshu,https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1,80',
    `# ${'shared file comment '.repeat(8)}`,
  ].join('\n');
  assert.ok(new TextEncoder().encode(content).byteLength > policies.bilibili.maxBytes);
  assert.ok(new TextEncoder().encode(content).byteLength > policies.douyin.maxBytes);

  const normalizeWithSelected = selected => normalizeTargetImportWithPolicyLoader(
    selected,
    content,
    OPTIONS,
    async platformId => policies[platformId] || PLATFORM_IMPORT_POLICIES[platformId],
  );
  const [fromBilibili, fromDouyin] = await Promise.all([
    normalizeWithSelected('bilibili'),
    normalizeWithSelected('douyin'),
  ]);

  assert.equal(
    fromBilibili.content,
    'xiaohongshu,https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1,80',
  );
  assert.equal(fromDouyin.content, fromBilibili.content);
});

test('mixed DPMS files enforce raw UTF-8 bytes against each owning platform', async () => {
  const policies = {
    bilibili: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.bilibili,
      maxBytes: 120,
      tooLargeErrorCode: 'bilibili_import_too_large_test',
    }),
    xiaohongshu: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.xiaohongshu,
      maxBytes: 1_000,
    }),
  };
  const paddedBilibiliRow = [
    'bilibili',
    'https://t.bilibili.com/123',
    '75',
    '',
    `"${'界'.repeat(40)}\n${'padding'.repeat(12)}"`,
  ].join(',');
  const content = [
    'platform,url,score',
    paddedBilibiliRow,
    'xiaohongshu,https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1,80',
  ].join('\n');

  await assert.rejects(
    normalizeTargetImportWithPolicyLoader(
      'xiaohongshu',
      content,
      OPTIONS,
      async platformId => policies[platformId] || PLATFORM_IMPORT_POLICIES[platformId],
    ),
    error => error?.code === 'bilibili_import_too_large_test'
      && error.platformId === 'bilibili'
      && error.byteLength > error.maxBytes
      && error.maxBytes === policies.bilibili.maxBytes,
  );
});

test('implicit single-platform imports cannot borrow a peer platform size budget', async () => {
  const bilibiliPolicy = Object.freeze({
    ...PLATFORM_IMPORT_POLICIES.bilibili,
    maxBytes: 80,
    tooLargeErrorCode: 'bilibili_import_too_large_test',
  });
  const content = [
    'https://t.bilibili.com/123',
    `# ${'padding'.repeat(20)}`,
  ].join('\n');

  await assert.rejects(
    normalizeTargetImportWithPolicyLoader(
      'bilibili',
      content,
      OPTIONS,
      async platformId => (
        platformId === 'bilibili'
          ? bilibiliPolicy
          : PLATFORM_IMPORT_POLICIES[platformId]
      ),
    ),
    error => error?.code === 'bilibili_import_too_large_test'
      && error.platformId === 'bilibili',
  );
});

test('shared import runtime has no static dependency on a platform policy', async () => {
  const source = await readFile(new URL('./importRuntime.js', import.meta.url), 'utf8');
  assert.doesNotMatch(
    source,
    /^\s*import\s+[\s\S]*?from\s+['"]\.\/(?:bilibili|douyin|weibo|xiaohongshu)\//mu,
  );
});

test('descriptor import bridge keeps the central runtime edge dynamic and acyclic', async () => {
  const source = await readFile(
    new URL('./descriptorImportRuntime.js', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(
    source,
    /^\s*import\s+[\s\S]*?from\s+['"]\.\/importRuntime\.js['"]/mu,
  );
  assert.match(source, /await import\(\s*['"]\.\/importRuntime\.js['"]\s*\)/mu);
});

test('policy chunk loader brands failures without exposing deployment asset details', async () => {
  await assert.rejects(
    loadTargetImportPolicyModule(
      'weibo',
      async () => {
        throw new TypeError(
          'Failed to fetch dynamically imported module: /assets/weibo-private-build.js',
        );
      },
    ),
    error => (
      isTargetImportPolicyModuleLoadFailure(error)
      && error.name === 'TargetImportPolicyModuleLoadError'
      && error.message === 'target_import_policy_module_load_failed'
      && !error.message.includes('/assets/')
    ),
  );
  assert.equal(
    isTargetImportPolicyModuleLoadFailure(
      Object.assign(new Error('target_import_policy_module_load_failed'), {
        name: 'TargetImportPolicyModuleLoadError',
      }),
    ),
    false,
  );
});

test('policy evaluation and contract failures are never branded as chunk failures', async () => {
  const evaluationError = new ReferenceError(
    'policy evaluation failed near /assets/private-policy.js',
  );
  await assert.rejects(
    loadTargetImportPolicyModule(
      'weibo',
      async () => {
        throw evaluationError;
      },
    ),
    error => (
      error === evaluationError
      && !isTargetImportPolicyModuleLoadFailure(error)
    ),
  );

  const policyWithInvalidContract = await loadTargetImportPolicyModule(
    'weibo',
    async () => ({ id: 'douyin' }),
  );
  assert.deepEqual(policyWithInvalidContract, { id: 'douyin' });
  assert.equal(
    isTargetImportPolicyModuleLoadFailure(policyWithInvalidContract),
    false,
  );
});

test('arbitrary TypeErrors cannot impersonate a policy chunk load failure', async () => {
  const policyBug = new TypeError('normalizeUrl is not a function');
  await assert.rejects(
    loadTargetImportPolicyModule(
      'douyin',
      async () => {
        throw policyBug;
      },
    ),
    error => error === policyBug,
  );
});

test('file read preflight uses a selection-independent shared safety ceiling', async () => {
  const source = await readFile(new URL('../pages/Lotteries.jsx', import.meta.url), 'utf8');
  assert.equal(TARGET_IMPORT_FILE_MAX_BYTES, 10_000_000);
  assert.match(source, /file\.size > TARGET_IMPORT_FILE_MAX_BYTES/);
  assert.doesNotMatch(source, /file\.size > platformImportMaxBytes/);
  assert.match(source, /readTargetImportFile\(file\)/);
  assert.doesNotMatch(source, /await file\.text\(\)/);
});

test('foreign structured detectors cannot change a selected platform rejection', async () => {
  const loads = [];
  await assert.rejects(
    normalizeTargetImportWithPolicyLoader(
      'bilibili',
      JSON.stringify({
        aweme_id: '7300000000000000000',
        aweme_type: 0,
        title: 'foreign export',
      }),
      OPTIONS,
      async (platformId) => {
        loads.push(platformId);
        if (platformId !== 'bilibili') {
          throw new Error(`unexpected_foreign_detector:${platformId}`);
        }
        return PLATFORM_IMPORT_POLICIES[platformId];
      },
    ),
    error => error instanceof Error
      && error.code === 'target_import_structured_requires_platform',
  );
  assert.deepEqual(loads, ['bilibili']);
});

test('structured complexity errors remain owned by the selected platform', () => {
  let deeplyNested = 'ignored';
  for (let depth = 0; depth <= 32; depth += 1) {
    deeplyNested = [deeplyNested];
  }

  assert.throws(
    () => PLATFORM_IMPORT_POLICIES.douyin.normalizeStructuredImport(
      JSON.stringify({
        aweme_id: '7300000000000000000',
        title: deeplyNested,
      }),
    ),
    error => error?.code === 'douyin_import_too_complex',
  );
  assert.throws(
    () => PLATFORM_IMPORT_POLICIES.xiaohongshu.normalizeStructuredImport(
      JSON.stringify({ title: deeplyNested }),
    ),
    error => error?.code === 'xiaohongshu_import_too_complex',
  );
});

test('target import length matches the Core raw_url storage boundary', () => {
  const prefix = 'https://www.bilibili.com/video/BV1xx411c7mD?context=';
  const atLimit = prefix + 'x'.repeat(MAX_TARGET_LENGTH - prefix.length);
  const overLimit = `${atLimit}x`;

  assert.equal(MAX_TARGET_LENGTH, 512);
  assert.ok(parseSecureHttpsTarget(atLimit));
  assert.equal(parseSecureHttpsTarget(overLimit), null);

  // URL parsing percent-encodes Unicode paths. Re-check the normalized value,
  // not only the shorter source string, before it can reach Core.
  const expandedShortLink = `https://v.douyin.com/${'你'.repeat(100)}`;
  assert.ok(expandedShortLink.length < MAX_TARGET_LENGTH);
  assert.equal(PLATFORM_IMPORT_POLICIES.douyin.normalizeUrl(expandedShortLink), null);
});

test('replacing one platform import policy cannot change a peer policy result', () => {
  const xhsTarget = 'https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1';
  const weiboTarget = 'https://weibo.com/detail/PCAGRFqKj';
  const content = [
    `xiaohongshu,${xhsTarget}`,
    `weibo,${weiboTarget}`,
  ].join('\n');
  const baseline = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    content,
    OPTIONS,
    PLATFORM_IMPORT_POLICIES,
  );
  const replacementPolicies = Object.freeze({
    ...PLATFORM_IMPORT_POLICIES,
    weibo: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.weibo,
      normalizeUrl() {
        return 'https://weibo.com/detail/Replacement9';
      },
    }),
  });
  const isolated = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    content,
    OPTIONS,
    replacementPolicies,
  );

  assert.deepEqual(baseline.content.split('\n'), [
    'xiaohongshu,https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1',
    `weibo,${weiboTarget}`,
  ]);
  assert.deepEqual(isolated.content.split('\n'), [
    'xiaohongshu,https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1',
    'weibo,https://weibo.com/detail/Replacement9',
  ]);
  assert.equal(
    PLATFORM_IMPORT_POLICIES.xiaohongshu.normalizeUrl(xhsTarget),
    'https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1',
  );
});

test('mixed short-link budgets and errors are owned by each declared platform', () => {
  const onePerPlatform = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    [
      'platform,url',
      'douyin,https://v.douyin.com/one',
      'weibo,https://t.cn/AbCd123',
      'xiaohongshu,https://xhslink.com/one',
    ].join('\n'),
    OPTIONS,
    PLATFORM_IMPORT_POLICIES,
  );
  assert.deepEqual(onePerPlatform.content.split('\n'), [
    'douyin,https://v.douyin.com/one',
    'weibo,https://t.cn/AbCd123',
    'xiaohongshu,https://xhslink.com/one',
  ]);
  assert.equal(onePerPlatform.shortLinkCount, 3);
  assert.deepEqual(onePerPlatform.shortLinkCountsByPlatform, {
    douyin: 1,
    weibo: 1,
    xiaohongshu: 1,
  });

  const isolatedOverflow = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    [
      'platform,url',
      'douyin,https://v.douyin.com/one',
      'douyin,https://v.douyin.com/two',
      'weibo,https://t.cn/AbCd123',
    ].join('\n'),
    OPTIONS,
    PLATFORM_IMPORT_POLICIES,
  );
  assert.deepEqual(isolatedOverflow.content.split('\n'), [
    'douyin,https://v.douyin.com/one',
    'douyin,https://v.douyin.com/two',
    'weibo,https://t.cn/AbCd123',
  ]);
  assert.equal(isolatedOverflow.discardedRows, 0);
  assert.equal(isolatedOverflow.blockedShortLinkCount, 2);
  assert.equal(isolatedOverflow.targetCount, 3);
  assert.deepEqual(isolatedOverflow.shortLinkCountsByPlatform, {
    weibo: 1,
  });
  assert.deepEqual(isolatedOverflow.shortLinkErrorsByPlatform, {
    douyin: 'douyin_import_short_link_batch_unsupported',
  });

  const allBlockedButIndependentlyOwned = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    [
      'platform,url',
      'douyin,https://v.douyin.com/one',
      'douyin,https://v.douyin.com/two',
      'weibo,https://t.cn/AbCd123',
      'weibo,https://t.cn/DeFg456',
    ].join('\n'),
    OPTIONS,
    PLATFORM_IMPORT_POLICIES,
  );
  assert.equal(allBlockedButIndependentlyOwned.blockedShortLinkCount, 4);
  assert.equal(allBlockedButIndependentlyOwned.targetCount, 4);
  assert.deepEqual(allBlockedButIndependentlyOwned.shortLinkCountsByPlatform, {});
  assert.deepEqual(allBlockedButIndependentlyOwned.shortLinkErrorsByPlatform, {
    douyin: 'douyin_import_short_link_batch_unsupported',
    weibo: 'weibo_import_short_link_batch_unsupported',
  });

  const allSelectedShortLinks = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    'https://b23.tv/one\nhttps://b23.tv/two',
    OPTIONS,
    PLATFORM_IMPORT_POLICIES,
  );
  assert.equal(allSelectedShortLinks.targetCount, 2);
  assert.equal(allSelectedShortLinks.blockedShortLinkCount, 2);
  assert.deepEqual(allSelectedShortLinks.shortLinkErrorsByPlatform, {
    bilibili: 'xiaohongshu_import_short_link_batch_unsupported',
  });
  assert.equal(
    allSelectedShortLinks.content,
    'https://b23.tv/one\nhttps://b23.tv/two',
  );
});

test('changing one short-link host list cannot reject another platform rows', () => {
  const content = [
    'platform,url',
    'douyin,https://v.douyin.com/one',
    'weibo,https://weibo.com/detail/PCAGRFqKj',
  ].join('\n');
  const replacementPolicies = Object.freeze({
    ...PLATFORM_IMPORT_POLICIES,
    weibo: Object.freeze({
      ...PLATFORM_IMPORT_POLICIES.weibo,
      shortLinkHosts: Object.freeze(['weibo.com']),
    }),
  });
  const result = normalizeImportWithPolicy(
    bilibiliImportPolicy,
    content,
    OPTIONS,
    replacementPolicies,
  );

  assert.deepEqual(result.content.split('\n'), [
    'douyin,https://v.douyin.com/one',
    'weibo,https://weibo.com/detail/PCAGRFqKj',
  ]);
  assert.deepEqual(result.shortLinkCountsByPlatform, {
    douyin: 1,
    weibo: 1,
  });
});

test('legacy Xiaohongshu importer facade retains every historical named export', () => {
  for (const exportName of [
    'DOUYIN_IMPORT_MAX_BYTES',
    'TARGET_IMPORT_PASSTHROUGH_MAX_BYTES',
    'TargetImportError',
    'WEIBO_IMPORT_MAX_BYTES',
    'XIAOHONGSHU_IMPORT_MAX_BYTES',
    'XiaohongshuImportError',
    'looksLikeStructuredTargetExport',
    'normalizeDouyinTargetImport',
    'normalizeTargetImportForPlatform',
    'normalizeWeiboTargetImport',
    'normalizeXiaohongshuTargetImport',
  ]) {
    assert.ok(exportName in legacyImportFacade, `missing legacy export: ${exportName}`);
  }
  assert.equal(legacyImportFacade.XiaohongshuImportError, legacyImportFacade.TargetImportError);
});
