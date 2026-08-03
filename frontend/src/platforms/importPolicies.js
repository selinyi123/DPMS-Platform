import { bilibiliImportPolicy } from './bilibili/import.js';
import { douyinImportPolicy } from './douyin/import.js';
import { weiboImportPolicy } from './weibo/import.js';
import { xiaohongshuImportPolicy } from './xiaohongshu/import.js';

export const PLATFORM_IMPORT_POLICIES = Object.freeze({
  bilibili: bilibiliImportPolicy,
  douyin: douyinImportPolicy,
  weibo: weiboImportPolicy,
  xiaohongshu: xiaohongshuImportPolicy,
});
