import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createLatestAbortableRequestGate,
  createLatestRequestGate,
} from './requestGeneration.js';

test('only the latest request generation may update state', () => {
  const gate = createLatestRequestGate();
  const first = gate.begin();
  const second = gate.begin();

  assert.equal(gate.isCurrent(first), false);
  assert.equal(gate.isCurrent(second), true);
});

test('closing a surface invalidates its in-flight request', () => {
  const gate = createLatestRequestGate();
  const request = gate.begin();

  gate.invalidate();

  assert.equal(gate.isCurrent(request), false);
});

test('abortable latest gate cancels superseded and closed requests', () => {
  const gate = createLatestAbortableRequestGate();
  const first = gate.begin();
  const second = gate.begin();

  assert.equal(first.signal.aborted, true);
  assert.equal(gate.isCurrent(first), false);
  assert.equal(second.signal.aborted, false);
  assert.equal(gate.isCurrent(second), true);

  gate.invalidate();

  assert.equal(second.signal.aborted, true);
  assert.equal(gate.isCurrent(second), false);
});
