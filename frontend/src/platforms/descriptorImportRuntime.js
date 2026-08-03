import { PLATFORM_IDS } from './catalog.js';

function registeredImportOptions(options) {
  const source = options && typeof options === 'object' ? options : {};
  return {
    ...source,
    allowedPlatformIds: [...new Set([
      ...PLATFORM_IDS,
      ...(Array.isArray(source.allowedPlatformIds)
        ? source.allowedPlatformIds
        : []),
    ])],
  };
}

// Platform descriptors are loaded by ./index.js, which itself owns the
// central import facade. Resolve the runtime only when a descriptor method is
// invoked so the static module graph remains acyclic while every entry point
// still receives identical mixed-DPMS CSV semantics.
export async function normalizeDescriptorTargetImport(
  platform,
  content,
  options = {},
) {
  const { normalizeTargetImportForPlatform } = await import(
    './importRuntime.js'
  );
  return normalizeTargetImportForPlatform(
    platform,
    content,
    registeredImportOptions(options),
  );
}
