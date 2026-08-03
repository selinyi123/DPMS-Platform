import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TARGET_IMPORT_WORKER_THRESHOLD_BYTES,
  TARGET_IMPORT_WORKER_THRESHOLD_CHARS,
  normalizeTargetImportInWorker,
  readTargetImportFile,
  targetImportNeedsWorker,
  terminateTargetImportWorker,
} from './importWorkerClient.js';
import { TARGET_IMPORT_FILE_MAX_BYTES } from './importShared.js';
import {
  normalizeTargetImportForPlatform,
} from './importRuntime.js';

test('large target imports cross the off-main-thread threshold', () => {
  assert.equal(
    targetImportNeedsWorker('x'.repeat(TARGET_IMPORT_WORKER_THRESHOLD_CHARS - 1)),
    false,
  );
  assert.equal(
    targetImportNeedsWorker('x'.repeat(TARGET_IMPORT_WORKER_THRESHOLD_CHARS)),
    true,
  );
});

test('the 10 MB browser ceiling can never be parsed below the worker threshold', () => {
  // UTF-8 uses at most four bytes per Unicode scalar. Even an all-astral
  // 10 MB source has far more UTF-16 code units than this threshold.
  const minimumUtf16UnitsAtTenMegabytes = 10_000_000 / 2;
  assert.ok(minimumUtf16UnitsAtTenMegabytes > TARGET_IMPORT_WORKER_THRESHOLD_CHARS);
});

test('small files keep the text path while large files keep bytes for transfer', async () => {
  let smallTextReads = 0;
  let smallBufferReads = 0;
  const small = await readTargetImportFile({
    size: 12,
    async text() {
      smallTextReads += 1;
      return 'small import';
    },
    async arrayBuffer() {
      smallBufferReads += 1;
      throw new Error('large path must not run');
    },
  });
  assert.equal(small, 'small import');
  assert.equal(smallTextReads, 1);
  assert.equal(smallBufferReads, 0);

  const sourceBytes = new TextEncoder().encode('large import');
  let largeTextReads = 0;
  let largeBufferReads = 0;
  const large = await readTargetImportFile({
    size: TARGET_IMPORT_WORKER_THRESHOLD_BYTES,
    async text() {
      largeTextReads += 1;
      throw new Error('main-thread text path must not run');
    },
    async arrayBuffer() {
      largeBufferReads += 1;
      return sourceBytes.buffer;
    },
  });
  assert.ok(large instanceof ArrayBuffer);
  assert.equal(largeTextReads, 0);
  assert.equal(largeBufferReads, 1);
});

test('file byte caps and read failures stay strict and sanitized', async () => {
  let readAttempted = false;
  await assert.rejects(
    readTargetImportFile({
      size: TARGET_IMPORT_FILE_MAX_BYTES + 1,
      async text() {
        readAttempted = true;
        return '';
      },
      async arrayBuffer() {
        readAttempted = true;
        return new ArrayBuffer(0);
      },
    }),
    error => error?.code === 'target_import_too_large'
      && error.maxBytes === TARGET_IMPORT_FILE_MAX_BYTES,
  );
  assert.equal(readAttempted, false);

  await assert.rejects(
    readTargetImportFile({
      size: 10,
      async text() {
        throw new Error('C:\\private\\credential-shaped-file.csv');
      },
    }),
    error => (
      error?.code === 'target_import_file_read_failed'
      && !String(error?.message).includes('credential-shaped')
    ),
  );
});

test('ArrayBuffer imports transfer a copy without detaching caller state', async () => {
  const originalWorker = globalThis.Worker;
  const source = new TextEncoder().encode(
    'bilibili,https://t.bilibili.com/123',
  ).buffer;
  const sourceBytes = [...new Uint8Array(source)];
  let postedMessage;
  let transferList;
  class TransferInspectingWorker {
    postMessage(message, transfers) {
      postedMessage = message;
      transferList = transfers;
      queueMicrotask(() => {
        this.onmessage({
          data: {
            requestId: message.requestId,
            ok: true,
            result: { content: 'normalized', targetCount: 1 },
          },
        });
      });
    }

    terminate() {}
  }
  globalThis.Worker = TransferInspectingWorker;
  terminateTargetImportWorker();
  try {
    assert.deepEqual(
      await normalizeTargetImportInWorker('bilibili', source),
      { content: 'normalized', targetCount: 1 },
    );
    assert.equal(postedMessage.content, undefined);
    assert.notEqual(postedMessage.contentBuffer, source);
    assert.equal(transferList.length, 1);
    assert.equal(postedMessage.contentBuffer, transferList[0]);
    assert.notEqual(transferList[0], source);
    assert.equal(source.byteLength, sourceBytes.length);
    assert.deepEqual(
      [...new Uint8Array(transferList[0])],
      sourceBytes,
    );
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
  }
});

test('oversized binary imports are rejected before cloning caller state', async () => {
  const source = new ArrayBuffer(TARGET_IMPORT_FILE_MAX_BYTES + 1);
  let cloneAttempted = false;
  source.slice = () => {
    cloneAttempted = true;
    throw new Error('oversized buffer must not be cloned');
  };

  await assert.rejects(
    normalizeTargetImportInWorker('bilibili', source),
    error => error?.code === 'target_import_too_large'
      && error.maxBytes === TARGET_IMPORT_FILE_MAX_BYTES,
  );
  assert.equal(cloneAttempted, false);
});

test('large imports never fall back to parsing when workers are unavailable', async () => {
  const originalWorker = globalThis.Worker;
  delete globalThis.Worker;
  try {
    await assert.rejects(
      normalizeTargetImportForPlatform(
        'xiaohongshu',
        'x'.repeat(TARGET_IMPORT_WORKER_THRESHOLD_CHARS),
      ),
      error => error?.code === 'target_import_worker_unavailable',
    );
  } finally {
    if (originalWorker !== undefined) globalThis.Worker = originalWorker;
  }
});

test('a CSP or module-worker construction failure stays local and typed', async () => {
  const originalWorker = globalThis.Worker;
  const originalDispatchEvent = globalThis.dispatchEvent;
  const originalEvent = globalThis.Event;
  const moduleErrors = [];
  class BlockedWorker {
    constructor() {
      throw new Error('worker blocked by policy');
    }
  }
  globalThis.Worker = BlockedWorker;
  globalThis.Event = class {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.dispatchEvent = event => moduleErrors.push(event.type);
  terminateTargetImportWorker();
  try {
    await assert.rejects(
      normalizeTargetImportInWorker('xiaohongshu', 'large source'),
      error => error?.code === 'target_import_worker_unavailable',
    );
    assert.deepEqual(moduleErrors, []);
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
    if (originalDispatchEvent === undefined) delete globalThis.dispatchEvent;
    else globalThis.dispatchEvent = originalDispatchEvent;
    if (originalEvent === undefined) delete globalThis.Event;
    else globalThis.Event = originalEvent;
  }
});

test('a pre-ready module load error requests preload recovery once', async () => {
  const originalWorker = globalThis.Worker;
  const originalDispatchEvent = globalThis.dispatchEvent;
  const originalEvent = globalThis.Event;
  const moduleErrors = [];
  class BrokenModuleWorker {
    postMessage() {
      queueMicrotask(() => this.onerror(new Error('chunk missing')));
    }

    terminate() {}
  }
  globalThis.Worker = BrokenModuleWorker;
  globalThis.Event = class {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.dispatchEvent = event => moduleErrors.push(event.type);
  terminateTargetImportWorker();
  try {
    await assert.rejects(
      normalizeTargetImportInWorker('bilibili', 'large source'),
      error => error?.code === 'target_import_worker_failed',
    );
    assert.deepEqual(moduleErrors, ['dpms:moduleWorkerError']);
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
    if (originalDispatchEvent === undefined) delete globalThis.dispatchEvent;
    else globalThis.dispatchEvent = originalDispatchEvent;
    if (originalEvent === undefined) delete globalThis.Event;
    else globalThis.Event = originalEvent;
  }
});

test('a post-ready runtime crash never requests a page reload', async () => {
  const originalWorker = globalThis.Worker;
  const originalDispatchEvent = globalThis.dispatchEvent;
  const originalEvent = globalThis.Event;
  const moduleErrors = [];
  class RuntimeCrashWorker {
    constructor() {
      queueMicrotask(() => {
        this.onmessage({
          data: { type: 'dpms-target-import-ready' },
        });
      });
    }

    postMessage() {
      queueMicrotask(() => this.onerror(new Error('runtime crash')));
    }

    terminate() {}
  }
  globalThis.Worker = RuntimeCrashWorker;
  globalThis.Event = class {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.dispatchEvent = event => moduleErrors.push(event.type);
  terminateTargetImportWorker();
  try {
    await assert.rejects(
      normalizeTargetImportInWorker('bilibili', 'large source'),
      error => error?.code === 'target_import_worker_failed',
    );
    assert.deepEqual(moduleErrors, []);
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
    if (originalDispatchEvent === undefined) delete globalThis.dispatchEvent;
    else globalThis.dispatchEvent = originalDispatchEvent;
    if (originalEvent === undefined) delete globalThis.Event;
    else globalThis.Event = originalEvent;
  }
});

test('a post-ready policy chunk failure requests one bounded preload recovery', async () => {
  const originalWorker = globalThis.Worker;
  const originalDispatchEvent = globalThis.dispatchEvent;
  const originalEvent = globalThis.Event;
  const moduleErrors = [];
  class MissingPolicyChunkWorker {
    constructor() {
      queueMicrotask(() => {
        this.onmessage({
          data: { type: 'dpms-target-import-ready' },
        });
      });
    }

    postMessage(message) {
      queueMicrotask(() => {
        const response = {
          data: {
            requestId: message.requestId,
            ok: false,
            moduleGraphError: true,
            error: {
              code: 'target_import_worker_failed',
            },
          },
        };
        this.onmessage(response);
        // A terminated old worker may still have an already-queued event.
        // It must not request another page recovery.
        this.onmessage(response);
      });
    }

    terminate() {}
  }
  globalThis.Worker = MissingPolicyChunkWorker;
  globalThis.Event = class {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.dispatchEvent = event => moduleErrors.push({
    type: event.type,
    detail: event.detail,
  });
  terminateTargetImportWorker();
  try {
    await assert.rejects(
      normalizeTargetImportInWorker('weibo', 'large source'),
      error => error?.code === 'target_import_worker_failed',
    );
    assert.equal(moduleErrors.length, 1);
    assert.equal(moduleErrors[0].type, 'dpms:moduleWorkerError');
    assert.equal(moduleErrors[0].detail.platformId, 'weibo');
    assert.match(moduleErrors[0].detail.buildId, /^target-import-[0-9a-f]{8}$/);
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
    if (originalDispatchEvent === undefined) delete globalThis.dispatchEvent;
    else globalThis.dispatchEvent = originalDispatchEvent;
    if (originalEvent === undefined) delete globalThis.Event;
    else globalThis.Event = originalEvent;
  }
});

test('a policy evaluation failure rejects only its platform request', async () => {
  const originalWorker = globalThis.Worker;
  const originalDispatchEvent = globalThis.dispatchEvent;
  const originalEvent = globalThis.Event;
  const moduleErrors = [];
  class PolicyEvaluationWorker {
    constructor() {
      queueMicrotask(() => {
        this.onmessage({
          data: { type: 'dpms-target-import-ready' },
        });
      });
    }

    postMessage(message) {
      queueMicrotask(() => {
        if (message.platform === 'weibo') {
          this.onmessage({
            data: {
              requestId: message.requestId,
              ok: false,
              moduleGraphError: false,
              error: { code: 'target_import_worker_failed' },
            },
          });
          return;
        }
        this.onmessage({
          data: {
            requestId: message.requestId,
            ok: true,
            result: { content: 'peer-normalized', targetCount: 1 },
          },
        });
      });
    }

    terminate() {}
  }
  globalThis.Worker = PolicyEvaluationWorker;
  globalThis.Event = class {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.dispatchEvent = event => moduleErrors.push(event.type);
  terminateTargetImportWorker();
  try {
    await assert.rejects(
      normalizeTargetImportInWorker('weibo', 'evaluation failure'),
      error => error?.code === 'target_import_worker_failed',
    );
    assert.deepEqual(
      await normalizeTargetImportInWorker('douyin', 'peer remains healthy'),
      { content: 'peer-normalized', targetCount: 1 },
    );
    assert.deepEqual(moduleErrors, []);
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
    if (originalDispatchEvent === undefined) delete globalThis.dispatchEvent;
    else globalThis.dispatchEvent = originalDispatchEvent;
    if (originalEvent === undefined) delete globalThis.Event;
    else globalThis.Event = originalEvent;
  }
});

test('a platform timeout restarts only that platform worker lane', async () => {
  const originalWorker = globalThis.Worker;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const timers = [];
  const instances = [];
  class PlatformLaneWorker {
    constructor() {
      this.platform = null;
      this.terminated = false;
      instances.push(this);
    }

    postMessage(message) {
      this.platform = message.platform;
      if (message.platform !== 'douyin') return;
      queueMicrotask(() => {
        this.onmessage({
          data: {
            requestId: message.requestId,
            ok: true,
            result: { content: 'douyin-ok', targetCount: 1 },
          },
        });
      });
    }

    terminate() {
      this.terminated = true;
    }
  }
  globalThis.Worker = PlatformLaneWorker;
  globalThis.setTimeout = callback => {
    timers.push(callback);
    return timers.length;
  };
  globalThis.clearTimeout = () => {};
  terminateTargetImportWorker();
  try {
    const stalled = normalizeTargetImportInWorker('weibo', 'stalled');
    const peer = normalizeTargetImportInWorker('douyin', 'healthy');
    assert.equal(timers.length, 2);
    timers[0]();

    await assert.rejects(
      stalled,
      error => error?.code === 'target_import_worker_timeout',
    );
    assert.deepEqual(
      await peer,
      { content: 'douyin-ok', targetCount: 1 },
    );
    assert.equal(instances.length, 2);
    assert.equal(
      instances.find(instance => instance.platform === 'weibo')?.terminated,
      true,
    );
    assert.equal(
      instances.find(instance => instance.platform === 'douyin')?.terminated,
      false,
    );
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});

test('worker requests preserve results and typed import errors', async () => {
  const originalWorker = globalThis.Worker;
  class FakeWorker {
    postMessage(message) {
      queueMicrotask(() => {
        if (message.content === 'reject') {
          this.onmessage({
            data: {
              requestId: message.requestId,
              ok: false,
              error: {
                name: 'TargetImportError',
                code: 'douyin_import_invalid_json',
                platformId: 'douyin',
              },
            },
          });
          return;
        }
        this.onmessage({
          data: {
            requestId: message.requestId,
            ok: true,
            result: { content: 'normalized', targetCount: 1 },
          },
        });
      });
    }

    terminate() {}
  }

  globalThis.Worker = FakeWorker;
  try {
    assert.deepEqual(
      await normalizeTargetImportInWorker('bilibili', 'accept'),
      { content: 'normalized', targetCount: 1 },
    );
    await assert.rejects(
      normalizeTargetImportInWorker('douyin', 'reject'),
      error => error?.code === 'douyin_import_invalid_json'
        && error.platformId === 'douyin',
    );
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
  }
});

test('worker failures never expose an untyped parser message', async () => {
  const originalWorker = globalThis.Worker;
  class FakeWorker {
    postMessage(message) {
      queueMicrotask(() => {
        this.onmessage({
          data: {
            requestId: message.requestId,
            ok: false,
            error: {
              name: 'SyntaxError',
              message: `unexpected token near ${message.content}`,
            },
          },
        });
      });
    }

    terminate() {}
  }

  globalThis.Worker = FakeWorker;
  try {
    await assert.rejects(
      normalizeTargetImportInWorker(
        'douyin',
        'credential-shaped-private-source',
      ),
      error => (
        error?.code === 'target_import_worker_failed'
        && !String(error?.message).includes('credential-shaped')
      ),
    );
  } finally {
    terminateTargetImportWorker();
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
  }
});
