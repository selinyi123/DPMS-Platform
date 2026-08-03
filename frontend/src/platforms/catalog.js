export const PLATFORM_IDS = Object.freeze([
  'bilibili',
  'douyin',
  'weibo',
  'xiaohongshu',
]);

export const BILIBILI_EXECUTION_PATH_ID = 'bilibili_api_v2';
export const DOUYIN_DEVICE_EXECUTION_PATH_ID = 'douyin_device_v1';
export const DOUYIN_MANUAL_EXECUTION_PATH_ID = 'douyin_manual_v1';
// Compatibility alias: this name represents the platform default.
export const DOUYIN_EXECUTION_PATH_ID = DOUYIN_DEVICE_EXECUTION_PATH_ID;
export const DOUYIN_IMPORT_MAX_BYTES = 10_000_000;
export const WEIBO_OAUTH_EXECUTION_PATH_ID = 'weibo_oauth_v1';
export const WEIBO_MANUAL_EXECUTION_PATH_ID = 'weibo_manual_v1';
export const WEIBO_IMPORT_MAX_BYTES = 10_000_000;
export const WEIBO_MAX_UNIQUE_HANDLES = 32;
export const XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID = 'xiaohongshu_browser_v1';
export const XIAOHONGSHU_MANUAL_EXECUTION_PATH_ID = 'xiaohongshu_manual_v1';
// Compatibility alias: this name has always represented the platform default.
export const XIAOHONGSHU_EXECUTION_PATH_ID = XIAOHONGSHU_BROWSER_EXECUTION_PATH_ID;
export const XIAOHONGSHU_IMPORT_MAX_BYTES = 10_000_000;

export function normalizePlatformId(platform) {
  return String(platform || '').trim().toLowerCase();
}

export function isRegisteredPlatformId(platform) {
  return PLATFORM_IDS.includes(normalizePlatformId(platform));
}
