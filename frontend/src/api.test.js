import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiRequestError,
  SSE_MAX_EVENT_BUFFER_CHARS,
  apiPath,
  fetchJSON,
  isAuthenticationApiError,
  isRetryableApiError,
  parseServerSentEventBlock,
  readBoundedResponseBlob,
  subscribeAuthenticatedEventStream,
} from './api.js';

test('authenticated API paths reject absolute credential exfiltration targets', () => {
  assert.equal(apiPath('/metrics/overview'), '/api/metrics/overview');
  assert.throws(
    () => apiPath('https://attacker.example/collect'),
    /Absolute authenticated API paths are forbidden/,
  );
  assert.throws(
    () => apiPath('//attacker.example/collect'),
    /Absolute authenticated API paths are forbidden/,
  );
});

test('API failures are structured, retryable when transient, and hide gateway HTML', async () => {
  const previous = {
    fetch: globalThis.fetch,
    localStorage: globalThis.localStorage,
    window: globalThis.window,
  };
  try {
    globalThis.localStorage = { getItem: () => 'header-only-secret' };
    globalThis.window = {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
    };

    for (const status of [502, 503, 504]) {
      globalThis.fetch = async () => new Response(
        '<html><body><h1>nginx upstream failure</h1></body></html>',
        {
          status,
          headers: { 'content-type': 'text/html' },
        },
      );
      await assert.rejects(
        fetchJSON('/auth/me'),
        (error) => {
          assert.equal(error instanceof ApiRequestError, true);
          assert.equal(error.status, status);
          assert.equal(error.code, 'http_error');
          assert.equal(isRetryableApiError(error), true);
          assert.equal(isAuthenticationApiError(error), false);
          assert.doesNotMatch(error.message, /html|nginx|upstream/i);
          return true;
        },
      );
    }

    for (const status of [401, 403]) {
      globalThis.fetch = async () => new Response(
        JSON.stringify({ detail: 'Credential rejected' }),
        {
          status,
          headers: { 'content-type': 'application/json' },
        },
      );
      await assert.rejects(
        fetchJSON('/auth/me'),
        (error) => {
          assert.equal(error.status, status);
          assert.equal(isAuthenticationApiError(error), true);
          assert.equal(isRetryableApiError(error), false);
          return true;
        },
      );
    }

    globalThis.fetch = async () => new Response(
      JSON.stringify({
        detail: {
          code: 'real_run_prerequisites_not_ready',
          blocker_codes: ['worker_online', 'notification_ready'],
        },
      }),
      {
        status: 409,
        headers: { 'content-type': 'application/json' },
      },
    );
    await assert.rejects(
      fetchJSON('/metrics/runtime/settings/real-run'),
      (error) => {
        assert.equal(error.code, 'http_error');
        assert.equal(error.serverCode, 'real_run_prerequisites_not_ready');
        assert.equal(error.status, 409);
        assert.deepEqual(
          error.details?.blocker_codes,
          ['worker_online', 'notification_ready'],
        );
        assert.doesNotMatch(error.message, /blocker_codes/);
        return true;
      },
    );

    globalThis.fetch = async () => {
      throw new TypeError('connection refused');
    };
    await assert.rejects(
      fetchJSON('/auth/me'),
      (error) => {
        assert.equal(error.code, 'network_error');
        assert.equal(isRetryableApiError(error), true);
        assert.doesNotMatch(error.message, /connection refused/i);
        return true;
      },
    );

    globalThis.fetch = async (_url, options) => new Promise((resolve, reject) => {
      const abort = () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      };
      if (options.signal.aborted) abort();
      else options.signal.addEventListener('abort', abort, { once: true });
    });
    await assert.rejects(
      fetchJSON('/auth/me', { timeoutMs: 5 }),
      (error) => {
        assert.equal(error.code, 'timeout');
        assert.equal(error.path, '/auth/me');
        assert.equal(isRetryableApiError(error), true);
        return true;
      },
    );
  } finally {
    globalThis.fetch = previous.fetch;
    if (previous.localStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous.localStorage;
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
  }
});

test('SSE parser joins data fields and ignores comments or metadata', () => {
  assert.equal(
    parseServerSentEventBlock(
      ': heartbeat\nid: 7\nevent: log\ndata: first\ndata:second',
    ),
    'first\nsecond',
  );
  assert.equal(parseServerSentEventBlock(': heartbeat'), null);
});

test('authenticated blobs reject declared and streamed size overflow', async () => {
  let declaredOverflowCancelled = false;
  const declaredOverflow = new Response(
    new ReadableStream({
      cancel() {
        declaredOverflowCancelled = true;
      },
    }),
    { headers: { 'content-length': '5' } },
  );
  await assert.rejects(
    readBoundedResponseBlob(declaredOverflow, { maxBytes: 4 }),
    /exceeds the safety limit/,
  );
  assert.equal(declaredOverflowCancelled, true);

  let streamedOverflowCancelled = false;
  const streamedOverflow = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2, 3]));
        controller.enqueue(new Uint8Array([4, 5]));
      },
      cancel() {
        streamedOverflowCancelled = true;
      },
    }),
  );
  await assert.rejects(
    readBoundedResponseBlob(streamedOverflow, { maxBytes: 4 }),
    /exceeds the safety limit/,
  );
  assert.equal(streamedOverflowCancelled, true);
});

test('authenticated SSE uses a header and never places the token in the URL', async () => {
  const previous = {
    fetch: globalThis.fetch,
    localStorage: globalThis.localStorage,
    window: globalThis.window,
  };
  let request;
  let subscription;
  try {
    globalThis.localStorage = {
      getItem(key) {
        return key === 'dpms_admin_token' ? 'header-only-secret' : '';
      },
    };
    globalThis.window = {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
    };
    globalThis.fetch = async (url, options) => {
      request = { url, options };
      const encoder = new TextEncoder();
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode('data: {"event":"ready"}\n\n'),
            );
          },
        }),
        {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        },
      );
    };

    await new Promise((resolve, reject) => {
      subscription = subscribeAuthenticatedEventStream('/metrics/stream', {
        onMessage(event) {
          try {
            assert.deepEqual(JSON.parse(event.data), { event: 'ready' });
            subscription.close();
            resolve();
          } catch (error) {
            reject(error);
          }
        },
        onError: reject,
      });
    });

    assert.equal(request.url, '/api/metrics/stream');
    assert.equal(
      request.options.headers['x-admin-token'],
      'header-only-secret',
    );
    assert.doesNotMatch(request.url, /admin_token|header-only-secret/);
  } finally {
    subscription?.close();
    globalThis.fetch = previous.fetch;
    if (previous.localStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous.localStorage;
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
  }
});

test('authenticated SSE rejects a non-event-stream response before opening', async () => {
  const previous = {
    fetch: globalThis.fetch,
    localStorage: globalThis.localStorage,
    window: globalThis.window,
  };
  let opened = false;
  let subscription;
  try {
    globalThis.localStorage = { getItem: () => 'header-only-secret' };
    globalThis.window = {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
    };
    globalThis.fetch = async () => new Response('not an event stream', {
      status: 200,
      headers: { 'content-type': 'text/plain' },
    });
    await new Promise((resolve, reject) => {
      subscription = subscribeAuthenticatedEventStream('/metrics/stream', {
        onOpen() {
          opened = true;
        },
        onError(error) {
          try {
            assert.match(error.message, /Content-Type is invalid/);
            subscription.close();
            resolve();
          } catch (assertionError) {
            reject(assertionError);
          }
        },
      });
    });
    assert.equal(opened, false);
  } finally {
    subscription?.close();
    globalThis.fetch = previous.fetch;
    if (previous.localStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous.localStorage;
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
  }
});

test('authenticated SSE bounds an unterminated event buffer', async () => {
  const previous = {
    fetch: globalThis.fetch,
    localStorage: globalThis.localStorage,
    window: globalThis.window,
  };
  let subscription;
  try {
    globalThis.localStorage = { getItem: () => 'header-only-secret' };
    globalThis.window = {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
    };
    const oversizedEvent = new TextEncoder().encode(
      `data: ${'x'.repeat(SSE_MAX_EVENT_BUFFER_CHARS)}`,
    );
    globalThis.fetch = async () => new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(oversizedEvent);
        },
      }),
      {
        status: 200,
        headers: { 'content-type': 'text/event-stream; charset=utf-8' },
      },
    );
    await new Promise((resolve, reject) => {
      subscription = subscribeAuthenticatedEventStream('/metrics/stream', {
        onError(error) {
          try {
            assert.match(error.message, /event exceeds the safety limit/);
            subscription.close();
            resolve();
          } catch (assertionError) {
            reject(assertionError);
          }
        },
      });
    });
  } finally {
    subscription?.close();
    globalThis.fetch = previous.fetch;
    if (previous.localStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous.localStorage;
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
  }
});
