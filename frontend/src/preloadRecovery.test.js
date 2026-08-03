import assert from 'node:assert/strict';
import test from 'node:test';

import {
  PRELOAD_RELOAD_AT_KEY,
  PRELOAD_RELOAD_SCOPES_KEY,
  installPreloadErrorRecovery,
} from './preloadRecovery.js';

function fakeBrowser() {
  const listeners = new Map();
  const values = new Map();
  let reloads = 0;
  return {
    addEventListener(name, listener) {
      listeners.set(name, listener);
    },
    removeEventListener(name, listener) {
      if (listeners.get(name) === listener) listeners.delete(name);
    },
    location: {
      href: 'https://dpms.example/lotteries',
      reload() {
        reloads += 1;
      },
    },
    history: {
      state: null,
      replaceState(state) {
        this.state = state;
      },
    },
    sessionStorage: {
      getItem(key) {
        return values.get(key) ?? null;
      },
      setItem(key, value) {
        values.set(key, value);
      },
    },
    dispatch(event, name = 'vite:preloadError') {
      listeners.get(name)?.(event);
    },
    reloadCount() {
      return reloads;
    },
    storedValue(key) {
      return values.get(key);
    },
    hasListener(name = 'vite:preloadError') {
      return listeners.has(name);
    },
  };
}

test('a stale deployment chunk reloads at most once for its build scope', () => {
  const browser = fakeBrowser();
  const stop = installPreloadErrorRecovery(
    browser,
    { now: () => 500 },
  );
  let prevented = 0;
  browser.dispatch({ preventDefault: () => { prevented += 1; } });
  browser.dispatch({ preventDefault: () => { prevented += 1; } });

  assert.equal(browser.reloadCount(), 1);
  assert.equal(prevented, 1);
  assert.equal(browser.storedValue(PRELOAD_RELOAD_AT_KEY), '500');
  assert.equal(
    JSON.parse(browser.storedValue(PRELOAD_RELOAD_SCOPES_KEY)).length,
    1,
  );

  stop();
  assert.equal(browser.hasListener(), false);
  assert.equal(browser.hasListener('dpms:moduleWorkerError'), false);
});

test('the same build cannot auto-reload again during the browser session', () => {
  const browser = fakeBrowser();
  let timestamp = 20_000;
  installPreloadErrorRecovery(browser, { now: () => timestamp });
  browser.dispatch({ preventDefault() {} });
  timestamp = 31_000;
  browser.dispatch({ preventDefault() {} });

  assert.equal(browser.reloadCount(), 1);
});

test('a distinct build may perform its own one-time recovery', () => {
  const browser = fakeBrowser();
  const stop = installPreloadErrorRecovery(
    browser,
    { now: () => 20_000, buildId: 'build-a' },
  );
  browser.dispatch({ preventDefault() {} });
  stop();
  installPreloadErrorRecovery(
    browser,
    { now: () => 21_000, buildId: 'build-b' },
  );
  browser.dispatch({ preventDefault() {} });

  assert.equal(browser.reloadCount(), 2);
});

test('disabled session storage cannot prevent one recovery reload', () => {
  const browser = fakeBrowser();
  browser.sessionStorage.getItem = () => {
    throw new Error('storage disabled');
  };
  browser.sessionStorage.setItem = () => {
    throw new Error('storage disabled');
  };
  installPreloadErrorRecovery(browser, { now: () => 20_000 });
  browser.dispatch({ preventDefault() {} });
  browser.dispatch({ preventDefault() {} });

  assert.equal(browser.reloadCount(), 1);
});

test('history state bounds reloads after a new page instance when storage is disabled', () => {
  const browser = fakeBrowser();
  browser.sessionStorage.getItem = () => {
    throw new Error('storage disabled');
  };
  browser.sessionStorage.setItem = () => {
    throw new Error('storage disabled');
  };
  const firstStop = installPreloadErrorRecovery(
    browser,
    { now: () => 20_000 },
  );
  browser.dispatch({ preventDefault() {} });
  firstStop();

  // Reinstalling models the fresh JavaScript module state after location
  // reload while the browsing context's History state is retained.
  installPreloadErrorRecovery(
    browser,
    { now: () => 20_500 },
  );
  browser.dispatch({ preventDefault() {} });

  assert.equal(browser.reloadCount(), 1);
});

test('an unavailable session storage property still permits recovery', () => {
  const browser = fakeBrowser();
  Object.defineProperty(browser, 'sessionStorage', {
    get() {
      throw new Error('storage unavailable');
    },
  });
  installPreloadErrorRecovery(browser, { now: () => 20_000 });
  browser.dispatch({ preventDefault() {} });
  browser.dispatch({ preventDefault() {} });

  assert.equal(browser.reloadCount(), 1);
});

test('a stale module-worker chunk uses the same bounded reload recovery', () => {
  const browser = fakeBrowser();
  installPreloadErrorRecovery(browser, { now: () => 42_000 });
  browser.dispatch(
    { preventDefault() {} },
    'dpms:moduleWorkerError',
  );

  assert.equal(browser.reloadCount(), 1);
  assert.equal(browser.storedValue(PRELOAD_RELOAD_AT_KEY), '42000');
});

test('module-worker recovery is bounded independently by build and platform', () => {
  const browser = fakeBrowser();
  installPreloadErrorRecovery(browser, { now: () => 42_000 });
  const eventFor = platformId => ({
    detail: {
      buildId: 'target-import-build-a',
      platformId,
    },
    preventDefault() {},
  });
  browser.dispatch(eventFor('weibo'), 'dpms:moduleWorkerError');
  browser.dispatch(eventFor('weibo'), 'dpms:moduleWorkerError');
  browser.dispatch(eventFor('douyin'), 'dpms:moduleWorkerError');

  assert.equal(browser.reloadCount(), 2);
});
