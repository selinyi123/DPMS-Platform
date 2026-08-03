import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  DEFAULT_PLATFORM_MODULE_ID,
  lotteryPagePlatformModuleDemand,
} from './platformModuleDemand.js';

test('an empty lottery page initially requests only its selected platform', () => {
  assert.equal(DEFAULT_PLATFORM_MODULE_ID, 'bilibili');
  assert.deepEqual(lotteryPagePlatformModuleDemand(), ['bilibili']);
});

test('lottery page demand adds only edited or business-visible platforms', () => {
  assert.deepEqual(
    lotteryPagePlatformModuleDemand({
      createPlatform: 'weibo',
      importPlatform: 'bilibili',
      sourcePlatform: 'weibo',
      lotteries: [
        { platform: 'xiaohongshu' },
        { platform: 'xiaohongshu' },
      ],
      strategyQueue: [
        { platform: 'douyin' },
        { platform: 'not-registered' },
      ],
    }),
    ['weibo', 'bilibili', 'xiaohongshu', 'douyin'],
  );
});

test('platform boundary does not eagerly request every registered platform', async () => {
  const source = await readFile(
    new URL('./components/PlatformModuleBoundary.jsx', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(source, /startLoads\s*\(\s*registeredPlatformIds\s*\(\s*\)\s*\)/);
  assert.match(source, /requestPlatformModules=/);
});
