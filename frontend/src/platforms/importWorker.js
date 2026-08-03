import {
  isTargetImportPolicyModuleLoadFailure,
  normalizeTargetImportForPlatformInCurrentThread,
} from './importRuntime.js';
import {
  TARGET_IMPORT_FILE_MAX_BYTES,
  TargetImportError,
} from './importShared.js';

// A module-load failure prevents this handshake. Once it is observed, later
// worker errors are parser/runtime faults and must not be mistaken for a stale
// deployment chunk that warrants reloading the whole page.
globalThis.postMessage({ type: 'dpms-target-import-ready' });

function decodedImportContent(data) {
  if (data?.contentBuffer === undefined) {
    return String(data?.content || '');
  }
  if (!(data.contentBuffer instanceof ArrayBuffer)) {
    throw new TargetImportError('target_import_worker_failed');
  }
  const byteLength = data.contentBuffer.byteLength;
  if (byteLength > TARGET_IMPORT_FILE_MAX_BYTES) {
    throw new TargetImportError('target_import_too_large', {
      byteLength,
      maxBytes: TARGET_IMPORT_FILE_MAX_BYTES,
    });
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(
      data.contentBuffer,
    );
  } catch {
    throw new TargetImportError('target_import_invalid_encoding');
  }
}

function serializedError(error) {
  const candidateCode = String(error?.code || '');
  const isTypedImportError = (
    error?.name === 'TargetImportError'
    && /^[a-z][a-z0-9_]{2,127}$/.test(candidateCode)
  );
  return {
    // Unexpected parser/import errors may contain snippets of the source in
    // their message. Only the importer's finite error-code contract may cross
    // the worker boundary.
    name: 'TargetImportError',
    code: isTypedImportError
      ? candidateCode
      : 'target_import_worker_failed',
    platformId: isTypedImportError && error?.platformId
      ? String(error.platformId)
      : null,
    platformIds: isTypedImportError && Array.isArray(error?.platformIds)
      ? error.platformIds.map(value => String(value))
      : null,
    byteLength: isTypedImportError && Number.isSafeInteger(error?.byteLength)
      ? error.byteLength
      : null,
    maxBytes: isTypedImportError && Number.isSafeInteger(error?.maxBytes)
      ? error.maxBytes
      : null,
  };
}

globalThis.onmessage = async ({ data }) => {
  const requestId = Number(data?.requestId);
  if (!Number.isSafeInteger(requestId) || requestId <= 0) return;
  try {
    const content = decodedImportContent(data);
    const result = await normalizeTargetImportForPlatformInCurrentThread(
      data?.platform,
      content,
      data?.options,
    );
    globalThis.postMessage({ requestId, ok: true, result });
  } catch (error) {
    const moduleGraphError = isTargetImportPolicyModuleLoadFailure(error);
    globalThis.postMessage({
      requestId,
      ok: false,
      moduleGraphError,
      moduleGraphPlatformId: moduleGraphError
        ? String(error.platformId || '')
        : null,
      error: serializedError(error),
    });
  }
};
