const REAL_RUN_ENABLE_EXCLUDED_P0 = new Set([
  'real_run_global_switch',
  'global_circuit_breaker_closed',
  'autopilot_real_run_authorized',
]);

export function realRunControlState(runtimeSettings, productionReadiness) {
  const runtimeSwitchReported = (
    typeof runtimeSettings?.runtime_real_run_enabled === 'boolean'
  );
  const currentlyEnabled = runtimeSwitchReported
    ? runtimeSettings.runtime_real_run_enabled
    : runtimeSettings?.real_run_enabled === true;
  const deploymentCapabilityReported = (
    typeof runtimeSettings?.deployment_real_run_enabled === 'boolean'
  );
  const deploymentCapability = runtimeSettings?.deployment_real_run_enabled === true;
  const checks = Array.isArray(productionReadiness?.production_checks)
    ? productionReadiness.production_checks
    : [];
  const technicalP0Checks = checks.filter(check => (
    check?.priority === 'P0'
    && !REAL_RUN_ENABLE_EXCLUDED_P0.has(check?.code)
  ));
  const readinessAvailable = technicalP0Checks.length > 0;
  const blockers = technicalP0Checks.filter(check => check?.passed !== true);
  const canEnable = Boolean(
    deploymentCapability
    && readinessAvailable
    && blockers.length === 0
  );
  return {
    currentlyEnabled,
    deploymentCapability,
    deploymentCapabilityReported,
    readinessAvailable,
    blockers,
    canEnable,
    // A persisted runtime switch can always be turned off, even when the
    // deployment capability or readiness observation has disappeared.
    canArm: currentlyEnabled || canEnable,
  };
}
