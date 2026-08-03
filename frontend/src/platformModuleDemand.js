import {
  PLATFORM_IDS,
  normalizePlatformId,
} from './platforms/catalog.js';

export const DEFAULT_PLATFORM_MODULE_ID = 'bilibili';

function addPlatform(target, value) {
  const platformId = normalizePlatformId(value);
  if (PLATFORM_IDS.includes(platformId)) target.add(platformId);
}

function addRowPlatforms(target, rows) {
  for (const row of Array.isArray(rows) ? rows : []) {
    addPlatform(target, row?.platform);
  }
}

export function lotteryPagePlatformModuleDemand({
  createPlatform = DEFAULT_PLATFORM_MODULE_ID,
  importPlatform = DEFAULT_PLATFORM_MODULE_ID,
  sourcePlatform = DEFAULT_PLATFORM_MODULE_ID,
  lotteries = [],
  strategyQueue = [],
} = {}) {
  const demand = new Set();
  addPlatform(demand, createPlatform);
  addPlatform(demand, importPlatform);
  addPlatform(demand, sourcePlatform);
  addRowPlatforms(demand, lotteries);
  addRowPlatforms(demand, strategyQueue);
  return Object.freeze([...demand]);
}
