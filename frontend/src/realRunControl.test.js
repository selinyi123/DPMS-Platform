import assert from 'node:assert/strict';
import test from 'node:test';

import { realRunControlState } from './realRunControl.js';

const readySnapshot = {
  production_checks: [
    { code: 'worker_online', priority: 'P0', passed: true },
    { code: 'real_run_global_switch', priority: 'P0', passed: false },
    { code: 'global_circuit_breaker_closed', priority: 'P0', passed: false },
    { code: 'autopilot_real_run_authorized', priority: 'P0', passed: false },
  ],
};

test('a persisted runtime switch remains disableable when deployment capability is lost', () => {
  const state = realRunControlState({
    deployment_real_run_enabled: false,
    runtime_real_run_enabled: true,
    real_run_enabled: false,
  }, readySnapshot);

  assert.equal(state.currentlyEnabled, true);
  assert.equal(state.canEnable, false);
  assert.equal(state.canArm, true);
});

test('effective state remains a backward-compatible fallback for older servers', () => {
  const state = realRunControlState({
    deployment_real_run_enabled: true,
    real_run_enabled: true,
  }, readySnapshot);

  assert.equal(state.currentlyEnabled, true);
  assert.equal(state.canArm, true);
});
