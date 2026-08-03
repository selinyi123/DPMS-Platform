import assert from 'node:assert/strict';
import test from 'node:test';

import {
  reloadApplicationModuleGraph,
  settleRequestSlicesIndependently,
} from './asyncSlices.js';

test('a slow request slice cannot delay a ready sibling update', async () => {
  let releaseSlow;
  const slow = new Promise(resolve => {
    releaseSlow = resolve;
  });
  const applied = [];
  const handles = settleRequestSlicesIndependently([
    { key: 'ready', request: Promise.resolve('value'), onFulfilled: value => applied.push(value) },
    { key: 'slow', request: slow, onFulfilled: value => applied.push(value) },
  ]);

  const ready = await handles[0];
  assert.equal(ready.status, 'fulfilled');
  assert.deepEqual(applied, ['value']);

  releaseSlow('later');
  await handles[1];
  assert.deepEqual(applied, ['value', 'later']);
});

test('a rejected slice is reported without rejecting sibling handles', async () => {
  const handles = settleRequestSlicesIndependently([
    { key: 'failed', request: Promise.reject(new Error('offline')) },
    { key: 'ready', request: Promise.resolve(42) },
  ]);
  const results = await Promise.all(handles);
  assert.deepEqual(results.map(result => result.status), ['rejected', 'fulfilled']);
  assert.equal(results[0].error.message, 'offline');
  assert.equal(results[1].value, 42);
});

test('failed static imports recover by replacing the browser module graph', () => {
  let reloads = 0;
  reloadApplicationModuleGraph({ reload: () => { reloads += 1; } });
  assert.equal(reloads, 1);
  assert.throws(
    () => reloadApplicationModuleGraph({}),
    /module_graph_reload_unavailable/,
  );
});
