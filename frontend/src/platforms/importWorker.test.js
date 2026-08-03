import assert from 'node:assert/strict';
import test from 'node:test';

import { TARGET_IMPORT_FILE_MAX_BYTES } from './importShared.js';

test('worker rejects invalid UTF-8 and oversized bytes with finite errors', async () => {
  const originalPostMessage = globalThis.postMessage;
  const originalOnMessage = globalThis.onmessage;
  const messages = [];
  globalThis.postMessage = message => messages.push(message);
  try {
    await import(`./importWorker.js?protocol-test=${Date.now()}`);
    assert.deepEqual(messages.shift(), {
      type: 'dpms-target-import-ready',
    });

    await globalThis.onmessage({
      data: {
        requestId: 1,
        platform: 'bilibili',
        contentBuffer: Uint8Array.from([0xc3, 0x28]).buffer,
        options: {},
      },
    });
    assert.equal(messages[0].requestId, 1);
    assert.equal(messages[0].ok, false);
    assert.equal(messages[0].error.code, 'target_import_invalid_encoding');
    assert.doesNotMatch(JSON.stringify(messages[0]), /c3|credential|source/i);

    await globalThis.onmessage({
      data: {
        requestId: 2,
        platform: 'bilibili',
        contentBuffer: new ArrayBuffer(TARGET_IMPORT_FILE_MAX_BYTES + 1),
        options: {},
      },
    });
    assert.equal(messages[1].requestId, 2);
    assert.equal(messages[1].ok, false);
    assert.equal(messages[1].error.code, 'target_import_too_large');
    assert.equal(messages[1].error.maxBytes, TARGET_IMPORT_FILE_MAX_BYTES);
  } finally {
    if (originalPostMessage === undefined) delete globalThis.postMessage;
    else globalThis.postMessage = originalPostMessage;
    if (originalOnMessage === undefined) delete globalThis.onmessage;
    else globalThis.onmessage = originalOnMessage;
  }
});
