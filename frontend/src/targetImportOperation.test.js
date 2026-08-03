import assert from 'node:assert/strict';
import test from 'node:test';

import {
  targetImportCanSubmit,
  targetImportInvalidErrorCode,
  targetImportNotificationLevel,
  targetImportOperationIsCurrent,
  targetOperationErrorCode,
} from './targetImportOperation.js';

test('target import submission rejects file reads, imports, and blank drafts', () => {
  const ready = {
    content: 'bilibili,https://t.bilibili.com/1',
    moduleReady: true,
  };
  assert.equal(targetImportCanSubmit(ready), true);
  assert.equal(targetImportCanSubmit({ ...ready, fileBusy: true }), false);
  assert.equal(targetImportCanSubmit({ ...ready, importBusy: true }), false);
  assert.equal(targetImportCanSubmit({ ...ready, content: ' \n ' }), false);
  assert.equal(targetImportCanSubmit({ ...ready, moduleReady: false }), false);
});

test('target import async results are rejected after generation or operation changes', () => {
  const current = {
    mounted: true,
    expectedGeneration: 4,
    currentGeneration: 4,
    expectedOperationId: 9,
    currentOperationId: 9,
  };
  assert.equal(targetImportOperationIsCurrent(current), true);
  assert.equal(targetImportOperationIsCurrent({
    ...current,
    currentGeneration: 5,
  }), false);
  assert.equal(targetImportOperationIsCurrent({
    ...current,
    currentOperationId: 10,
  }), false);
  assert.equal(targetImportOperationIsCurrent({
    ...current,
    mounted: false,
  }), false);
});

test('partial local or Core target rejection can never render as success', () => {
  assert.equal(
    targetImportNotificationLevel(
      { invalid_count: 0 },
      { discardedRows: 0, blockedShortLinkCount: 0 },
    ),
    'success',
  );
  assert.equal(
    targetImportNotificationLevel(
      { invalid_count: 2 },
      { discardedRows: 0, blockedShortLinkCount: 0 },
    ),
    'warning',
  );
  assert.equal(
    targetImportNotificationLevel(
      { invalid_count: 0 },
      { discardedRows: 0, blockedShortLinkCount: 2 },
    ),
    'warning',
  );
});

test('target operation errors preserve Core business codes behind API transport errors', () => {
  assert.equal(
    targetOperationErrorCode({
      code: 'http_error',
      message: '400: lottery_target_canonicalization_failed',
    }),
    'lottery_target_canonicalization_failed',
  );
  assert.equal(
    targetOperationErrorCode({
      code: 'http_error',
      serverCode: 'lottery_target_canonicalization_failed',
      details: { code: 'lottery_target_canonicalization_failed' },
    }),
    'lottery_target_canonicalization_failed',
  );
  assert.equal(
    targetOperationErrorCode({ code: 'target_import_too_large' }),
    'target_import_too_large',
  );
  assert.equal(targetOperationErrorCode(new Error('Network disconnected')), '');
});

test('canonicalization reason codes take priority for manual and batch failures', () => {
  assert.equal(
    targetOperationErrorCode({
      code: 'http_error',
      serverCode: 'lottery_target_canonicalization_failed',
      details: {
        code: 'lottery_target_canonicalization_failed',
        reason_code: 'canonicalization_short_link_timeout',
        retryable: true,
      },
    }),
    'canonicalization_short_link_timeout',
  );
  assert.equal(
    targetImportInvalidErrorCode({
      error: 'lottery_target_canonicalization_failed',
      reason_code: 'canonicalization_target_not_allowed',
      retryable: false,
    }),
    'canonicalization_target_not_allowed',
  );
  assert.equal(
    targetImportInvalidErrorCode({
      error: 'lottery_target_canonicalization_failed',
    }),
    'lottery_target_canonicalization_failed',
  );
});
