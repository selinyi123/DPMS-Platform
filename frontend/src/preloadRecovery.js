const PRELOAD_RELOAD_AT_KEY = 'dpms:vite-preload-reload-at';
const PRELOAD_RELOAD_HISTORY_KEY = '__dpmsVitePreloadReloadAt';
const PRELOAD_RELOAD_SCOPES_KEY = 'dpms:vite-preload-reload-scopes';
const PRELOAD_RELOAD_HISTORY_SCOPES_KEY = '__dpmsVitePreloadReloadScopes';
const MODULE_GRAPH_ERROR_EVENTS = Object.freeze([
  'vite:preloadError',
  'dpms:moduleWorkerError',
]);

function stableModuleBuildId(value) {
  const source = String(value || '');
  let hash = 0x811c9dc5;
  for (let index = 0; index < source.length; index += 1) {
    hash ^= source.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `module-${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

const PRELOAD_MODULE_BUILD_ID = stableModuleBuildId(import.meta.url);

function finiteScopeList(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(
    value
      .filter(item => typeof item === 'string' && item.length <= 96),
  )];
}

function storedReloadScopes(storage) {
  try {
    const rawValue = storage?.getItem(PRELOAD_RELOAD_SCOPES_KEY);
    if (!rawValue) return [];
    return finiteScopeList(JSON.parse(rawValue));
  } catch {
    return [];
  }
}

function historyReloadScopes(history) {
  try {
    return finiteScopeList(
      history?.state?.[PRELOAD_RELOAD_HISTORY_SCOPES_KEY],
    );
  } catch {
    return [];
  }
}

function persistHistoryRecovery(browser, scopes, timestamp) {
  try {
    const history = browser?.history;
    if (typeof history?.replaceState !== 'function') return;
    const existing = (
      history.state !== null
      && typeof history.state === 'object'
      && !Array.isArray(history.state)
    ) ? history.state : {};
    history.replaceState(
      {
        ...existing,
        [PRELOAD_RELOAD_HISTORY_KEY]: timestamp,
        [PRELOAD_RELOAD_HISTORY_SCOPES_KEY]: scopes,
      },
      '',
      browser.location?.href,
    );
  } catch {
    // Some embedded browsers restrict History state. Session storage remains
    // the primary cross-reload guard where available.
  }
}

function eventRecoveryScope(event, defaultBuildId) {
  const eventBuildId = String(event?.detail?.buildId || '').slice(0, 128);
  const buildId = stableModuleBuildId(eventBuildId || defaultBuildId);
  const candidatePlatform = String(event?.detail?.platformId || '');
  const platformId = /^[a-z][a-z0-9_]{1,31}$/.test(candidatePlatform)
    ? candidatePlatform
    : 'shared';
  return `${buildId}:${platformId}`;
}

export function installPreloadErrorRecovery(
  browser = globalThis,
  {
    now = () => Date.now(),
    buildId = PRELOAD_MODULE_BUILD_ID,
  } = {},
) {
  if (
    !browser
    || typeof browser.addEventListener !== 'function'
    || typeof browser.location?.reload !== 'function'
  ) {
    throw new Error('preload_error_recovery_unavailable');
  }

  const inMemoryScopes = new Set();
  const onPreloadError = (event) => {
    const currentTime = Number(now());
    if (!Number.isFinite(currentTime)) return;
    let storage;
    try {
      storage = browser.sessionStorage;
    } catch {
      storage = null;
    }
    const scope = eventRecoveryScope(event, buildId);
    const previousScopes = new Set([
      ...inMemoryScopes,
      ...storedReloadScopes(storage),
      ...historyReloadScopes(browser.history),
    ]);
    if (previousScopes.has(scope)) {
      // Do not suppress the second error: the page-level boundary then
      // presents its explicit reload action instead of entering a loop.
      return;
    }
    previousScopes.add(scope);
    const persistedScopes = [...previousScopes];
    try {
      storage?.setItem(
        PRELOAD_RELOAD_AT_KEY,
        String(currentTime),
      );
      storage?.setItem(
        PRELOAD_RELOAD_SCOPES_KEY,
        JSON.stringify(persistedScopes),
      );
    } catch {
      // Storage may be disabled; History state below survives a normal reload
      // in the same browsing context without changing the visible URL.
    }
    persistHistoryRecovery(browser, persistedScopes, currentTime);
    inMemoryScopes.add(scope);
    event?.preventDefault?.();
    browser.location.reload();
  };

  for (const eventName of MODULE_GRAPH_ERROR_EVENTS) {
    browser.addEventListener(eventName, onPreloadError);
  }
  return () => {
    for (const eventName of MODULE_GRAPH_ERROR_EVENTS) {
      browser.removeEventListener?.(eventName, onPreloadError);
    }
  };
}

export {
  PRELOAD_RELOAD_AT_KEY,
  PRELOAD_RELOAD_HISTORY_KEY,
  PRELOAD_RELOAD_SCOPES_KEY,
  PRELOAD_RELOAD_HISTORY_SCOPES_KEY,
  PRELOAD_MODULE_BUILD_ID,
  MODULE_GRAPH_ERROR_EVENTS,
};
