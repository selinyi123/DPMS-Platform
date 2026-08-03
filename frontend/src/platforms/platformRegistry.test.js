import assert from 'node:assert/strict';
import test from 'node:test';

import { dispatchSafetyBlocker } from '../lotteryCompatibility.js';
import {
  DOUYIN_DEVICE_EXECUTION_PATH_ID,
  DOUYIN_MANUAL_EXECUTION_PATH_ID,
  WEIBO_MANUAL_EXECUTION_PATH_ID,
  WEIBO_OAUTH_EXECUTION_PATH_ID,
  XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
  accountMatchesPlatformDispatch,
  actionsFollowPlatformModuleOrder,
  buildPlatformAccountCredentialIndex,
  eligibleAccountsForPlatformDispatch,
  eligibleAccountCountForPlatformDispatch,
  hasEligibleAccountForPlatformDispatch,
  loadPlatformModuleBounded,
  loadPlatformModulesIndependently,
  loadRegisteredPlatformModules,
  normalizePlatformTargetImport,
  platformAccountCredentialKinds,
  platformDiscoverySourceTypes,
  platformExecutionPaths,
  platformLotteryActions,
  platformModeBlocker,
  platformModule,
  platformModuleLoadState,
  platformSupportsDiscoverySource,
  platformSupportsExecutionPath,
  registeredPlatformModules,
  resolvePlatformExecutionPath,
  settlePlatformModuleLoads,
} from './index.js';
import {
  normalizeDouyinTargetImport,
  normalizeWeiboTargetImport,
} from '../xiaohongshuImport.js';
import { normalizeDouyinTargetImport as normalizeDouyinPlatformImport } from './douyin/index.js';
import { normalizeWeiboTargetImport as normalizeWeiboPlatformImport } from './weibo/index.js';

const browserAccount = Object.freeze({
  id: 1,
  platform: 'weibo',
  credential_kind: 'browser_session',
});
const oauthAccount = Object.freeze({
  id: 2,
  platform: 'weibo',
  credential_kind: 'weibo_oauth',
});

await loadRegisteredPlatformModules();

test('one failed platform module load does not block peer modules', async () => {
  const fakeModules = {
    bilibili: { id: 'bilibili' },
    weibo: { id: 'weibo' },
  };
  const result = await settlePlatformModuleLoads(
    ['bilibili', 'douyin', 'weibo'],
    async (platformId) => {
      if (platformId === 'douyin') throw new Error('synthetic_douyin_import_failure');
      return fakeModules[platformId];
    },
  );
  assert.deepEqual(result.modules.map(module => module.id), ['bilibili', 'weibo']);
  assert.match(result.failures.douyin.message, /synthetic_douyin_import_failure/);
});

test('independent platform loads publish peers without awaiting a stuck module', async () => {
  const stuck = new Promise(() => {});
  const settlements = [];
  const handles = loadPlatformModulesIndependently(
    ['bilibili', 'douyin', 'weibo'],
    {
      loadModule: async (platformId) => {
        if (platformId === 'douyin') return stuck;
        return { id: platformId };
      },
      onSettled: result => settlements.push(result.platformId),
    },
  );

  const peerResults = await Promise.all([handles[0], handles[2]]);
  assert.deepEqual(
    peerResults.map(result => [result.platformId, result.status]),
    [
      ['bilibili', 'fulfilled'],
      ['weibo', 'fulfilled'],
    ],
  );
  assert.deepEqual(settlements.sort(), ['bilibili', 'weibo']);
});

test('a stuck platform module load fails within its own bounded timeout', async () => {
  const peer = loadPlatformModuleBounded(
    'bilibili',
    async () => ({ id: 'bilibili' }),
    20,
  );
  const stuck = loadPlatformModuleBounded(
    'douyin',
    () => new Promise(() => {}),
    20,
  );
  assert.deepEqual(await peer, { id: 'bilibili' });
  await assert.rejects(
    stuck,
    error => error?.code === 'platform_module_load_timeout'
      && error.platformId === 'douyin',
  );
});

test('registers four isolated platform capability descriptors', () => {
  const modules = registeredPlatformModules();
  assert.deepEqual(modules.map(module => module.id).sort(), [
    'bilibili', 'douyin', 'weibo', 'xiaohongshu',
  ]);
  assert.equal(new Set(modules.map(module => module.actions)).size, 4);
  assert.equal(new Set(modules.map(module => module.discoverySourceTypes)).size, 4);
  assert.equal(new Set(modules.map(module => module.targetKinds)).size, 4);
  assert.equal(new Set(modules.map(module => module.strategy)).size, 4);
  for (const module of modules) {
    assert.equal(Object.isFrozen(module), true);
    assert.equal(Object.isFrozen(module.actions), true);
    assert.equal(Object.isFrozen(module.discoverySourceTypes), true);
    assert.equal(Object.isFrozen(module.targetKinds), true);
    assert.equal(Object.isFrozen(module.strategy), true);
  }
  assert.equal(platformModuleLoadState('bilibili').status, 'ready');
  assert.equal(platformModuleLoadState('not-a-platform').status, 'unsupported');
  assert.equal(
    platformModeBlocker('not-a-platform', 'real_run'),
    'platform_module_unavailable',
  );
});

test('target kinds remain explicit and platform-owned', () => {
  assert.deepEqual(platformModule('bilibili').targetKinds, ['dynamic', 'video', 'article']);
  assert.deepEqual(platformModule('weibo').targetKinds, ['status']);
  assert.deepEqual(platformModule('xiaohongshu').targetKinds, ['note']);
  assert.deepEqual(platformModule('douyin').targetKinds, ['video', 'note']);
  assert.deepEqual(platformModule('bilibili').realTargetKinds, ['dynamic']);
  assert.deepEqual(platformModule('weibo').realTargetKinds, ['status']);
  assert.deepEqual(platformModule('xiaohongshu').realTargetKinds, ['note']);
  assert.deepEqual(platformModule('douyin').realTargetKinds, ['video', 'note']);
});

test('discovery source capabilities stay platform-local and fail closed', () => {
  assert.deepEqual(platformDiscoverySourceTypes('bilibili'), ['url_list', 'keyword', 'up']);
  for (const platform of ['weibo', 'xiaohongshu', 'douyin']) {
    assert.deepEqual(platformDiscoverySourceTypes(platform), ['url_list']);
    assert.equal(platformSupportsDiscoverySource(platform, 'keyword'), false);
    assert.equal(platformSupportsDiscoverySource(platform, 'up'), false);
    assert.equal(platformSupportsDiscoverySource(platform, 'url_list'), true);
  }
  assert.deepEqual(platformDiscoverySourceTypes('unregistered'), []);
  assert.equal(platformSupportsDiscoverySource('unregistered', 'url_list'), false);
});

test('actions and execution paths are resolved by the owning platform', () => {
  assert.deepEqual(platformLotteryActions('bilibili'), ['followed', 'liked', 'commented', 'reposted']);
  assert.deepEqual(platformLotteryActions('xiaohongshu'), ['followed', 'liked', 'commented', 'favorited']);
  assert.deepEqual(platformLotteryActions('douyin'), ['followed', 'liked', 'commented', 'favorited']);
  assert.deepEqual(platformLotteryActions('weibo'), ['followed', 'liked', 'commented', 'favorited', 'reposted']);
  assert.equal(resolvePlatformExecutionPath('weibo', ''), WEIBO_OAUTH_EXECUTION_PATH_ID);
  assert.equal(resolvePlatformExecutionPath('weibo', 'invalid'), 'invalid');
  assert.equal(platformSupportsExecutionPath('weibo', 'invalid'), false);
  assert.deepEqual(platformExecutionPaths('weibo'), [
    WEIBO_OAUTH_EXECUTION_PATH_ID,
    WEIBO_MANUAL_EXECUTION_PATH_ID,
  ]);
  assert.equal(platformModeBlocker('weibo', 'dry_run', 'invalid'), 'execution_path_mismatch');
  assert.equal(accountMatchesPlatformDispatch(oauthAccount, 'weibo', 'dry_run', 'invalid'), false);
  assert.equal(
    resolvePlatformExecutionPath('weibo', WEIBO_MANUAL_EXECUTION_PATH_ID),
    WEIBO_MANUAL_EXECUTION_PATH_ID,
  );
  assert.equal(
    resolvePlatformExecutionPath('xiaohongshu', ''),
    XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  );
  assert.deepEqual(platformExecutionPaths('xiaohongshu'), [
    XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
  ]);
  assert.equal(
    resolvePlatformExecutionPath('douyin', ''),
    DOUYIN_DEVICE_EXECUTION_PATH_ID,
  );
  assert.deepEqual(platformExecutionPaths('douyin'), [
    DOUYIN_DEVICE_EXECUTION_PATH_ID,
    DOUYIN_MANUAL_EXECUTION_PATH_ID,
  ]);
  for (const [platform, defaultPath] of [
    ['bilibili', 'bilibili_api_v2'],
  ]) {
    assert.equal(resolvePlatformExecutionPath(platform, ''), defaultPath);
    assert.equal(resolvePlatformExecutionPath(platform, 'foreign_path_v9'), 'foreign_path_v9');
    assert.equal(platformSupportsExecutionPath(platform, 'foreign_path_v9'), false);
    assert.equal(platformModeBlocker(platform, 'shadow_run', 'foreign_path_v9'), 'execution_path_mismatch');
  }
});

test('changing one platform action order does not alter another platform contract', () => {
  const bilibili = platformModule('bilibili');
  const replacementWeibo = {
    ...platformModule('weibo'),
    actions: ['liked', 'followed', 'commented', 'favorited', 'reposted'],
  };
  assert.equal(
    actionsFollowPlatformModuleOrder(replacementWeibo, ['liked', 'followed', 'reposted']),
    true,
  );
  assert.equal(
    actionsFollowPlatformModuleOrder(replacementWeibo, ['followed', 'liked', 'reposted']),
    false,
  );
  assert.equal(
    actionsFollowPlatformModuleOrder(bilibili, ['followed', 'liked', 'reposted']),
    true,
  );
});

test('Weibo Shadow binds browser sessions while OAuth dry and real bind OAuth credentials', () => {
  for (const executionPathId of [WEIBO_OAUTH_EXECUTION_PATH_ID, WEIBO_MANUAL_EXECUTION_PATH_ID]) {
    assert.equal(
      accountMatchesPlatformDispatch(browserAccount, 'weibo', 'shadow_run', executionPathId),
      true,
    );
    assert.equal(
      accountMatchesPlatformDispatch(oauthAccount, 'weibo', 'shadow_run', executionPathId),
      false,
    );
  }
  for (const mode of ['dry_run', 'real_run']) {
    assert.equal(
      accountMatchesPlatformDispatch(oauthAccount, 'weibo', mode, WEIBO_OAUTH_EXECUTION_PATH_ID),
      true,
    );
    assert.equal(
      accountMatchesPlatformDispatch(browserAccount, 'weibo', mode, WEIBO_OAUTH_EXECUTION_PATH_ID),
      false,
    );
  }
  assert.deepEqual(
    eligibleAccountsForPlatformDispatch(
      [browserAccount, oauthAccount],
      'weibo',
      'shadow_run',
      WEIBO_OAUTH_EXECUTION_PATH_ID,
    ).map(account => account.id),
    [1],
  );
  const accountIndex = buildPlatformAccountCredentialIndex([browserAccount, oauthAccount]);
  assert.equal(
    eligibleAccountCountForPlatformDispatch(
      accountIndex,
      'weibo',
      'shadow_run',
      WEIBO_OAUTH_EXECUTION_PATH_ID,
    ),
    1,
  );
  assert.equal(
    hasEligibleAccountForPlatformDispatch(
      accountIndex,
      'weibo',
      'real_run',
      WEIBO_OAUTH_EXECUTION_PATH_ID,
    ),
    true,
  );
});

test('blocked execution modes expose no eligible credential kind', () => {
  for (const mode of ['dry_run', 'real_run']) {
    assert.deepEqual(
      platformAccountCredentialKinds('weibo', mode, WEIBO_MANUAL_EXECUTION_PATH_ID),
      [],
    );
    assert.equal(
      accountMatchesPlatformDispatch(
        browserAccount,
        'weibo',
        mode,
        WEIBO_MANUAL_EXECUTION_PATH_ID,
      ),
      false,
    );
    for (const [platform, executionPathId] of [
      ['xiaohongshu', 'xiaohongshu_manual_v1'],
      ['douyin', 'douyin_manual_v1'],
    ]) {
      const account = { platform, credential_kind: 'browser_session' };
      assert.deepEqual(
        platformAccountCredentialKinds(platform, mode, executionPathId),
        [],
      );
      assert.equal(
        accountMatchesPlatformDispatch(account, platform, mode, executionPathId),
        false,
      );
    }
  }
  assert.deepEqual(
    platformAccountCredentialKinds('weibo', 'shadow_run', WEIBO_OAUTH_EXECUTION_PATH_ID),
    ['browser_session'],
  );
  for (const mode of ['dry_run', 'shadow_run', 'real_run']) {
    assert.deepEqual(
      platformAccountCredentialKinds(
        'xiaohongshu',
        mode,
        XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
      ),
      ['browser_session'],
    );
    assert.deepEqual(
      platformAccountCredentialKinds(
        'douyin',
        mode,
        DOUYIN_DEVICE_EXECUTION_PATH_ID,
      ),
      ['device_agent'],
    );
  }
});

test('strategy validation hooks keep platform-specific errors isolated', () => {
  const validationContext = {
    plan: { executable: true, runtime_capability_requirements: {} },
    actions: platformLotteryActions('xiaohongshu'),
    actionsValid: true,
    executionPathId: WEIBO_MANUAL_EXECUTION_PATH_ID,
    sameOrderedList: (left, right) => JSON.stringify(left) === JSON.stringify(right),
    sameJsonValue: (left, right) => JSON.stringify(left) === JSON.stringify(right),
  };
  assert.deepEqual(platformModule('bilibili').strategy.validatePlan(validationContext), []);
  assert.deepEqual(platformModule('xiaohongshu').strategy.validatePlan({
    ...validationContext,
    executionPathId: XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID,
  }), [
    'xiaohongshu_manual_plan_must_be_non_executable',
  ]);
  assert.deepEqual(platformModule('xiaohongshu').strategy.validatePlan({
    ...validationContext,
    executionPathId: XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID,
  }), []);
  assert.deepEqual(platformModule('douyin').strategy.validatePlan({
    ...validationContext,
    executionPathId: DOUYIN_MANUAL_EXECUTION_PATH_ID,
  }), [
    'douyin_manual_plan_must_be_non_executable',
  ]);
  assert.deepEqual(platformModule('douyin').strategy.validatePlan({
    ...validationContext,
    executionPathId: DOUYIN_DEVICE_EXECUTION_PATH_ID,
  }), []);
  assert.deepEqual(platformModule('weibo').strategy.validatePlan(validationContext), [
    'weibo_manual_plan_must_be_non_executable',
  ]);
});

test('dispatch safety reports a selected credential-kind mismatch before queueing', () => {
  assert.equal(dispatchSafetyBlocker({
    lottery: {
      id: 9,
      platform: 'weibo',
      raw_url: 'https://weibo.com/detail/PCAGRFqKj',
      action_plan: { execution_path_id: WEIBO_OAUTH_EXECUTION_PATH_ID },
    },
    mode: 'shadow_run',
    safeAccountAvailable: true,
    accountScopeBound: true,
    accountScopeCompatible: false,
  }), 'account_credential_kind_mismatch');
});

test('platform import entries preserve the legacy public import contract', async () => {
  const weibo = JSON.stringify({
    mblogid: 'PCAGRFqKj',
    mid: '4890123456789012',
    text_raw: 'lottery',
  });
  const douyin = JSON.stringify([{
    aweme_id: '7300000000000000000',
    aweme_type: 0,
    title: 'lottery',
  }]);
  assert.deepEqual(
    await normalizeWeiboPlatformImport(weibo),
    normalizeWeiboTargetImport(weibo),
  );
  assert.deepEqual(
    await normalizeDouyinPlatformImport(douyin),
    normalizeDouyinTargetImport(douyin),
  );
  assert.deepEqual(
    await platformModule('weibo').normalizeTargetImport(weibo),
    await normalizePlatformTargetImport('weibo', weibo),
  );
  await assert.rejects(
    normalizePlatformTargetImport('unregistered', 'https://example.test'),
    /target_import_platform_unsupported/,
  );
});

test('all four descriptor import entries match central mixed-DPMS semantics', async () => {
  const mixed = [
    'platform,url,score',
    'bilibili,https://t.bilibili.com/123?from=share,75',
    'douyin,https://www.douyin.com/video/7300000000000000000?share_token=drop,80',
    'weibo,https://weibo.com/detail/PCAGRFqKj?token=drop,70',
    'xiaohongshu,https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1?xsec_token=drop,60',
  ].join('\n');

  for (const platform of ['bilibili', 'douyin', 'weibo', 'xiaohongshu']) {
    const [descriptorResult, centralResult] = await Promise.all([
      platformModule(platform).normalizeTargetImport(mixed),
      normalizePlatformTargetImport(platform, mixed),
    ]);
    assert.deepEqual(
      descriptorResult,
      centralResult,
      `${platform} descriptor drifted from central mixed import`,
    );
    assert.equal(descriptorResult.targetCount, 4);
    assert.doesNotMatch(descriptorResult.content, /share_token|xsec_token|token=|drop/i);
  }
});
