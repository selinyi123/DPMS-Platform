export function targetImportCanSubmit({
  content,
  fileBusy = false,
  importBusy = false,
  moduleReady = false,
} = {}) {
  return moduleReady
    && !fileBusy
    && !importBusy
    && String(content || '').trim().length > 0;
}

export function targetImportOperationIsCurrent({
  mounted = false,
  expectedGeneration,
  currentGeneration,
  expectedOperationId,
  currentOperationId,
} = {}) {
  return mounted
    && expectedGeneration === currentGeneration
    && expectedOperationId === currentOperationId;
}

export function targetImportNotificationLevel(
  result = {},
  normalizedImport = {},
) {
  return (
    Number(result.invalid_count || 0) > 0
    || Number(normalizedImport.discardedRows || 0) > 0
    || Number(normalizedImport.blockedShortLinkCount || 0) > 0
  ) ? 'warning' : 'success';
}

const GENERIC_API_ERROR_CODES = new Set([
  'api_error',
  'http_error',
  'network_error',
  'timeout',
]);
const BUSINESS_ERROR_CODE_PATTERN = /(?:^|:\s*)([a-z][a-z0-9_]{2,})$/i;

export function targetOperationErrorCode(error) {
  const directCode = typeof error?.code === 'string' ? error.code.trim() : '';
  const candidates = [
    error?.details?.reason_code,
    error?.reason_code,
    error?.serverCode,
    error?.details?.code,
    !GENERIC_API_ERROR_CODES.has(directCode) ? directCode : '',
    typeof error === 'string' ? error : error?.message,
  ];
  for (const candidate of candidates) {
    const match = BUSINESS_ERROR_CODE_PATTERN.exec(String(candidate || '').trim());
    if (match) return match[1].toLowerCase();
  }
  return '';
}

export function targetImportInvalidErrorCode(item = {}) {
  return targetOperationErrorCode(item?.reason_code)
    || targetOperationErrorCode(item?.error);
}
