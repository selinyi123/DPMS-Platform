import {
  TARGET_IMPORT_FILE_MAX_BYTES,
  TargetImportError,
} from './importShared.js';
import {
  isRegisteredPlatformId,
  normalizePlatformId,
} from './catalog.js';

// Parsing a provider export can walk every JSON node or CSV character. Keep
// that work off the UI thread once a source is large enough to cause visible
// input latency on low-end clients.
export const TARGET_IMPORT_WORKER_THRESHOLD_CHARS = 256_000;
export const TARGET_IMPORT_WORKER_THRESHOLD_BYTES = 256_000;
export const TARGET_IMPORT_WORKER_TIMEOUT_MS = 30_000;

let nextRequestId = 1;
const importWorkerLanes = new Map();

function stableModuleBuildId(value) {
  const source = String(value || '');
  let hash = 0x811c9dc5;
  for (let index = 0; index < source.length; index += 1) {
    hash ^= source.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `target-import-${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

export const TARGET_IMPORT_MODULE_BUILD_ID = stableModuleBuildId(import.meta.url);

function signalModuleWorkerError(platformId) {
  try {
    if (
      typeof globalThis.dispatchEvent === 'function'
      && typeof globalThis.Event === 'function'
    ) {
      const detail = Object.freeze({
        buildId: TARGET_IMPORT_MODULE_BUILD_ID,
        platformId: /^[a-z][a-z0-9_]{1,31}$/.test(String(platformId || ''))
          ? String(platformId)
          : 'unknown',
      });
      let event;
      if (typeof globalThis.CustomEvent === 'function') {
        event = new CustomEvent('dpms:moduleWorkerError', { detail });
      } else {
        event = new Event('dpms:moduleWorkerError');
        Object.defineProperty(event, 'detail', { value: detail });
      }
      globalThis.dispatchEvent(event);
    }
  } catch {
    // Reload recovery is best-effort; the typed local import failure below
    // remains authoritative when events are unavailable.
  }
}

function supportsModuleWorker() {
  return typeof globalThis.Worker === 'function';
}

function binaryImportContent(content) {
  // postMessage transfer detaches its ArrayBuffer. Clone caller-owned buffers
  // so retries and programmatic callers never observe a destructive side
  // effect. Typed-array views were already copied into an exact-size buffer.
  if (content instanceof ArrayBuffer) return content.slice(0);
  if (!ArrayBuffer.isView(content)) return null;
  return content.buffer.slice(
    content.byteOffset,
    content.byteOffset + content.byteLength,
  );
}

export function targetImportNeedsWorker(content) {
  return content instanceof ArrayBuffer
    || ArrayBuffer.isView(content)
    || String(content || '').length >= TARGET_IMPORT_WORKER_THRESHOLD_CHARS;
}

export function targetImportWorkerSupported() {
  return supportsModuleWorker();
}

function assertSharedImportByteLimit(byteLength) {
  if (
    !Number.isSafeInteger(byteLength)
    || byteLength < 0
    || byteLength > TARGET_IMPORT_FILE_MAX_BYTES
  ) {
    throw new TargetImportError('target_import_too_large', {
      byteLength: Number.isSafeInteger(byteLength) ? byteLength : 0,
      maxBytes: TARGET_IMPORT_FILE_MAX_BYTES,
    });
  }
}

export async function readTargetImportFile(file) {
  const declaredSize = Number(file?.size);
  assertSharedImportByteLimit(declaredSize);
  if (declaredSize >= TARGET_IMPORT_WORKER_THRESHOLD_BYTES) {
    let content;
    try {
      content = await file.arrayBuffer();
    } catch {
      throw new TargetImportError('target_import_file_read_failed');
    }
    const buffer = content instanceof ArrayBuffer
      ? content
      : binaryImportContent(content);
    if (!buffer) throw new TargetImportError('target_import_file_read_failed');
    assertSharedImportByteLimit(buffer.byteLength);
    return buffer;
  }

  let content;
  try {
    content = await file.text();
  } catch {
    throw new TargetImportError('target_import_file_read_failed');
  }
  const text = String(content || '');
  assertSharedImportByteLimit(new TextEncoder().encode(text).byteLength);
  return text;
}

function errorFromWorker(payload = {}) {
  const candidateCode = String(payload.code || '');
  const code = /^[a-z][a-z0-9_]{2,127}$/.test(candidateCode)
    ? candidateCode
    : 'target_import_worker_failed';
  return new TargetImportError(code, {
    platformId: payload.platformId,
    platformIds: payload.platformIds,
    byteLength: payload.byteLength,
    maxBytes: payload.maxBytes,
  });
}

function rejectEveryPending(lane, error) {
  for (const { reject, timeoutId } of lane.pendingRequests.values()) {
    clearTimeout(timeoutId);
    reject(error);
  }
  lane.pendingRequests.clear();
}

function discardPlatformWorker(platformId, error = null) {
  const lane = importWorkerLanes.get(platformId);
  if (!lane) return;
  importWorkerLanes.delete(platformId);
  lane.worker.terminate();
  if (error) rejectEveryPending(lane, error);
}

function createImportWorker(platformId) {
  const worker = new Worker(
    new URL('./importWorker.js', import.meta.url),
    // Vite must statically analyze module-worker options to emit a separate
    // worker chunk. Platform isolation is owned by importWorkerLanes, not by
    // the diagnostic Worker.name, so keep this object entirely static.
    { name: 'dpms-target-import', type: 'module' },
  );
  const lane = {
    worker,
    pendingRequests: new Map(),
    moduleReady: false,
    moduleRecoverySignaled: false,
  };
  const isCurrentWorker = () => importWorkerLanes.get(platformId) === lane;
  const requestModuleRecovery = (failedPlatformId) => {
    if (!isCurrentWorker() || lane.moduleRecoverySignaled) return;
    lane.moduleRecoverySignaled = true;
    signalModuleWorkerError(failedPlatformId || platformId);
  };
  worker.onmessage = ({ data }) => {
    if (!isCurrentWorker()) return;
    if (data?.type === 'dpms-target-import-ready') {
      lane.moduleReady = true;
      return;
    }
    const requestId = Number(data?.requestId);
    const pending = lane.pendingRequests.get(requestId);
    if (!pending) return;
    if (data?.moduleGraphError === true) {
      requestModuleRecovery(data?.moduleGraphPlatformId || pending.platformId);
      discardPlatformWorker(platformId, errorFromWorker(data?.error));
      return;
    }
    lane.pendingRequests.delete(requestId);
    clearTimeout(pending.timeoutId);
    if (data?.ok === true) pending.resolve(data.result);
    else pending.reject(errorFromWorker(data?.error));
  };
  worker.onerror = () => {
    if (!isCurrentWorker()) return;
    if (!lane.moduleReady) {
      const pendingPlatform = [...lane.pendingRequests.values()]
        .map(request => request.platformId)
        .find(Boolean);
      requestModuleRecovery(pendingPlatform);
    }
    discardPlatformWorker(
      platformId,
      new TargetImportError('target_import_worker_failed'),
    );
  };
  worker.onmessageerror = () => {
    if (!isCurrentWorker()) return;
    discardPlatformWorker(
      platformId,
      new TargetImportError('target_import_worker_failed'),
    );
  };
  return lane;
}

function currentWorkerLane(platformId) {
  if (!supportsModuleWorker()) return null;
  let lane = importWorkerLanes.get(platformId);
  if (!lane) {
    lane = createImportWorker(platformId);
    importWorkerLanes.set(platformId, lane);
  }
  return lane;
}

function cloneableOptions(options) {
  return {
    allowedPlatformIds: Array.isArray(options?.allowedPlatformIds)
      ? options.allowedPlatformIds.map(value => String(value))
      : [],
  };
}

export function normalizeTargetImportInWorker(platform, content, options = {}) {
  const platformId = normalizePlatformId(platform);
  if (!isRegisteredPlatformId(platformId)) {
    return Promise.reject(
      new TargetImportError('target_import_platform_unsupported'),
    );
  }
  const hasBinaryContent = content instanceof ArrayBuffer
    || ArrayBuffer.isView(content);
  if (hasBinaryContent) {
    try {
      // Reject caller-supplied binary content before cloning it for transfer.
      // Otherwise an oversized programmatic import can synchronously double
      // its memory footprint on the main thread before the bound is applied.
      assertSharedImportByteLimit(content.byteLength);
    } catch (error) {
      return Promise.reject(error);
    }
  }
  let contentBuffer;
  try {
    contentBuffer = binaryImportContent(content);
  } catch {
    return Promise.reject(
      new TargetImportError('target_import_worker_failed'),
    );
  }
  let lane;
  try {
    lane = currentWorkerLane(platformId);
  } catch {
    discardPlatformWorker(platformId);
    return Promise.reject(
      new TargetImportError('target_import_worker_unavailable'),
    );
  }
  if (!lane) {
    return Promise.reject(
      new TargetImportError('target_import_worker_unavailable'),
    );
  }
  const { worker, pendingRequests } = lane;
  const requestId = nextRequestId;
  nextRequestId += 1;
  return new Promise((resolve, reject) => {
    const timeoutId = setTimeout(() => {
      if (!pendingRequests.delete(requestId)) return;
      reject(new TargetImportError('target_import_worker_timeout'));
      // A timed-out parser can still be consuming CPU. Terminate it instead
      // of letting a later import on the same platform share an unhealthy
      // worker. Other platform lanes remain independent.
      discardPlatformWorker(
        platformId,
        new TargetImportError('target_import_worker_restarted'),
      );
    }, TARGET_IMPORT_WORKER_TIMEOUT_MS);
    pendingRequests.set(requestId, {
      resolve,
      reject,
      timeoutId,
      platformId,
    });
    try {
      const message = {
        requestId,
        platform: platformId,
        options: cloneableOptions(options),
      };
      if (contentBuffer) message.contentBuffer = contentBuffer;
      else message.content = String(content || '');
      worker.postMessage(
        message,
        contentBuffer ? [contentBuffer] : [],
      );
    } catch {
      pendingRequests.delete(requestId);
      clearTimeout(timeoutId);
      discardPlatformWorker(
        platformId,
        new TargetImportError('target_import_worker_failed'),
      );
      reject(new TargetImportError('target_import_worker_failed'));
    }
  });
}

export function terminateTargetImportWorker() {
  for (const [platformId, lane] of [...importWorkerLanes.entries()]) {
    discardPlatformWorker(
      platformId,
      lane.pendingRequests.size
        ? new TargetImportError('target_import_worker_restarted')
        : null,
    );
  }
}
