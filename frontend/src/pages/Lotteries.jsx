import { useEffect, useMemo, useRef, useState } from 'react';

import { fetchJSON, postJSON, putJSON } from '../api';
import { settleRequestSlicesIndependently } from '../asyncSlices';
import StatusBadge from '../components/StatusBadge';
import AuthenticatedAssetLink from '../components/AuthenticatedAssetLink';
import { formatText } from '../i18n/format';
import { lotteryPagePlatformModuleDemand } from '../platformModuleDemand';
import {
  actionRequirementValues,
  authoritativeRuleText,
  automaticFollowTarget,
  defaultRepostText,
  lotteryTargetIdentity,
  ruleEditorSaveBlockers,
  sourceRequires,
  validLotteryHandle,
  visibleRuleSnapshotParts,
} from '../lotteryRuleEditor';
import {
  actionPlanHasMediaRequirement,
  actionPlanV2Blockers,
  actionPlanV2Ready,
  actionPlanV2ReviewBlockers,
  actionPlanV2ReviewReady,
  buildActionPlanV2Update,
  dispatchSafetyBlocker,
  evidenceResponseMatchesAccountScope,
  exactActionPayloadErrors,
  executionEvidencePresentation,
  isFixedManualActionPlatform,
  isManualAssistedPlan,
  isManualAssistedPlatform,
  lotteryActionsForPlatform,
  manualAssistedChecklist,
  manualParticipationCanSubmit,
  manualParticipationConfirmationEnabled,
  manualParticipationIsFinalized,
  manualParticipationResultNote,
  manualShadowObservation,
  platformDispatchBlocker,
  platformExecutionPathId,
  realRunEvidencePath,
  sourceRuleCorrectionPath,
  targetTransportCompatibilityIssue,
  targetValidationErrorCode,
  unresolvedRuleRequirements,
} from '../lotteryCompatibility';
import { useUi } from '../uiContext';
import {
  accountMatchesPlatformDispatch,
  buildPlatformAccountCredentialIndex,
  hasEligibleAccountForPlatformDispatch,
  normalizePlatformTargetImport,
  normalizedAccountCredentialKind,
  platformDiscoverySourceTypes,
  platformExecutionPathPresentation,
  platformExecutionPaths,
  platformRealTargetKinds,
  platformSupportsDiscoverySource,
  platformSupportsExecutionPath,
  platformSupportsStructuredTargetImport,
  readTargetImportFile,
  TARGET_IMPORT_FILE_MAX_BYTES,
  TargetImportError,
} from '../platforms';
import {
  targetImportCanSubmit,
  targetImportInvalidErrorCode,
  targetImportNotificationLevel,
  targetImportOperationIsCurrent,
  targetOperationErrorCode,
} from '../targetImportOperation';

function probePhaseTotal(summary, platform) {
  return Array.isArray(summary?.required_phases) && summary.required_phases.length
    ? summary.required_phases.length
    : lotteryActionsForPlatform(platform).length;
}

const API_ACTION_PHASES = {
  follow: 'followed',
  like: 'liked',
  comment: 'commented',
  favorite: 'favorited',
  repost: 'reposted',
};
const REAL_RUN_EVIDENCE_TTL_MS = 65000;
const MANUAL_DISCOVERY_SCAN_TIMEOUT_MS = 145_000;

export default function Lotteries({
  platformModuleStates = {},
  requestPlatformModules = null,
}) {
  const { notify, t } = useUi();
  const loadInFlightRef = useRef(false);
  const evidenceRefreshPendingRef = useRef(false);
  const evidenceExpiryTimerRef = useRef(null);
  const evidenceRequestGenerationRef = useRef(0);
  const selectedAccountRef = useRef('');
  const targetFileReadIdRef = useRef(0);
  const targetImportGenerationRef = useRef(0);
  const targetImportOperationRef = useRef(0);
  const targetImportBusyRef = useRef(false);
  // Keep large imported text out of React's reconciliation graph. The
  // textarea and this ref are the single draft owner; platform/score changes
  // still use the small targetImport state object.
  const targetImportContentRef = useRef('');
  const targetImportTextareaRef = useRef(null);
  const targetFileBusyRef = useRef(false);
  const loadErrorSignatureRef = useRef('');
  const mountedRef = useRef(true);
  const [lotteries, setLotteries] = useState([]);
  const [runs, setRuns] = useState([]);
  const [probes, setProbes] = useState([]);
  const [sources, setSources] = useState([]);
  const [strategyQueue, setStrategyQueue] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [platforms, setPlatforms] = useState([]);
  const [adapters, setAdapters] = useState([]);
  const [readiness, setReadiness] = useState(null);
  const [realRunEvidence, setRealRunEvidence] = useState([]);
  const [error, setError] = useState('');
  const [activityFormError, setActivityFormError] = useState('');
  const [targetImportError, setTargetImportError] = useState('');
  const [discoveryMessage, setDiscoveryMessage] = useState('');
  const [dispatchMode, setDispatchMode] = useState('dry_run');
  const [selectedAccount, setSelectedAccount] = useState('');
  const [form, setForm] = useState({ platform: 'bilibili', raw_url: '', value_score: 50 });
  const [targetImport, setTargetImport] = useState({ platform: 'bilibili', value_score: 50 });
  const [targetImportResult, setTargetImportResult] = useState(null);
  const [targetImportBusy, setTargetImportBusy] = useState(false);
  const [targetFileBusy, setTargetFileBusy] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [sourceForm, setSourceForm] = useState({
    platform: 'bilibili',
    source_type: 'url_list',
    source_value: '',
    scan_interval_minutes: 30,
  });

  const sourcePlatformModuleReady = (
    platformModuleStates[sourceForm.platform] === 'ready'
  );
  const sourceTypeOptions = useMemo(
    () => platformDiscoverySourceTypes(sourceForm.platform),
    [sourceForm.platform, sourcePlatformModuleReady],
  );

  const adapterById = useMemo(
    () => Object.fromEntries(adapters.map(adapter => [adapter.platform, adapter])),
    [adapters],
  );

  const readinessById = useMemo(
    () => Object.fromEntries((readiness?.platforms || []).map(platform => [platform.platform, platform])),
    [readiness],
  );

  const gateByLotteryId = useMemo(
    () => Object.fromEntries(realRunEvidence.map(item => [item.lottery_id, item])),
    [realRunEvidence],
  );

  const safeAccounts = useMemo(
    () => accounts.filter(account => account.status === 'ready' && account.credential_ready && account.latest_calibration?.status === 'succeeded'),
    [accounts],
  );
  const safeAccountCredentialIndex = useMemo(
    () => buildPlatformAccountCredentialIndex(safeAccounts),
    [safeAccounts],
  );

  const realRunEnabled = Boolean(realRunEvidence[0]?.real_run_enabled);
  const safeAccountCount = platformId => safeAccounts.filter(account => account.platform === platformId).length;
  const selectedSafeAccount = safeAccounts.find(account => String(account.id) === String(selectedAccount));
  const executionPathForLottery = lottery => lottery?.action_plan?.execution_path_id || '';
  const hasEligibleSafeAccountFor = (lottery, mode) => hasEligibleAccountForPlatformDispatch(
    safeAccountCredentialIndex,
    lottery?.platform,
    mode,
    executionPathForLottery(lottery),
  );
  const selectedAccountMatchesPlatform = lottery => Boolean(
    lottery
    && selectedSafeAccount
    && selectedSafeAccount.platform === lottery.platform
  );
  const selectedAccountMatchesDispatch = (lottery, mode) => accountMatchesPlatformDispatch(
    selectedSafeAccount,
    lottery?.platform,
    mode,
    executionPathForLottery(lottery),
  );
  const manualTargetIssue = targetTransportCompatibilityIssue(form.platform, form.raw_url);
  const targetImportModuleReady = platformModuleStates[targetImport.platform] === 'ready';
  const platformModuleDemand = useMemo(
    () => lotteryPagePlatformModuleDemand({
      createPlatform: form.platform,
      importPlatform: targetImport.platform,
      sourcePlatform: sourceForm.platform,
      lotteries,
      strategyQueue,
    }),
    [
      form.platform,
      lotteries,
      sourceForm.platform,
      strategyQueue,
      targetImport.platform,
    ],
  );

  const updateTargetImportDraft = (update) => {
    targetImportGenerationRef.current += 1;
    targetFileReadIdRef.current += 1;
    if (targetFileBusyRef.current) {
      targetFileBusyRef.current = false;
      setTargetFileBusy(false);
    }
    setTargetImportError('');
    setTargetImportResult(null);
    setTargetImport(previous => (
      typeof update === 'function' ? update(previous) : update
    ));
  };

  const setTargetImportContent = (content) => {
    const nextContent = String(content || '');
    targetImportContentRef.current = nextContent;
    if (
      targetImportTextareaRef.current
      && targetImportTextareaRef.current.value !== nextContent
    ) {
      targetImportTextareaRef.current.value = nextContent;
    }
  };

  useEffect(() => {
    if (typeof requestPlatformModules === 'function') {
      requestPlatformModules(platformModuleDemand);
    }
  }, [platformModuleDemand, requestPlatformModules]);

  useEffect(() => {
    if (!sourcePlatformModuleReady || !sourceTypeOptions.length) return;
    setSourceForm((previous) => {
      if (
        previous.platform !== sourceForm.platform
        || sourceTypeOptions.includes(previous.source_type)
      ) return previous;
      return { ...previous, source_type: sourceTypeOptions[0] };
    });
  }, [
    sourceForm.platform,
    sourcePlatformModuleReady,
    sourceTypeOptions,
  ]);

  const load = async (includeRealRunEvidence = true) => {
    if (!mountedRef.current) return;
    const evidenceAccountId = includeRealRunEvidence
      ? String(selectedAccountRef.current || '')
      : '';
    const evidenceGeneration = includeRealRunEvidence
      ? evidenceRequestGenerationRef.current + 1
      : evidenceRequestGenerationRef.current;
    if (includeRealRunEvidence) {
      evidenceRequestGenerationRef.current = evidenceGeneration;
      window.clearTimeout(evidenceExpiryTimerRef.current);
      setRealRunEvidence([]);
    }
    if (loadInFlightRef.current) {
      evidenceRefreshPendingRef.current ||= includeRealRunEvidence;
      return;
    }
    loadInFlightRef.current = true;
    try {
      const applyWhileMounted = apply => (value) => {
        if (mountedRef.current) apply(value);
      };
      const handles = settleRequestSlicesIndependently([
        {
          key: 'lotteries',
          request: fetchJSON('/lotteries/'),
          onFulfilled: applyWhileMounted(setLotteries),
        },
        {
          key: 'runs',
          request: fetchJSON('/lotteries/tasks/runs'),
          onFulfilled: applyWhileMounted(setRuns),
        },
        {
          key: 'probes',
          request: fetchJSON('/lotteries/probes'),
          onFulfilled: applyWhileMounted(setProbes),
        },
        {
          key: 'sources',
          request: fetchJSON('/lotteries/sources'),
          onFulfilled: applyWhileMounted(setSources),
        },
        {
          key: 'strategy',
          // Account-scoped readiness performs authoritative evidence checks.
          // Refresh it with the one-minute evidence cadence rather than every
          // lightweight 15-second status poll.
          request: includeRealRunEvidence
            ? fetchJSON('/lotteries/strategy/queue')
            : Promise.resolve(null),
          onFulfilled: applyWhileMounted((value) => {
            if (value) setStrategyQueue(value.items || []);
          }),
        },
        {
          key: 'accounts',
          request: fetchJSON('/accounts/'),
          onFulfilled: applyWhileMounted(setAccounts),
        },
        {
          key: 'platforms',
          request: fetchJSON('/accounts/platforms'),
          onFulfilled: applyWhileMounted(setPlatforms),
        },
        {
          key: 'adapters',
          request: fetchJSON('/lotteries/adapters'),
          onFulfilled: applyWhileMounted(setAdapters),
        },
        {
          key: 'readiness',
          request: fetchJSON('/metrics/readiness'),
          onFulfilled: applyWhileMounted(setReadiness),
        },
        {
          key: 'evidence',
          request: includeRealRunEvidence
            ? fetchJSON(realRunEvidencePath(evidenceAccountId))
            : Promise.resolve(null),
          onFulfilled: applyWhileMounted((value) => {
            if (!value) return;
            if (
              evidenceRequestGenerationRef.current !== evidenceGeneration
              || String(selectedAccountRef.current || '') !== evidenceAccountId
              || !evidenceResponseMatchesAccountScope(value, evidenceAccountId)
            ) return;
            setRealRunEvidence(value.items || []);
            window.clearTimeout(evidenceExpiryTimerRef.current);
            evidenceExpiryTimerRef.current = window.setTimeout(() => {
              if (mountedRef.current) setRealRunEvidence([]);
            }, REAL_RUN_EVIDENCE_TTL_MS);
          }),
        },
      ]);
      const results = await Promise.all(handles);
      if (!mountedRef.current) return;

      const failures = results
        .filter(result => (
          result.status === 'rejected'
          && (
            result.key !== 'evidence'
            || (
              includeRealRunEvidence
              && evidenceRequestGenerationRef.current === evidenceGeneration
              && String(selectedAccountRef.current || '') === evidenceAccountId
            )
          )
        ));
      if (failures.length) {
        const firstReason = failures[0].error;
        const firstMessage = firstReason instanceof Error
          ? firstReason.message
          : String(firstReason || 'request_failed');
        const message = targetValidationErrorText(firstMessage, t);
        const signature = failures.map(result => result.key).join(',');
        setLoadError(message);
        if (loadErrorSignatureRef.current !== signature) notify(message, 'error');
        loadErrorSignatureRef.current = signature;
      } else {
        setLoadError('');
        loadErrorSignatureRef.current = '';
      }
    } catch (err) {
      if (!mountedRef.current) return;
      if (includeRealRunEvidence) setRealRunEvidence([]);
      const message = targetValidationErrorText(err.message, t);
      setLoadError(message);
      if (loadErrorSignatureRef.current !== message) notify(message, 'error');
      loadErrorSignatureRef.current = message;
    } finally {
      loadInFlightRef.current = false;
      if (mountedRef.current && evidenceRefreshPendingRef.current) {
        evidenceRefreshPendingRef.current = false;
        void load(true);
      } else if (!mountedRef.current) {
        evidenceRefreshPendingRef.current = false;
      }
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    load(true);
    let pollCount = 0;
    const timer = setInterval(() => {
      pollCount += 1;
      // Probe and Shadow completion is asynchronous. Refresh their evidence at
      // least once per minute so the displayed gate cannot remain stale for the
      // lifetime of the page, while keeping the 15-second polls lightweight.
      load(pollCount % 4 === 0);
    }, 15000);
    return () => {
      mountedRef.current = false;
      evidenceRefreshPendingRef.current = false;
      clearInterval(timer);
      window.clearTimeout(evidenceExpiryTimerRef.current);
    };
  }, []);

  const createLottery = async (e) => {
    e.preventDefault();
    setActivityFormError('');
    try {
      await postJSON('/lotteries/', {
        platform: form.platform,
        source_type: 'manual',
        raw_url: form.raw_url,
        canonical_url: form.raw_url,
        value_score: Number(form.value_score || 0),
      });
      notify(t('lotteries.activityCreated'), 'success');
      await load();
    } catch (err) {
      const message = targetImportErrorText(err, t);
      setActivityFormError(message);
      notify(message, 'error');
    }
  };

  const importTargets = async (e) => {
    e.preventDefault();
    if (!targetImportCanSubmit({
      content: targetImportContentRef.current,
      fileBusy: targetFileBusyRef.current,
      importBusy: targetImportBusyRef.current,
      moduleReady: targetImportModuleReady,
    })) return;
    targetImportBusyRef.current = true;
    setTargetImportBusy(true);
    targetFileReadIdRef.current += 1;
    const operationId = targetImportOperationRef.current + 1;
    targetImportOperationRef.current = operationId;
    const generation = targetImportGenerationRef.current;
    const snapshot = {
      ...targetImport,
      content: targetImportContentRef.current,
    };
    const isCurrent = () => targetImportOperationIsCurrent({
      mounted: mountedRef.current,
      expectedGeneration: generation,
      currentGeneration: targetImportGenerationRef.current,
      expectedOperationId: operationId,
      currentOperationId: targetImportOperationRef.current,
    });
    setTargetImportError('');
    setTargetImportResult(null);
    try {
      const normalizedImport = await normalizeTargetImport(snapshot);
      if (!isCurrent()) return;
      if (normalizedImport.converted) {
        const messageKey = targetImportSanitizedMessageKey(snapshot.platform);
        notify(formatText(t(messageKey), {
          count: normalizedImport.targetCount,
          discarded: normalizedImport.discardedSensitiveFields,
          skipped: normalizedImport.discardedRows,
        }), 'warning');
      }
      const result = await postJSON('/lotteries/targets/import', {
        platform: snapshot.platform,
        content: normalizedImport.content,
        value_score: Number(snapshot.value_score || 0),
      }, {
        timeoutMs: targetImportTimeout(normalizedImport),
      });
      if (!isCurrent()) return;
      if (normalizedImport.converted) {
        // Keep the original draft recoverable until Core has durably accepted
        // the normalized rows. This matters for partial mixed-platform
        // rejections and for retrying a failed request.
        if (isCurrent()) setTargetImportContent(normalizedImport.content);
      }
      const displayedResult = {
        ...result,
        local_skipped_count: normalizedImport.discardedRows || 0,
      };
      setTargetImportResult(displayedResult);
      const message = formatText(t('lotteries.targetsImported'), result);
      notify(message, targetImportNotificationLevel(result, normalizedImport));
      await load();
      if (!isCurrent()) return;
    } catch (err) {
      if (!isCurrent()) return;
      const message = targetImportErrorText(err, t);
      setTargetImportError(message);
      notify(message, 'error');
    } finally {
      if (targetImportOperationRef.current === operationId) {
        targetImportBusyRef.current = false;
        if (mountedRef.current) setTargetImportBusy(false);
      }
    }
  };

  const readTargetFile = async (e) => {
    if (targetImportBusyRef.current) return;
    const file = e.target.files?.[0];
    if (!file) return;
    const input = e.currentTarget;
    const readId = targetFileReadIdRef.current + 1;
    targetFileReadIdRef.current = readId;
    const generation = targetImportGenerationRef.current + 1;
    targetImportGenerationRef.current = generation;
    const snapshot = { ...targetImport, content: '' };
    const isCurrent = () => targetImportOperationIsCurrent({
      mounted: mountedRef.current,
      expectedGeneration: generation,
      currentGeneration: targetImportGenerationRef.current,
      expectedOperationId: readId,
      currentOperationId: targetFileReadIdRef.current,
    });
    targetFileBusyRef.current = true;
    setTargetFileBusy(true);
    setTargetImportError('');
    setTargetImportResult(null);
    setTargetImport(snapshot);
    setTargetImportContent('');
    try {
      // This is only the shared browser-memory ceiling. The normalizer assigns
      // delimited rows to their declared platforms and enforces each owning
      // policy's byte limit after reading the bounded file.
      if (file.size > TARGET_IMPORT_FILE_MAX_BYTES) {
        throw new TargetImportError('target_import_too_large', {
          byteLength: file.size,
          maxBytes: TARGET_IMPORT_FILE_MAX_BYTES,
        });
      }
      const content = await readTargetImportFile(file);
      if (!isCurrent()) return;
      const normalizedImport = await normalizeTargetImport(
        { ...snapshot, content },
      );
      if (!isCurrent()) return;
      if (isCurrent()) setTargetImportContent(normalizedImport.content);
      const messageKey = targetImportSanitizedMessageKey(snapshot.platform);
      const message = normalizedImport.converted
        ? formatText(t(messageKey), {
            count: normalizedImport.targetCount,
            discarded: normalizedImport.discardedSensitiveFields,
            skipped: normalizedImport.discardedRows,
          })
        : formatText(t('lotteries.targetFileLoaded'), { name: file.name });
      notify(message, normalizedImport.converted ? 'warning' : 'success');
    } catch (err) {
      if (!isCurrent()) return;
      const message = targetImportErrorText(err, t);
      setTargetImportError(message);
      notify(message, 'error');
    } finally {
      input.value = '';
      if (readId === targetFileReadIdRef.current) {
        targetFileBusyRef.current = false;
        if (mountedRef.current) setTargetFileBusy(false);
      }
    }
  };

  const createSource = async (e) => {
    e.preventDefault();
    setError('');
    setDiscoveryMessage('');
    try {
      if (!platformSupportsDiscoverySource(sourceForm.platform, sourceForm.source_type)) {
        throw new Error(t('lotteries.discoverySourceUnsupported'));
      }
      await postJSON('/lotteries/sources', {
        ...sourceForm,
        scan_interval_minutes: Number(sourceForm.scan_interval_minutes || 30),
      });
      setDiscoveryMessage(t('lotteries.sourceSaved'));
      notify(t('lotteries.sourceSaved'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const scanSources = async () => {
    setError('');
    setDiscoveryMessage('');
    try {
      const result = await postJSON('/lotteries/sources/scan', {}, {
        timeoutMs: MANUAL_DISCOVERY_SCAN_TIMEOUT_MS,
      });
      const message = formatText(t('lotteries.scanComplete'), result);
      setDiscoveryMessage(message);
      notify(message, result.failed ? 'error' : 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const saveActionPlan = async (lottery, draft) => {
    setError('');
    try {
      const result = await putJSON(
        `/lotteries/${lottery.id}/action-plan`,
        buildActionPlanV2Update({ ...draft, platform: lottery.platform }),
      );
      const manualAssisted = isManualAssistedPlan(
        lottery.platform,
        result?.action_plan,
      );
      const stillBlocked = manualAssisted
        ? !actionPlanV2ReviewReady(result?.action_plan, lottery.platform)
        : !actionPlanV2Ready(result?.action_plan, lottery.platform);
      notify(
        stillBlocked
          ? t('lotteries.ruleNeedsReview')
          : t(manualAssisted ? 'lotteries.manualRuleSaved' : 'lotteries.ruleSaved'),
        stillBlocked ? 'warning' : 'success',
      );
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const dispatchBlockerFor = (lottery, mode) => dispatchSafetyBlocker({
    lottery,
    mode,
    gate: lottery ? gateByLotteryId[lottery.id] : null,
    safeAccountAvailable: hasEligibleSafeAccountFor(lottery, mode),
    accountScopeBound: selectedAccountMatchesPlatform(lottery),
    accountScopeCompatible: selectedAccountMatchesDispatch(lottery, mode),
  });

  const accountScopeBlockerFor = (lottery, mode) => {
    if (!hasEligibleSafeAccountFor(lottery, mode)) return 'no_safe_account';
    if (!selectedAccountMatchesPlatform(lottery)) return 'account_scope_required';
    if (!selectedAccountMatchesDispatch(lottery, mode)) return 'account_credential_kind_mismatch';
    return null;
  };

  const dispatchBlockerMessage = (blocker, lottery) => {
    if (blocker === 'xiaohongshu_manual_only') return t('lotteries.xiaohongshuManualOnlyHint');
    if (blocker === 'xiaohongshu_manual_shadow_only') return t('lotteries.xiaohongshuManualShadowOnlyHint');
    if (blocker === 'douyin_manual_only') return t('lotteries.douyinManualOnlyHint');
    if (blocker === 'douyin_manual_shadow_only') return t('lotteries.douyinManualShadowOnlyHint');
    if (blocker === 'weibo_manual_only') return t('lotteries.weiboManualOnlyHint');
    if (blocker === 'weibo_manual_shadow_only') return t('lotteries.weiboManualShadowOnlyHint');
    if (blocker === 'legacy_http_target') return t('lotteries.legacyHttpTargetHint');
    if (blocker === 'no_safe_account') return t('lotteries.noSafeAccount');
    if (blocker === 'account_credential_kind_mismatch') return t('lotteries.accountCredentialKindMismatch');
    if (blocker === 'account_scope_required') return t('lotteries.accountScopeRequired');
    if (blocker === 'mode_blocked') return gateTitle(gateByLotteryId[lottery?.id], t);
    if (blocker === 'execution_path_mismatch') {
      return t('lotteries.actionPlanBlockers.execution_path_mismatch');
    }
    if (blocker === 'action_plan_v2') return t('lotteries.actionPlanV2Required');
    if (blocker === 'real_run_gate') return gateTitle(gateByLotteryId[lottery?.id], t);
    return t('lotteries.gateUnknown');
  };

  const dispatch = async (id, modeOverride = dispatchMode) => {
    setError('');
    try {
      const lottery = lotteries.find(item => item.id === id);
      const mode = modeOverride || dispatchMode;
      const uiBlocker = dispatchBlockerFor(lottery, mode);
      if (uiBlocker) {
        const message = dispatchBlockerMessage(uiBlocker, lottery);
        setError(message);
        notify(message, 'warning');
        return;
      }
      const selectedMatches = selectedAccountMatchesDispatch(lottery, mode);
      await postJSON(`/lotteries/${id}/dispatch`, {
        mode,
        dry_run: mode !== 'real_run',
        confirm: mode === 'real_run',
        account_id: selectedMatches ? Number(selectedAccount) : null,
      }, { confirm: mode === 'real_run' });
      notify(t('lotteries.dispatchQueued'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const repairMissingActions = async (id) => {
    setError('');
    try {
      const lottery = lotteries.find(item => item.id === id);
      const accountBlocker = accountScopeBlockerFor(lottery, 'real_run');
      if (accountBlocker) {
        const message = dispatchBlockerMessage(accountBlocker, lottery);
        setError(message);
        notify(message, 'warning');
        return;
      }
      const selectedMatches = selectedAccountMatchesDispatch(lottery, 'real_run');
      await postJSON(`/lotteries/${id}/repair-dispatch`, {
        confirm: true,
        account_id: selectedMatches ? Number(selectedAccount) : null,
      }, { confirm: true });
      notify(t('lotteries.repairQueued'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const probe = async (id) => {
    setError('');
    try {
      const lottery = lotteries.find(item => item.id === id);
      const accountBlocker = accountScopeBlockerFor(lottery, 'shadow_run');
      if (accountBlocker) {
        const message = dispatchBlockerMessage(accountBlocker, lottery);
        setError(message);
        notify(message, 'warning');
        return;
      }
      const selectedMatches = selectedAccountMatchesDispatch(lottery, 'shadow_run');
      await postJSON(`/lotteries/${id}/probe`, { account_id: selectedMatches ? Number(selectedAccount) : null });
      notify(t('lotteries.probeQueued'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const runGateNextAction = async (lottery, gate) => {
    const action = gate?.next_action || 'blocked';
    if (action === 'probe') return probe(lottery.id);
    if (action === 'shadow_run') return dispatch(lottery.id, 'shadow_run');
    if (action === 'real_run') return dispatch(lottery.id, 'real_run');
    const message = t(`lotteries.nextActionHints.${action}`);
    notify(message === `lotteries.nextActionHints.${action}` ? gateTitle(gate, t) : message, 'warning');
  };

  const probeSummary = (probe) => {
    const result = typeof probe.result === 'string' ? safeJson(probe.result) : probe.result;
    return result?._summary || null;
  };

  const selectorObservationComplete = summary => {
    if (!summary) return false;
    if (typeof summary.selector_observation_complete === 'boolean') {
      return summary.selector_observation_complete;
    }
    // Older persisted probe results expose only this misleading legacy key.
    return summary.ready_for_real_actions === true;
  };

  const markResult = async (id, status, note = '') => {
    setError('');
    try {
      const resultNote = String(note || '').trim() || `Manual result set to ${status}`;
      await putJSON(`/lotteries/${id}/result`, { status, note: resultNote });
      notify(t('lotteries.resultUpdated'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('lotteries.eyebrow')}</p>
          <h1>{t('lotteries.title')}</h1>
        </div>
        <div className="toolbar">
          <select
            className="input compact-input"
            value={selectedAccount}
            onChange={e => {
              const accountId = e.target.value;
              selectedAccountRef.current = accountId;
              setSelectedAccount(accountId);
              void load(true);
            }}
          >
            <option value="">{t('lotteries.autoPick')}</option>
            {safeAccounts.map(account => (
              <option value={account.id} key={account.id}>
                A{account.id} / {account.platform} / {normalizedAccountCredentialKind(account)} / {t('lotteries.calibrated')}
              </option>
            ))}
          </select>
          <div className="segmented">
            <button className={dispatchMode === 'dry_run' ? 'active' : ''} onClick={() => setDispatchMode('dry_run')}>{t('lotteries.dryRun')}</button>
            <button className={dispatchMode === 'shadow_run' ? 'active' : ''} onClick={() => setDispatchMode('shadow_run')}>{t('lotteries.shadowRun')}</button>
            <button className={dispatchMode === 'real_run' ? 'active danger' : ''} onClick={() => setDispatchMode('real_run')}>{t('lotteries.real')}</button>
          </div>
          <span className={`badge ${realRunEnabled ? 'badge-danger' : 'badge-muted'}`}>
            {realRunEnabled ? t('lotteries.realRunSwitchOn') : t('lotteries.realRunSwitchOff')}
          </span>
        </div>
      </header>

      <div className="ops-grid three-columns">
        {platforms.map(platform => (
          <div className="panel platform-card" key={platform.id}>
            {(() => {
              const ready = readinessById[platform.id];
              const moduleReady = platformModuleStates[platform.id] === 'ready';
              const realTargetKinds = moduleReady
                ? platformRealTargetKinds(platform.id)
                : [];
              const selectorPhaseTotal = Array.isArray(ready?.latest_probe?.required_phases)
                && ready.latest_probe.required_phases.length
                ? ready.latest_probe.required_phases.length
                : moduleReady
                  ? lotteryActionsForPlatform(platform.id).length
                  : null;
              return (
                <>
            <div className="panel-kicker">{platform.id}</div>
            <div className="panel-title">{platform.label}</div>
            <div className="capability-row"><span>{t('lotteries.qrLogin')}</span><StatusBadge status={platform.qr_login ? 'ready' : 'failed'} /></div>
            <div className="capability-row"><span>{t('lotteries.cookieLogin')}</span><StatusBadge status={platform.cookie_login ? 'ready' : 'failed'} /></div>
            <div className="capability-row">
              <span>{t('lotteries.realActions')}</span>
              <StatusBadge status={ready?.ready_for_real_run ? 'ready' : platform.action_adapter ? 'gray' : 'pending'} />
            </div>
            <div className="capability-row">
              <span>{t('lotteries.realTargetKinds')}</span>
              <span className="mono small-text">
                {moduleReady
                  ? (realTargetKinds.length ? realTargetKinds.join(', ') : t('common.none'))
                  : '-'}
              </span>
            </div>
            <div className="capability-row"><span>{t('lotteries.adapter')}</span><span className="mono small-text">{platform.adapter_status || 'planned'}</span></div>
            <div className="capability-row"><span>{t('lotteries.safeAccounts')}</span><span className="mono small-text">{safeAccountCount(platform.id)}</span></div>
            <div className="capability-row"><span>{t('lotteries.selectorObservation')}</span><span className="mono small-text">{ready?.latest_probe ? `${ready.latest_probe.ready_phase_count || 0}/${selectorPhaseTotal ?? '-'}` : '-'}</span></div>
            <p className="muted-text tight-text">{adapterById[platform.id]?.notes || t('lotteries.adapterLoading')}</p>
            {!!ready?.blocker_codes?.length && (
              <div className="blocker-list compact-blockers">
                {ready.blocker_codes.map(code => <span className="badge badge-warn" key={code}>{gateBlockerText(code, t)}</span>)}
              </div>
            )}
            <div className="capability-row"><span>{t('lotteries.cookieDomain')}</span><span className="mono small-text">{platform.cookie_domain}</span></div>
                </>
              );
            })()}
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.strategyQueue')}</div>
        <p className="muted-text tight-text">{t('lotteries.strategyQueueHint')}</p>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('lotteries.rank')}</th>
                <th>{t('lotteries.activity')}</th>
                <th>{t('lotteries.priorityScore')}</th>
                <th>{t('lotteries.expectedValue')}</th>
                <th>{t('lotteries.recommendedMode')}</th>
                <th>{t('lotteries.recommendedAccount')}</th>
                <th>{t('lotteries.knowledge')}</th>
                <th>{t('lotteries.reasons')}</th>
                <th>{t('lotteries.action')}</th>
              </tr>
            </thead>
            <tbody>
              {strategyQueue.map(item => {
                const lottery = lotteries.find(candidate => candidate.id === item.lottery_id);
                const strategyGate = gateByLotteryId[item.lottery_id];
                const strategyBlocker = item.recommended_mode === 'blocked'
                  ? 'mode_blocked'
                  : dispatchBlockerFor(lottery, item.recommended_mode);
                const strategyNextAction = strategyGate?.next_action
                  || (!actionPlanV2Ready(lottery?.action_plan, lottery?.platform)
                    ? 'review_rule'
                    : 'blocked');
                return (
                <tr key={item.lottery_id}>
                  <td className="mono">#{item.rank}</td>
                  <td>
                    <div className="mono">L{item.lottery_id} / {item.platform}</div>
                    <div className="truncate-cell small-text" title={item.raw_url}>{item.raw_url}</div>
                  </td>
                  <td>{item.strategy_score}</td>
                  <td>
                    <div className="mono">{formatNumber(item.expected_value)}</div>
                    <div className="small-text muted-text">{formatRate(item.estimated_win_probability)}</div>
                  </td>
                  <td><span className={`badge ${item.recommended_mode === 'blocked' ? 'badge-danger' : item.recommended_mode === 'real_run' ? 'badge-warn' : 'badge-info'}`}>{modeText(item.recommended_mode, t)}</span></td>
                  <td>
                    {item.recommended_account ? (
                      <div className="strategy-account">
                        <span className="mono">A{item.recommended_account.account_id}</span>
                        <div className="knowledge-score compact-score">
                          <span>{item.recommended_account.reputation_score}</span>
                          <div><i style={{ width: `${item.recommended_account.reputation_score || 0}%` }} /></div>
                        </div>
                      </div>
                    ) : (
                      <span className="badge badge-muted">{t('lotteries.autoPick')}</span>
                    )}
                  </td>
                  <td>
                    <div className="strategy-account">
                      <span className="mono">{item.platform_knowledge?.knowledge_confidence ?? 0}</span>
                      <div className="small-text muted-text">{formatRate(item.platform_knowledge?.win_rate)}</div>
                    </div>
                  </td>
                  <td>
                    {item.recommended_mode === 'blocked' && (
                      <strong className="small-text">{t('lotteries.strategyBlockReasons')}</strong>
                    )}
                    <div className="blocker-list compact-blockers">
                      {item.blockers?.map(reason => (
                        <span className="badge badge-danger" key={reason}>{gateBlockerText(reason, t)}</span>
                      ))}
                      {item.reason_codes?.map(code => (
                        <span className="badge badge-muted" key={code}>{reasonText(code, t)}</span>
                      ))}
                    </div>
                  </td>
                  <td>
                    <button
                      className={item.recommended_mode === 'real_run' ? 'btn-danger' : 'btn-primary'}
                      disabled={Boolean(strategyBlocker)}
                      title={strategyBlocker ? dispatchBlockerMessage(strategyBlocker, lottery) : ''}
                      onClick={() => dispatch(item.lottery_id, item.recommended_mode)}
                    >
                      {t('lotteries.dispatchRecommended')}
                    </button>
                    {strategyBlocker && (
                      <div className="strategy-next-step small-text warning-text">
                        <strong>{t('lotteries.strategyNextStep')}:</strong>{' '}
                        {nextActionText(strategyNextAction, t)}
                      </div>
                    )}
                  </td>
                </tr>
                );
              })}
              {!strategyQueue.length && <tr><td className="empty-cell" colSpan="9">{t('lotteries.noStrategyTargets')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.manualIntake')}</div>
        <div className="intake-split">
          <form onSubmit={createLottery} className="form-grid lottery-form">
            <label>
              <span>{t('lotteries.platform')}</span>
              <select className="input" value={form.platform} onChange={e => {
                setActivityFormError('');
                setForm({ ...form, platform: e.target.value });
              }}>
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <label className="url-field">
              <span>{t('lotteries.activityUrl')}</span>
              <input className="input" maxLength={512} value={form.raw_url} onChange={e => {
                setActivityFormError('');
                setForm({ ...form, raw_url: e.target.value });
              }} />
              {manualTargetIssue && (
                <span className="small-text warning-text" role="note">
                  {t('lotteries.targetErrors.https_required')}
                </span>
              )}
            </label>
            <label>
              <span>{t('lotteries.score')}</span>
              <input className="input" type="number" value={form.value_score} onChange={e => {
                setActivityFormError('');
                setForm({ ...form, value_score: e.target.value });
              }} />
            </label>
            <button className="btn-primary" type="submit">{t('lotteries.create')}</button>
            {activityFormError && <div className="alert-danger form-error" role="alert">{activityFormError}</div>}
          </form>
          <form onSubmit={importTargets} className="form-grid target-import-form">
            <label>
              <span>{t('lotteries.defaultPlatform')}</span>
              <select
                className="input"
                value={targetImport.platform}
                disabled={targetImportBusy}
                onChange={(e) => {
                  updateTargetImportDraft(previous => ({
                    ...previous,
                    platform: e.target.value,
                  }));
                }}
              >
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <label>
              <span>{t('lotteries.defaultScore')}</span>
              <input
                className="input"
                type="number"
                value={targetImport.value_score}
                disabled={targetImportBusy}
                onChange={e => updateTargetImportDraft(previous => ({
                  ...previous,
                  value_score: e.target.value,
                }))}
              />
            </label>
            <label className="btn-ghost file-button target-file-button">
              {t('lotteries.uploadTargets')}
              <input
                type="file"
                accept={platformSupportsStructuredTargetImport(targetImport.platform)
                  ? '.txt,.csv,.json,.jsonl,text/plain,text/csv,application/json'
                  : '.txt,.csv,text/plain,text/csv'}
                disabled={targetImportBusy || !targetImportModuleReady}
                onChange={readTargetFile}
                hidden
              />
            </label>
            <label className="target-text-field">
              <span>{t('lotteries.targetList')}</span>
              <textarea
                className="input textarea"
                ref={targetImportTextareaRef}
                defaultValue=""
                disabled={targetImportBusy}
                onChange={(e) => {
                  setTargetImportContent(e.target.value);
                  updateTargetImportDraft(previous => ({
                    ...previous,
                  }));
                }}
                placeholder={t('lotteries.targetPlaceholder')}
              />
              <span className="muted-text small-text">{t('lotteries.targetPlaceholderHint')}</span>
            </label>
            <button
              className="btn-primary"
              type="submit"
              disabled={!targetImportCanSubmit({
                content: targetImportContentRef.current,
                fileBusy: targetFileBusy,
                importBusy: targetImportBusy,
                moduleReady: targetImportModuleReady,
              })}
            >
              {t('lotteries.importTargets')}
            </button>
            {targetImportError && <div className="alert-danger form-error" role="alert">{targetImportError}</div>}
          </form>
        </div>
        {targetImportResult && (
          <div className="notice import-result">
            {formatText(t('lotteries.importSummary'), targetImportResult)}
            {!!targetImportResult.invalid?.length && (
              <div className="small-text">
                {targetImportResult.invalid
                  .slice(0, 3)
                  .map(item => (
                    `#${item.line}${item.platform ? ` [${item.platform}]` : ''}: `
                    + targetValidationErrorText(
                      targetImportInvalidErrorCode(item) || item.error,
                      t,
                    )
                  ))
                  .join(' / ')}
              </div>
            )}
          </div>
        )}
        {loadError && <div className="alert-danger">{loadError}</div>}
        {error && <div className="alert-danger">{error}</div>}
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.discoverySources')}</div>
        <form onSubmit={createSource} className="form-grid discovery-form">
          <label>
            <span>{t('lotteries.platform')}</span>
            <select
              className="input"
              value={sourceForm.platform}
              onChange={(e) => {
                const platform = e.target.value;
                setSourceForm({ ...sourceForm, platform, source_type: '' });
              }}
            >
              {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
            </select>
          </label>
          <label>
            <span>{t('lotteries.type')}</span>
            <select className="input" value={sourceForm.source_type} onChange={e => setSourceForm({ ...sourceForm, source_type: e.target.value })}>
              {sourceTypeOptions.map(sourceType => (
                <option value={sourceType} key={sourceType}>{discoverySourceTypeLabel(sourceType)}</option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('lotteries.interval')}</span>
            <input className="input" type="number" min="1" value={sourceForm.scan_interval_minutes} onChange={e => setSourceForm({ ...sourceForm, scan_interval_minutes: e.target.value })} />
          </label>
          <label className="url-field">
            <span>{t('lotteries.sourceValue')}</span>
            <textarea
              className="input textarea"
              value={sourceForm.source_value}
              maxLength={256}
              onChange={e => setSourceForm({ ...sourceForm, source_value: e.target.value })}
              placeholder={sourceForm.source_type === 'up' ? t('lotteries.upUidPlaceholder') : t('lotteries.sourceValuePlaceholder')}
            />
          </label>
          <div className="toolbar form-actions">
            <button className="btn-primary" type="submit" disabled={!sourceTypeOptions.length}>{t('lotteries.saveSource')}</button>
            <button className="btn-ghost" type="button" onClick={scanSources}>{t('lotteries.scanNow')}</button>
          </div>
        </form>
        {discoveryMessage && <div className="notice">{discoveryMessage}</div>}
        <div className="table-wrap compact-table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>{t('lotteries.platform')}</th><th>{t('lotteries.type')}</th><th>{t('lotteries.interval')}</th><th>{t('lotteries.lastScan')}</th><th>{t('lotteries.active')}</th></tr>
            </thead>
            <tbody>
              {sources.map(source => (
                <tr key={source.id}>
                  <td className="mono">S{source.id}</td>
                  <td>{source.platform}</td>
                  <td>{source.source_type}</td>
                  <td>{source.scan_interval_minutes}m</td>
                  <td className="small-text">{source.last_scan_at || '-'}</td>
                  <td title={source.validation_error || ''}>
                    <StatusBadge status={
                      source.active && source.effective_active === false
                        ? 'failed'
                        : (source.effective_active ?? Boolean(source.active))
                          ? 'ready'
                          : 'pending'
                    } />
                    {source.validation_error && (
                      <div className="small-text mono">{source.validation_error}</div>
                    )}
                  </td>
                </tr>
              ))}
              {!sources.length && <tr><td className="empty-cell" colSpan="6">{t('lotteries.noSources')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.activityPool')}</div>
        <div className="table-wrap">
          <table className="data-table activity-pool-table">
            <thead>
              <tr><th>ID</th><th>{t('lotteries.platform')}</th><th>{t('lotteries.url')}</th><th>{t('lotteries.rulePlan')}</th><th>{t('lotteries.status')}</th><th>{t('lotteries.score')}</th><th>{t('lotteries.realGate')}</th><th>{t('lotteries.expires')}</th><th>{t('lotteries.action')}</th></tr>
            </thead>
            <tbody>
              {lotteries.map(lottery => {
                const gate = gateByLotteryId[lottery.id];
                const repairPlan = gate?.repair_plan;
                const executionPathId = lottery.action_plan?.execution_path_id || '';
                const manualAssisted = isManualAssistedPlan(lottery.platform, executionPathId);
                const platformModeBlocker = platformDispatchBlocker(
                  lottery.platform,
                  dispatchMode,
                  executionPathId,
                );
                const targetIssue = targetTransportCompatibilityIssue(lottery.platform, lottery.raw_url);
                const transportBlocksSelectedMode = Boolean(targetIssue && dispatchMode !== 'dry_run');
                const safeAccountAvailable = hasEligibleSafeAccountFor(lottery, dispatchMode);
                const accountScopeBound = selectedAccountMatchesPlatform(lottery);
                const accountScopeReady = selectedAccountMatchesDispatch(lottery, dispatchMode);
                const accountScopeBlocked = dispatchMode !== 'dry_run' && !accountScopeReady;
                const accountCredentialKindBlocked = accountScopeBound && !accountScopeReady;
                const probeSafeAccountAvailable = hasEligibleSafeAccountFor(lottery, 'shadow_run');
                const probeAccountScopeReady = selectedAccountMatchesDispatch(lottery, 'shadow_run');
                const repairSafeAccountAvailable = hasEligibleSafeAccountFor(lottery, 'real_run');
                const repairAccountScopeReady = selectedAccountMatchesDispatch(lottery, 'real_run');
                const actionPlanReady = manualAssisted
                  ? actionPlanV2ReviewReady(lottery.action_plan, lottery.platform)
                  : actionPlanV2Ready(lottery.action_plan, lottery.platform);
                const actionPlanBlocked = dispatchMode !== 'dry_run' && !actionPlanReady;
                const gateBlocked = dispatchMode === 'real_run'
                  && (Boolean(platformModeBlocker) || !gate?.allowed || actionPlanBlocked);
                const gateCanRunNext = gateBlocked
                  && !platformModeBlocker
                  && !actionPlanBlocked
                  && ['probe', 'shadow_run', 'real_run'].includes(gate?.next_action);
                const gateNextActionMode = gate?.next_action === 'probe'
                  ? 'shadow_run'
                  : gate?.next_action;
                const gateNextAccountBlocker = gateCanRunNext
                  ? accountScopeBlockerFor(lottery, gateNextActionMode)
                  : null;
                const repairAvailable = Boolean(repairPlan?.executable);
                const repairUnavailable = Boolean(
                  repairPlan?.eligible && !repairPlan?.executable,
                );
                const repairBlocked = repairAvailable && (
                  manualAssisted
                  || Boolean(platformModeBlocker)
                  || Boolean(targetIssue)
                  || !gate?.allowed
                  || !repairSafeAccountAvailable
                  || !repairAccountScopeReady
                  || !actionPlanReady
                );
                const repairBlockReason = manualAssisted
                  ? t(manualOnlyHintKey(lottery.platform))
                  : (targetIssue ? t('lotteries.legacyHttpTargetHint')
                  : (!repairAccountScopeReady
                    ? (selectedAccountMatchesPlatform(lottery)
                      ? t('lotteries.accountCredentialKindMismatch')
                      : t('lotteries.accountScopeRequired'))
                    : (!actionPlanReady ? t('lotteries.actionPlanV2Required') : (repairBlocked ? gateTitle(gate, t) : ''))));
                const dispatchDisabled = Boolean(platformModeBlocker)
                  || transportBlocksSelectedMode
                  || !safeAccountAvailable
                  || accountCredentialKindBlocked
                  || accountScopeBlocked
                  || actionPlanBlocked
                  || Boolean(gateNextAccountBlocker)
                  || (gateBlocked && !gateCanRunNext);
                let dispatchTitle = '';
                if (platformModeBlocker) dispatchTitle = dispatchBlockerMessage(platformModeBlocker, lottery);
                else if (transportBlocksSelectedMode) dispatchTitle = t('lotteries.legacyHttpTargetHint');
                else if (!safeAccountAvailable) dispatchTitle = t('lotteries.noSafeAccount');
                else if (accountCredentialKindBlocked) dispatchTitle = t('lotteries.accountCredentialKindMismatch');
                else if (accountScopeBlocked) dispatchTitle = t('lotteries.accountScopeRequired');
                else if (actionPlanBlocked) dispatchTitle = t('lotteries.actionPlanV2Required');
                else if (gateNextAccountBlocker) dispatchTitle = dispatchBlockerMessage(gateNextAccountBlocker, lottery);
                else if (gateBlocked) dispatchTitle = gateTitle(gate, t);
                let dispatchLabel = t(`lotteries.dispatch_${dispatchMode}`);
                if (platformModeBlocker) dispatchLabel = t('lotteries.manualAssistedOnly');
                else if (transportBlocksSelectedMode) dispatchLabel = t('lotteries.compatibilityBlocked');
                else if (!safeAccountAvailable) dispatchLabel = t('lotteries.noSafeAccount');
                else if (accountCredentialKindBlocked) dispatchLabel = t('lotteries.selectAccount');
                else if (accountScopeBlocked) dispatchLabel = t('lotteries.selectAccount');
                else if (actionPlanBlocked) dispatchLabel = t('lotteries.nextActions.review_rule');
                else if (gateNextAccountBlocker) dispatchLabel = t('lotteries.selectAccount');
                else if (gateBlocked) dispatchLabel = nextActionText(gate?.next_action || 'blocked', t);
                const probeBlockReason = targetIssue
                  ? t('lotteries.legacyHttpTargetHint')
                  : (!probeSafeAccountAvailable
                    ? t('lotteries.noSafeAccount')
                    : (!probeAccountScopeReady
                      ? (selectedAccountMatchesPlatform(lottery)
                        ? t('lotteries.accountCredentialKindMismatch')
                        : t('lotteries.accountScopeRequired'))
                      : (!actionPlanReady ? t('lotteries.actionPlanV2Required') : '')));
                return (
                  <tr key={lottery.id}>
                  <td className="mono">L{lottery.id}</td>
                  <td>{lottery.platform}</td>
                  <td className="truncate-cell" title={lottery.rule_text || lottery.raw_url}>
                    {lottery.title && <div className="table-primary">{lottery.title}</div>}
                    <div className="small-text">{lottery.raw_url}</div>
                    {targetIssue && <span className="badge badge-danger">{t('lotteries.legacyHttpTarget')}</span>}
                  </td>
                  <td>
                    <RulePlanEditor
                      key={`${lottery.id}:${platformModuleStates[lottery.platform] === 'ready' ? 'ready' : 'pending'}`}
                      lottery={lottery}
                      gate={gate}
                      onSave={saveActionPlan}
                      onMarkResult={markResult}
                      platformModuleReady={
                        platformModuleStates[lottery.platform] === 'ready'
                      }
                      t={t}
                    />
                  </td>
                  <td><LotteryStatusCell lottery={lottery} gate={gate} t={t} /></td>
                  <td>{lottery.value_score}</td>
                  <td>
                    <RealGateCell
                      gate={gate}
                      platform={lottery.platform}
                      executionPathId={executionPathId}
                      targetIssue={targetIssue}
                      t={t}
                    />
                  </td>
                  <td className="small-text">{lottery.expires_at || '-'}</td>
                  <td className="action-cell">
                    <button
                      onClick={() => gateBlocked ? runGateNextAction(lottery, gate) : dispatch(lottery.id)}
                      disabled={dispatchDisabled}
                      title={dispatchTitle}
                      className={platformModeBlocker || transportBlocksSelectedMode || (dispatchMode === 'real_run' && !gateCanRunNext) ? 'btn-danger' : 'btn-primary'}
                    >
                      {dispatchLabel}
                    </button>
                    <button onClick={() => markResult(lottery.id, 'won')} className="btn-ghost">{t('lotteries.won')}</button>
                    <button onClick={() => markResult(lottery.id, 'lost')} className="btn-ghost">{t('lotteries.lost')}</button>
                    <button
                      onClick={() => probe(lottery.id)}
                      disabled={Boolean(targetIssue) || !probeSafeAccountAvailable || !probeAccountScopeReady || !actionPlanReady}
                      title={probeBlockReason}
                      className="btn-ghost"
                    >
                      {t('lotteries.probe')}
                    </button>
                    {repairAvailable && (
                      <div className="repair-action">
                        <button
                          onClick={() => repairMissingActions(lottery.id)}
                          disabled={repairBlocked}
                          title={repairBlocked ? repairBlockReason : actionSummary(repairPlan.missing_actions, t)}
                          className="btn-danger"
                        >
                          {t('lotteries.repairMissing')}
                        </button>
                        {repairBlocked && <div className="small-text muted-text">{repairBlockReason}</div>}
                      </div>
                    )}
                    {repairUnavailable && (
                      <div className="small-text warning-text">
                        {t('lotteries.repairExecutionUnavailable')}
                      </div>
                    )}
                  </td>
                  </tr>
                );
              })}
              {!lotteries.length && <tr><td className="empty-cell" colSpan="9">{t('lotteries.noActivities')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.adapterProbes')}</div>
        <div className="notice notice-warning">{t('lotteries.probeSelectorObservationOnly')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>{t('lotteries.probe')}</th><th>{t('lotteries.platform')}</th><th>{t('lotteries.account')}</th><th>{t('lotteries.activity')}</th><th>{t('lotteries.status')}</th><th>{t('lotteries.visiblePhases')}</th><th>{t('lotteries.selectorObservation')}</th><th>{t('lotteries.evidence')}</th></tr>
            </thead>
            <tbody>
              {probes.map(item => {
                const summary = probeSummary(item);
                const observationComplete = selectorObservationComplete(summary);
                return (
                  <tr key={item.id}>
                    <td className="mono">{item.probe_id?.slice(0, 8)}</td>
                    <td>{item.platform}</td>
                    <td>A{item.account_id}</td>
                    <td>{item.lottery_id ? `L${item.lottery_id}` : '-'}</td>
                    <td><StatusBadge status={item.status} /></td>
                    <td>{summary ? `${summary.ready_phase_count}/${probePhaseTotal(summary, item.platform)}` : '-'}</td>
                    <td>
                      {summary ? (
                        <span className={`badge ${observationComplete ? 'badge-ready' : 'badge-warn'}`}>
                          {t(observationComplete
                            ? 'lotteries.selectorObservationComplete'
                            : 'lotteries.selectorObservationIncomplete')}
                        </span>
                      ) : (
                        <span className="badge badge-muted">{t('lotteries.raw')}</span>
                      )}
                    </td>
                    <td className="small-text">
                      {item.screenshot_path ? (
                        <AuthenticatedAssetLink
                          className="badge badge-warn evidence-link"
                          path={`/lotteries/probes/${item.probe_id}/screenshot`}
                          onError={(assetError) => {
                            setError(assetError.message);
                            notify(assetError.message, 'error');
                          }}
                        >
                          {t('lotteries.openProbe')}
                        </AuthenticatedAssetLink>
                      ) : (item.error_message || '-')}
                    </td>
                  </tr>
                );
              })}
              {!probes.length && <tr><td className="empty-cell" colSpan="8">{t('lotteries.noAdapterProbes')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.executionRuns')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>{t('lotteries.task')}</th><th>{t('lotteries.account')}</th><th>{t('lotteries.activity')}</th><th>{t('lotteries.status')}</th><th>{t('lotteries.mode')}</th><th>{t('lotteries.evidence')}</th></tr>
            </thead>
            <tbody>
              {runs.map(run => (
                <tr key={run.id}>
                  <td className="mono">{run.task_id?.slice(0, 8)}</td>
                  <td>A{run.account_id}</td>
                  <td>L{run.lottery_id}</td>
                  <td><StatusBadge status={run.status} /></td>
                  <td>{modeLabel(run, t)}</td>
                  <td className="small-text">
                    {run.screenshot_path ? (
                      <AuthenticatedAssetLink
                        className="badge badge-warn evidence-link"
                        path={`/lotteries/tasks/runs/${run.task_id}/screenshot`}
                        onError={(assetError) => {
                          setError(assetError.message);
                          notify(assetError.message, 'error');
                        }}
                      >
                        {t('lotteries.openEvidence')}
                      </AuthenticatedAssetLink>
                    ) : (run.error_message || '-')}
                  </td>
                </tr>
              ))}
              {!runs.length && <tr><td className="empty-cell" colSpan="6">{t('lotteries.noRuns')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function safeJson(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function modeLabel(run, t) {
  const mode = run.task_mode || (run.dry_run ? 'dry_run' : 'real_run');
  const label = t(`lotteries.${mode}`);
  return label === `lotteries.${mode}` ? mode : label;
}

function modeText(mode, t) {
  const label = t(`lotteries.${mode}`);
  return label === `lotteries.${mode}` ? mode : label;
}

function reasonText(code, t) {
  const label = t(`lotteries.strategyReasons.${code}`);
  if (label !== `lotteries.strategyReasons.${code}`) return label;
  return formatText(t('lotteries.unknownBlockerCode'), { code: String(code || '') });
}

function nextActionText(code, t) {
  const normalized = String(code || 'blocked');
  const key = `lotteries.nextActions.${normalized}`;
  const label = t(key);
  if (label !== key) return label;
  return formatText(t('lotteries.unknownNextAction'), { code: normalized });
}

function gateBlockerText(code, t) {
  if (String(code || '').startsWith('global=')) {
    return t('lotteries.globalCircuitBreakerReason');
  }
  const label = t(`lotteries.realGateBlockers.${code}`);
  if (label !== `lotteries.realGateBlockers.${code}`) return label;
  const legacyKey = `dashboard.blockersMap.${code}`;
  const legacyLabel = t(legacyKey);
  if (legacyLabel !== legacyKey) return legacyLabel;
  return actionPlanBlockerText(code, t);
}

function actionPlanBlockerText(code, t) {
  const label = t(`lotteries.actionPlanBlockers.${code}`);
  if (label !== `lotteries.actionPlanBlockers.${code}`) return label;
  const value = String(code || '');
  return /^[a-z0-9_]+$/i.test(value)
    ? formatText(t('lotteries.unknownBlockerCode'), { code: value })
    : value;
}

function ruleSaveBlockerText(code, t) {
  const value = String(code || '');
  if (value.startsWith('payload:')) {
    return actionPlanBlockerText(value.slice('payload:'.length), t);
  }
  if (value.startsWith('requirement:')) {
    const requirement = value.slice('requirement:'.length);
    const label = t(`lotteries.unsupportedActions.${requirement}`);
    return label === `lotteries.unsupportedActions.${requirement}` ? requirement : label;
  }
  const key = `lotteries.ruleSaveBlockers.${value}`;
  const translated = t(key);
  return translated === key ? value : translated;
}

function shortIdentity(value) {
  const text = String(value || '');
  return text.length > 12 ? `${text.slice(0, 12)}…` : (text || '-');
}

function gateTitle(gate, t) {
  if (!gate?.blockers?.length) return t('lotteries.realAdapterMissing');
  const reason = gate.blockers.map(code => gateBlockerText(code, t)).join(' / ');
  const risk = riskCooldownText(gate, t);
  return risk ? `${reason} / ${risk}` : reason;
}

function sameActionSet(left = [], right = []) {
  if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every(action => rightSet.has(action));
}

function actionSummary(actions = [], t) {
  if (!Array.isArray(actions) || !actions.length) return t('lotteries.noSavedRuleActions');
  return actions.map(action => t(`lotteries.actions.${action}`)).join(' / ');
}

function targetValidationErrorText(value, t) {
  const code = targetOperationErrorCode(value) || targetValidationErrorCode(value);
  if (!code) return value;
  const key = `lotteries.targetErrors.${code}`;
  const translated = t(key);
  if (translated !== key) return translated;
  const importKey = `lotteries.targetImportErrors.${code}`;
  const importTranslated = t(importKey);
  return importTranslated === importKey ? value : importTranslated;
}

async function normalizeTargetImport(targetImport) {
  return normalizePlatformTargetImport(targetImport?.platform, targetImport?.content);
}

function discoverySourceTypeLabel(sourceType) {
  if (sourceType === 'keyword') return 'Keyword';
  if (sourceType === 'up') return 'Creator';
  return 'URL list';
}

function targetImportSanitizedMessageKey(platform) {
  const normalized = String(platform || '').trim().toLowerCase();
  if (normalized === 'xiaohongshu') return 'lotteries.xiaohongshuExportSanitized';
  if (normalized === 'douyin') return 'lotteries.douyinExportSanitized';
  if (normalized === 'weibo') return 'lotteries.weiboExportSanitized';
  return 'lotteries.targetImportSanitized';
}

function manualOnlyHintKey(platform) {
  const normalized = String(platform || '').trim().toLowerCase();
  if (normalized === 'douyin') return 'lotteries.douyinManualOnlyHint';
  if (normalized === 'weibo') return 'lotteries.weiboManualOnlyHint';
  return 'lotteries.xiaohongshuManualOnlyHint';
}

function manualMediaRequirementKey(platform) {
  const normalized = String(platform || '').trim().toLowerCase();
  if (normalized === 'douyin') return 'lotteries.douyinMediaRequirementManual';
  if (normalized === 'weibo') return 'lotteries.weiboMediaRequirementManual';
  return 'lotteries.xiaohongshuMediaRequirementManual';
}

function targetImportTimeout(normalizedImport) {
  if (!normalizedImport?.targetCount) return undefined;
  const configuredTimeout = Number(import.meta.env.VITE_API_TIMEOUT_MS || 15_000);
  const rowBudget = normalizedImport.targetCount * 40;
  const shortLinkBudget = normalizedImport.shortLinkCount ? 42_000 : 0;
  return Math.max(configuredTimeout, Math.min(58_000, 15_000 + rowBudget + shortLinkBudget));
}

function targetImportErrorText(error, t) {
  const code = targetOperationErrorCode(error);
  const key = `lotteries.targetImportErrors.${code}`;
  const translated = t(key);
  return translated === key ? (error?.message || code || t('lotteries.targetImportErrors.unknown')) : translated;
}

function displayTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function riskCooldownText(gate, t) {
  const risk = gate?.account_risk;
  if (!risk?.has_recent_risk) return '';
  const controllingEvent = risk.controlling_event || risk.latest_event;
  const account = controllingEvent?.account_id ? `A${controllingEvent.account_id}` : t('lotteries.account');
  return formatText(t('lotteries.riskCooldownUntil'), {
    account,
    time: displayTime(risk.cooldown_until),
  });
}

function LotteryStatusCell({ lottery, gate, t }) {
  const status = String(lottery?.status || '').trim().toLowerCase();
  const directReason = String(
    lottery?.blocked_reason || lottery?.status_reason || lottery?.error_message || '',
  ).trim();
  const directBlockers = Array.isArray(lottery?.blockers) ? lottery.blockers : [];
  const gateBlockers = Array.isArray(gate?.blockers) ? gate.blockers : [];
  const blockers = [...new Set([...directBlockers, ...gateBlockers].filter(Boolean))];
  const showReason = status === 'blocked' || Boolean(directReason);
  return (
    <div className="gate-cell">
      <StatusBadge status={lottery?.status} />
      {showReason && (
        <div className="status-reason-panel">
          <strong className="small-text">{t('lotteries.statusBlockReason')}</strong>
          {directReason && (
            <div className="small-text warning-text">{gateBlockerText(directReason, t)}</div>
          )}
          {!!blockers.length && (
            <div className="blocker-list compact-blockers">
              {blockers.map(code => (
                <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
              ))}
            </div>
          )}
          {!directReason && !blockers.length && (
            <div className="small-text warning-text">{t('lotteries.statusBlockReasonUnavailable')}</div>
          )}
        </div>
      )}
    </div>
  );
}

function RealGateCell({ gate, platform, executionPathId, targetIssue, t }) {
  if (targetIssue) {
    return (
      <div className="gate-cell">
        <span className="badge badge-danger">{t('lotteries.compatibilityBlocked')}</span>
        <div className="small-text warning-text">{t('lotteries.legacyHttpTargetHint')}</div>
        {!!gate?.blockers?.length && (
          <div className="gate-block-reasons">
            <strong className="small-text">{t('lotteries.gateBlockReasons')}</strong>
            <div className="blocker-list compact-blockers">
              {gate.blockers.map(code => (
                <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }
  if (isManualAssistedPlatform(platform, executionPathId)) {
    const shadowObservation = manualShadowObservation(gate);
    return (
      <div className="gate-cell">
        <span className="badge badge-warn">{t('lotteries.manualAssistedOnly')}</span>
        <div className="small-text warning-text">{t(manualOnlyHintKey(platform))}</div>
        <div className="capability-row">
          <span>{t('lotteries.shadowEvidence')}</span>
          <span className={`badge ${shadowObservation.complete ? 'badge-ready' : 'badge-muted'}`}>
            {shadowObservation.complete
              ? t('lotteries.shadowEvidenceReady')
              : t('lotteries.shadowEvidenceMissing')}
          </span>
        </div>
        {!!gate?.blockers?.length && (
          <div className="gate-block-reasons">
            <strong className="small-text">{t('lotteries.gateBlockReasons')}</strong>
            <div className="blocker-list compact-blockers">
              {gate.blockers.map(code => (
                <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
              ))}
            </div>
          </div>
        )}
        {!!shadowObservation.taskId && (
          <div className="small-text mono">Shadow {shortIdentity(shadowObservation.taskId)}</div>
        )}
      </div>
    );
  }
  if (!gate) return <span className="badge badge-muted">{t('lotteries.gateUnknown')}</span>;
  const repairPlan = gate.repair_plan;
  const riskText = riskCooldownText(gate, t);
  return (
    <div className="gate-cell">
      <span className={`badge ${gate.allowed ? 'badge-ready' : 'badge-warn'}`}>
        {gate.allowed ? t('lotteries.gateReady') : t('lotteries.gateBlocked')}
      </span>
      <div className="small-text muted-text">
        {formatText(t('lotteries.realRunAccountPool'), {
          ready: gate.safe_accounts ?? 0,
          runnable: gate.risk_clear_accounts ?? gate.safe_accounts ?? 0,
        })}
      </div>
      {!!gate.blockers?.length && (
        <div className="gate-block-reasons">
          <strong className="small-text">{t('lotteries.gateBlockReasons')}</strong>
          <div className="blocker-list compact-blockers">
            {gate.blockers.map(code => (
              <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
            ))}
          </div>
        </div>
      )}
      {riskText && <div className="small-text warning-text">{riskText}</div>}
      <ExecutionEvidenceDetails gate={gate} t={t} />
      {!!(repairPlan?.completed_actions?.length || repairPlan?.missing_actions?.length) && (
        <div className="repair-plan-summary">
          {!!repairPlan.completed_actions?.length && (
            <div className="small-text">{formatText(t('lotteries.repairCompletedActions'), { actions: actionSummary(repairPlan.completed_actions, t) })}</div>
          )}
          {!!repairPlan.missing_actions?.length && (
            <div className="small-text">{formatText(t('lotteries.repairMissingActions'), { actions: actionSummary(repairPlan.missing_actions, t) })}</div>
          )}
        </div>
      )}
      {repairPlan?.eligible && !repairPlan?.executable && (
        <div className="small-text warning-text">
          {t('lotteries.repairExecutionUnavailable')}
        </div>
      )}
      <ActionLedgerSummary ledger={gate.action_ledger} repairPlan={repairPlan} t={t} />
    </div>
  );
}

function ExecutionEvidenceDetails({ gate, t }) {
  const evidence = executionEvidencePresentation(gate);
  const missingReasons = evidence.reasons.length
    ? evidence.reasons
    : (evidence.bound ? [] : ['execution_evidence_required']);
  return (
    <div className="action-ledger-summary">
      <div className="capability-row">
        <span>{t('lotteries.executionEvidenceBinding')}</span>
        <span className={`badge ${evidence.bound && evidence.status === 'verified' ? 'badge-ready' : 'badge-warn'}`}>
          {evidenceStatusText(evidence.status, t)}
        </span>
      </div>
      {!!evidence.id && <div className="small-text mono">ID {shortIdentity(evidence.id)}</div>}
      {!!evidence.executionPathId && (
        <div className="small-text mono">{t('lotteries.executionPath')}: {evidence.executionPathId}</div>
      )}
      {!!evidence.accountId && (
        <div className="small-text mono">
          {t('lotteries.account')}: A{evidence.accountId} / {evidence.accountScopeMatchesPlatform
            ? t('lotteries.accountScopeMatched')
            : t('lotteries.accountScopeMismatch')}
        </div>
      )}
      {!!evidence.probeId && <div className="small-text mono">Probe {shortIdentity(evidence.probeId)}</div>}
      {!!evidence.shadowTaskId && <div className="small-text mono">Shadow {shortIdentity(evidence.shadowTaskId)}</div>}
      {!!evidence.verifiedAt && (
        <div className="small-text muted-text">{t('lotteries.evidenceVerifiedAt')}: {displayTime(evidence.verifiedAt)}</div>
      )}
      {!!evidence.expiresAt && (
        <div className="small-text muted-text">{t('lotteries.evidenceExpiresAt')}: {displayTime(evidence.expiresAt)}</div>
      )}
      {!evidence.bound && !!missingReasons.length && (
        <div className="gate-block-reasons">
          <strong className="small-text">{t('lotteries.evidenceMissingItems')}</strong>
          <div className="blocker-list compact-blockers">
            {missingReasons.map(code => (
              <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function evidenceStatusText(status, t) {
  const normalized = String(status || 'unbound').trim().toLowerCase();
  const key = `lotteries.evidenceStatuses.${normalized}`;
  const translated = t(key);
  return translated === key ? normalized : translated;
}

function ActionLedgerSummary({ ledger = [], repairPlan, t }) {
  const rows = Array.isArray(ledger) ? ledger.slice(0, 4) : [];
  const shouldShow = rows.length || repairPlan?.completed_actions?.length || repairPlan?.missing_actions?.length;
  if (!shouldShow) return null;
  return (
    <div className="action-ledger-summary">
      <div className="small-text ledger-disclaimer">{t('lotteries.actionLedgerHint')}</div>
      {rows.length ? rows.map(row => (
        <div className="action-ledger-row" key={`${row.task_id}-${row.action}-${row.id || row.created_at}`}>
          <span className={`badge ${ledgerOutcomeClass(row)}`}>{ledgerActionLabel(row, t)}</span>
          <span className="small-text">{ledgerOutcomeLabel(row, t)}</span>
          <span className="small-text muted-text">{displayTime(row.created_at)}</span>
        </div>
      )) : (
        <div className="small-text muted-text">{t('lotteries.actionLedgerEmpty')}</div>
      )}
    </div>
  );
}

function ledgerActionLabel(row, t) {
  const phase = row.phase || API_ACTION_PHASES[row.action];
  if (phase) return actionSummary([phase], t);
  return row.action || '-';
}

function ledgerOutcomeLabel(row, t) {
  const outcome = row.outcome || 'missing';
  const status = row.ok ? t('lotteries.ledgerStatuses.completed') : t(`lotteries.ledgerStatuses.${outcome}`);
  const label = status === `lotteries.ledgerStatuses.${outcome}` ? outcome : status;
  return row.code === null || row.code === undefined ? label : `${label} (${row.code})`;
}

function ledgerOutcomeClass(row) {
  if (row.ok) return 'badge-ready';
  if (['risk', 'auth', 'captcha', 'fatal'].includes(row.outcome)) return 'badge-danger';
  if (['limit', 'retry'].includes(row.outcome)) return 'badge-warn';
  return 'badge-muted';
}

function RulePlanEditor({
  lottery,
  gate,
  onSave,
  onMarkResult,
  platformModuleReady,
  t,
}) {
  const { notify } = useUi();
  const plan = lottery.action_plan || {};
  const initialFollowTarget = automaticFollowTarget(lottery, plan);
  const fixedManualActions = isFixedManualActionPlatform(lottery.platform);
  const availableActions = lotteryActionsForPlatform(lottery.platform);
  const availableExecutionPaths = platformExecutionPaths(lottery.platform);
  const savedActions = Array.isArray(plan.required_actions) ? plan.required_actions : [];
  const savedExecutionPathId = platformExecutionPathId(
    lottery.platform,
    plan.execution_path_id,
  );
  const savedManualAssisted = isManualAssistedPlan(lottery.platform, plan);
  const initialActions = fixedManualActions ? availableActions : savedActions;
  const savedPayloads = actionPayloadDraft(
    savedActions,
    plan.action_payloads,
    null,
    lottery.platform,
  );
  const initialPayloads = actionPayloadDraft(
    initialActions,
    plan.action_payloads,
    plan.content_requirements,
    lottery.platform,
    {
      followTargetFallback: initialFollowTarget,
      rulePlan: plan,
      prepareForEditing: true,
    },
  );
  const planSignature = JSON.stringify({
    actions: savedActions,
    payloads: savedPayloads,
    contentRequirements: plan.content_requirements,
    sourceContentRequirements: plan.source_content_requirements,
    friendMentionRequirements: plan.friend_mention_requirements,
    executionPathId: savedExecutionPathId,
    version: plan.version,
    hash: plan.plan_hash,
  });
  const [actions, setActions] = useState(initialActions);
  const [payloads, setPayloads] = useState(initialPayloads);
  const [ruleText, setRuleText] = useState(authoritativeRuleText(lottery));
  const [executionPathId, setExecutionPathId] = useState(savedExecutionPathId);
  const [ruleCompleteConfirmed, setRuleCompleteConfirmed] = useState(false);
  const [reviewedConfirmed, setReviewedConfirmed] = useState(false);
  const [suggestion, setSuggestion] = useState(null);
  const [suggesting, setSuggesting] = useState(false);
  const [hydration, setHydration] = useState(null);
  const [hydrating, setHydrating] = useState(false);
  const [hydrationError, setHydrationError] = useState('');
  const [saving, setSaving] = useState(false);
  const hydrationAttemptedRef = useRef(false);
  const operatorEditedRef = useRef(false);
  const effectiveLottery = hydration ? { ...lottery, ...hydration } : lottery;
  const targetIdentity = lotteryTargetIdentity(effectiveLottery);
  const ruleSnapshotParts = visibleRuleSnapshotParts(effectiveLottery);
  const suggestionActions = Array.isArray(suggestion?.required_actions) ? suggestion.required_actions : [];
  const draftPayloads = actionPayloadDraft(actions, payloads, null, lottery.platform);
  const semanticSource = suggestion || plan;
  const friendMentionRequirements = (
    semanticSource.friend_mention_requirements
    && typeof semanticSource.friend_mention_requirements === 'object'
  ) ? semanticSource.friend_mention_requirements : {};
  const manualAssisted = isManualAssistedPlatform(lottery.platform, executionPathId);
  const unresolvedRequirements = unresolvedRuleRequirements(semanticSource, draftPayloads);
  const payloadErrors = exactActionPayloadErrors(actions, draftPayloads, lottery.platform);
  const executionPathValid = platformSupportsExecutionPath(lottery.platform, executionPathId);
  const persistedPlanBlockers = savedManualAssisted
    ? actionPlanV2ReviewBlockers(plan, lottery.platform)
    : [
      ...actionPlanV2Blockers(plan, lottery.platform),
      ...(Array.isArray(plan.payload_validation_errors) ? plan.payload_validation_errors : []),
      ...(Array.isArray(plan.capability_blockers) ? plan.capability_blockers : []),
    ];
  const planBlockers = [...new Set([
    ...persistedPlanBlockers,
  ])];
  const planReady = savedManualAssisted
    ? actionPlanV2ReviewReady(plan, lottery.platform)
    : actionPlanV2Ready(plan, lottery.platform);
  const draftChanged = !sameActionSet(actions, savedActions)
    || JSON.stringify(draftPayloads) !== JSON.stringify(savedPayloads)
    || executionPathId !== savedExecutionPathId;
  const missingSuggestedActions = suggestionActions.filter(action => !savedActions.includes(action));
  const sourceRuleLocked = Boolean(authoritativeRuleText(effectiveLottery));
  const discoveryManagedSource = sourceRuleCorrectionPath(lottery.platform, lottery.source_type) === 'discovery_refresh';
  const sourceRuleHelpId = `lottery-${lottery.id}-source-rule-help`;
  const mediaRequired = actionPlanHasMediaRequirement({ action_payloads: draftPayloads });
  const mediaRequirementNotice = t(manualAssisted
    ? manualMediaRequirementKey(lottery.platform)
    : 'lotteries.mediaRuleStoredButUnsupported');
  const requiredActionSetComplete = !fixedManualActions
    || (actions.length === availableActions.length && sameActionSet(actions, availableActions));
  const saveBlockers = ruleEditorSaveBlockers({
    actions,
    ruleText,
    executionPathId,
    executionPathValid,
    ruleCompleteConfirmed,
    reviewedConfirmed,
    requiredActionSetComplete,
    unresolvedRequirements,
    payloadErrors,
  });
  if (hydrating) saveBlockers.push('rule_hydration_pending');
  const saveDisabled = saveBlockers.length > 0;
  const saveBlockerLabels = saveBlockers.map(code => ruleSaveBlockerText(code, t));

  useEffect(() => {
    const persistedActions = Array.isArray(plan.required_actions) ? plan.required_actions : [];
    const nextActions = isFixedManualActionPlatform(lottery.platform)
      ? lotteryActionsForPlatform(lottery.platform)
      : persistedActions;
    const followTargetFallback = automaticFollowTarget(lottery, plan);
    setActions(nextActions);
    setPayloads(actionPayloadDraft(
      nextActions,
      plan.action_payloads,
      plan.content_requirements,
      lottery.platform,
      {
        followTargetFallback,
        rulePlan: plan,
        prepareForEditing: true,
      },
    ));
    setRuleText(authoritativeRuleText(lottery));
    setExecutionPathId(platformExecutionPathId(lottery.platform, plan.execution_path_id));
    setRuleCompleteConfirmed(false);
    setReviewedConfirmed(false);
    setSuggestion(null);
    setHydration(null);
    setHydrationError('');
    setSaving(false);
    hydrationAttemptedRef.current = false;
    operatorEditedRef.current = false;
  }, [
    lottery.id,
    lottery.platform,
    lottery.rule_text,
    planSignature,
    platformModuleReady,
  ]);

  const toggle = action => {
    operatorEditedRef.current = true;
    setActions(current => availableActions.filter(candidate => (
      candidate === action ? !current.includes(candidate) : current.includes(candidate)
    )));
    setPayloads(current => ({
      ...current,
      [action]: current[action] || (
        ['commented', 'reposted'].includes(action)
          ? { text: '' }
          : (action === 'followed' ? { target_handle: '' } : {})
      ),
    }));
    setRuleCompleteConfirmed(false);
    setReviewedConfirmed(false);
  };

  const updateTextPayload = (action, field, value) => {
    operatorEditedRef.current = true;
    setPayloads(current => ({
      ...current,
      [action]: {
        ...(current[action] || { text: '' }),
        [field]: ['topic_tags', 'mentions', 'media_refs'].includes(field)
          ? parseMetadataLines(value)
          : value,
      },
    }));
    setRuleCompleteConfirmed(false);
    setReviewedConfirmed(false);
  };

  const updateFollowTarget = value => {
    operatorEditedRef.current = true;
    setPayloads(current => ({
      ...current,
      followed: { target_handle: value },
    }));
    setRuleCompleteConfirmed(false);
    setReviewedConfirmed(false);
  };

  const requestSuggestion = async ({
    sourceText = ruleText,
    automatic = false,
    identitySource = effectiveLottery,
  } = {}) => {
    const normalizedRuleText = String(sourceText || '').trim();
    if (!normalizedRuleText) return;
    setSuggesting(true);
    try {
      const response = await fetchJSON(`/lotteries/${lottery.id}/action-plan/suggest?rule_text=${encodeURIComponent(normalizedRuleText)}`);
      const suggested = response.suggested_action_plan || {};
      const suggestedActions = Array.isArray(suggested.required_actions)
        ? suggested.required_actions
        : [];
      setSuggestion(suggested);
      if (!automatic || !operatorEditedRef.current) {
        const suggestedSelection = availableActions.filter(action => suggestedActions.includes(action));
        const nextActions = fixedManualActions
          ? availableActions
          : (automatic && actions.length ? actions : suggestedSelection);
        const followTargetFallback = automaticFollowTarget(identitySource, suggested);
        setActions(nextActions);
        setPayloads(current => actionPayloadDraft(
          nextActions,
          current,
          suggested.content_requirements,
          lottery.platform,
          {
            followTargetFallback,
            rulePlan: suggested,
            prepareForEditing: true,
          },
        ));
        setRuleCompleteConfirmed(false);
        setReviewedConfirmed(false);
      }
    } catch (err) {
      if (!automatic) notify(err.message, 'error');
    } finally {
      setSuggesting(false);
    }
  };

  const hydrateRule = async ({ force = false } = {}) => {
    if (hydrating || (hydrationAttemptedRef.current && !force)) return;
    hydrationAttemptedRef.current = true;
    setHydrating(true);
    setHydrationError('');
    try {
      const response = await fetchJSON(`/lotteries/${lottery.id}/rule-hydration`);
      const hydratedLottery = { ...lottery, ...response };
      const hydratedRuleText = authoritativeRuleText(hydratedLottery);
      setHydration(response);
      setRuleText(hydratedRuleText);
      setRuleCompleteConfirmed(false);
      setReviewedConfirmed(false);
      if (hydratedRuleText) {
        await requestSuggestion({
          sourceText: hydratedRuleText,
          automatic: true,
          identitySource: hydratedLottery,
        });
      }
    } catch (err) {
      setHydrationError(err.message || t('lotteries.ruleHydrationFailed'));
      if (force) notify(err.message || t('lotteries.ruleHydrationFailed'), 'error');
      if (ruleText.trim() && !suggestion) {
        await requestSuggestion({ automatic: true });
      }
    } finally {
      setHydrating(false);
    }
  };

  const handleSave = async () => {
    if (saveDisabled) {
      notify(formatText(t('lotteries.ruleSaveBlocked'), {
        reasons: saveBlockerLabels.join('；'),
      }), 'warning');
      return;
    }
    setSaving(true);
    try {
      await onSave(lottery, {
        requiredActions: actions,
        actionPayloads: draftPayloads,
        ruleText,
        ruleCompleteConfirmed,
        reviewed: reviewedConfirmed,
        executionPathId,
        platform: lottery.platform,
      });
    } finally {
      setSaving(false);
    }
  };

  if (!platformModuleReady) {
    return (
      <details className="rule-plan-editor">
        <summary>
          <span className="badge badge-warn">
            {t('lotteries.platformModuleLoading')}
          </span>
          <span className="small-text">{actionSummary(savedActions, t)}</span>
        </summary>
        <div className="notice notice-warning">
          {t('lotteries.platformModuleLoading')}
        </div>
      </details>
    );
  }

  return (
    <details
      className="rule-plan-editor"
      onToggle={event => {
        if (event.currentTarget.open) hydrateRule();
      }}
    >
      <summary>
        <span className={`badge ${planReady && !draftChanged ? 'badge-ready' : 'badge-warn'}`}>
          {planReady
            ? t(savedManualAssisted ? 'lotteries.manualRuleReady' : 'lotteries.ruleReady')
            : t('lotteries.ruleNeedsReview')}
        </span>
        <span className="small-text">{actionSummary(savedActions, t)}</span>
        {draftChanged && <span className="badge badge-warn">{t('lotteries.ruleDraftUnsavedBadge')}</span>}
      </summary>
      <div className="rule-plan-body">
        <div className="rule-plan-snapshots">
          <div className="rule-plan-snapshot">
            <span>{t(savedManualAssisted ? 'lotteries.manualSavedPlan' : 'lotteries.savedExecutionPlan')}</span>
            <strong>{actionSummary(savedActions, t)}</strong>
          </div>
          <div className={`rule-plan-snapshot ${draftChanged ? 'is-dirty' : ''}`}>
            <span>{t(manualAssisted ? 'lotteries.manualDraftPlan' : 'lotteries.draftExecutionPlan')}</span>
            <strong>{actionSummary(actions, t)}</strong>
          </div>
          {!!suggestionActions.length && (
            <div className={`rule-plan-snapshot ${missingSuggestedActions.length ? 'is-dirty' : ''}`}>
              <span>{t('lotteries.suggestedExecutionPlan')}</span>
              <strong>{actionSummary(suggestionActions, t)}</strong>
            </div>
          )}
        </div>

        <div className="small-text mono">
          v{plan.version || 1} / {plan.execution_path_id || '-'} / snapshot {plan.rule_snapshot_id || '-'}
        </div>
        <div className="small-text mono" title={plan.rule_hash || ''}>
          rule {shortIdentity(plan.rule_hash)} / plan {shortIdentity(plan.plan_hash)}
        </div>
        {!!planBlockers.length && (
          <div className="blocker-list compact-blockers" role="alert">
            {planBlockers.map(code => (
              <span className="badge badge-warn" key={code}>{actionPlanBlockerText(code, t)}</span>
            ))}
          </div>
        )}
        {actionPlanHasMediaRequirement(plan) && (
          <div className="notice notice-warning">{mediaRequirementNotice}</div>
        )}
        <SavedExactPayloads payloads={savedPayloads} t={t} />
        {savedManualAssisted && (
          <ManualAssistedChecklist
            plan={plan}
            gate={gate}
            platform={lottery.platform}
            lotteryId={lottery.id}
            lotteryStatus={lottery.status}
            onMarkResult={onMarkResult}
            t={t}
          />
        )}

        {(availableExecutionPaths.length > 1 || !executionPathValid) && (
          <label>
            <span>{t('lotteries.executionPath')}</span>
            <select
              className="input"
              value={executionPathId}
              onChange={event => {
                operatorEditedRef.current = true;
                setExecutionPathId(event.target.value);
                setRuleCompleteConfirmed(false);
                setReviewedConfirmed(false);
              }}
            >
              {!executionPathValid && (
                <option value={executionPathId} disabled>{executionPathId}</option>
              )}
              {availableExecutionPaths.map((pathId) => {
                const presentation = platformExecutionPathPresentation(lottery.platform, pathId);
                return (
                  <option value={pathId} key={pathId}>
                    {presentation?.labelKey ? t(presentation.labelKey) : pathId}
                  </option>
                );
              })}
            </select>
            {platformExecutionPathPresentation(lottery.platform, executionPathId)?.hintKey && (
              <div className="small-text muted-text">
                {t(platformExecutionPathPresentation(lottery.platform, executionPathId).hintKey)}
              </div>
            )}
          </label>
        )}

        {draftChanged && (
          <div className="notice notice-warning">
            {t(manualAssisted ? 'lotteries.manualRuleDraftNotSaved' : 'lotteries.ruleDraftNotSaved')}
          </div>
        )}
        {!!missingSuggestedActions.length && (
          <div className="notice notice-warning">
            {formatText(t('lotteries.suggestedActionsMissingSaved'), { actions: actionSummary(missingSuggestedActions, t) })}
          </div>
        )}
        <div className="rule-source-toolbar">
          <strong>{t('lotteries.fullRuleText')}</strong>
          <button
            className="btn-ghost"
            type="button"
            disabled={hydrating}
            onClick={() => hydrateRule({ force: true })}
          >
            {hydrating ? t('lotteries.ruleHydrating') : t('lotteries.refreshRuleHydration')}
          </button>
        </div>
        <textarea
          className="input textarea rule-source-text"
          value={ruleText}
          onChange={event => {
            operatorEditedRef.current = true;
            setRuleText(event.target.value);
            setRuleCompleteConfirmed(false);
            setReviewedConfirmed(false);
            setSuggestion(null);
          }}
          readOnly={sourceRuleLocked}
          aria-readonly={sourceRuleLocked}
          aria-describedby={sourceRuleLocked ? sourceRuleHelpId : undefined}
          placeholder={hydrating ? t('lotteries.ruleHydrating') : t('lotteries.ruleTextPlaceholder')}
        />
        {hydrationError && (
          <div className="notice notice-warning small-text" role="alert">
            {t('lotteries.ruleHydrationFailed')}: {hydrationError}
          </div>
        )}
        {!!hydration?.warnings?.length && (
          <div className="notice notice-warning small-text" role="note">
            {t('lotteries.ruleHydrationWarnings')}: {hydration.warnings.join(' / ')}
          </div>
        )}
        {!!ruleSnapshotParts.length && (
          <details className="rule-source-snapshots">
            <summary>{t('lotteries.ruleSnapshotDetails')}</summary>
            <div className="rule-source-snapshot-list">
              {ruleSnapshotParts.map(part => (
                <div className="rule-source-snapshot" key={part.key}>
                  <div className="capability-row">
                    <strong>{t(`lotteries.ruleSnapshotParts.${part.key}`)}</strong>
                    <span className={`badge ${part.trusted ? 'badge-ready' : 'badge-warn'}`}>
                      {t(part.trusted ? 'lotteries.snapshotTrusted' : 'lotteries.snapshotUntrusted')}
                    </span>
                  </div>
                  <div className="small-text rule-source-snapshot-text">{part.value}</div>
                </div>
              ))}
            </div>
          </details>
        )}
        {sourceRuleLocked && (
          <div className="notice notice-warning small-text" id={sourceRuleHelpId} role="note">
            <div>{t('lotteries.sourceRuleReadOnly')}</div>
            <div>{t(discoveryManagedSource
              ? 'lotteries.sourceRuleDiscoveryCorrectionHint'
              : 'lotteries.sourceRuleCorrectionUnavailable')}</div>
          </div>
        )}
        <button className="btn-ghost" type="button" disabled={!ruleText.trim() || suggesting} onClick={() => requestSuggestion()}>
          {suggesting ? t('lotteries.suggesting') : t('lotteries.suggestRule')}
        </button>
        {suggestion && (
          <div className="rule-suggestion small-text">
            <div>{formatText(t('lotteries.suggestionConfidence'), { value: Math.round((suggestion.confidence || 0) * 100) })}</div>
            {!!suggestion.ambiguity_patterns?.length && (
              <div className="badge badge-warn">{t('lotteries.suggestionAmbiguous')}</div>
            )}
            {suggestion.unsupported_actions?.map(action => (
              <div className="badge badge-warn" key={action}>
                {t('lotteries.requirementPrefix')}{t(`lotteries.unsupportedActions.${action}`)}
              </div>
            ))}
          </div>
        )}
        <div className="rule-action-grid">
          {availableActions.map(action => (
            <label key={action}>
              <input
                type="checkbox"
                checked={actions.includes(action)}
                disabled={fixedManualActions}
                onChange={() => toggle(action)}
              />
              <span>{t(`lotteries.actions.${action}`)}</span>
            </label>
          ))}
        </div>
        {fixedManualActions && (
          <div className="small-text muted-text">{t('lotteries.xiaohongshuFourActionsFixed')}</div>
        )}

        {actions.includes('followed') && (
          <fieldset className="exact-payload-editor">
            <legend>{t('lotteries.followTarget')}</legend>
            <div className="target-identity-card">
              <div className="capability-row">
                <strong>{t('lotteries.targetAuthorIdentity')}</strong>
                <span className={`badge ${targetIdentity.verified ? 'badge-ready' : 'badge-warn'}`}>
                  {t(targetIdentity.verified
                    ? 'lotteries.targetIdentityVerified'
                    : 'lotteries.targetIdentityUnverified')}
                </span>
              </div>
              {targetIdentity.displayName && (
                <div className="small-text">
                  {t('lotteries.targetAuthorNickname')}: {targetIdentity.displayName}
                </div>
              )}
              {targetIdentity.uid && (
                <div className="small-text mono">
                  {t('lotteries.targetAuthorUid')}: {targetIdentity.uid}
                </div>
              )}
              {!targetIdentity.displayName && !targetIdentity.uid && (
                <div className="small-text warning-text">{t('lotteries.targetIdentityMissing')}</div>
              )}
            </div>
            <label>
              <span>{t('lotteries.followTarget')}</span>
              <input
                className="input"
                type="text"
                value={draftPayloads.followed?.target_handle || ''}
                onChange={event => updateFollowTarget(event.target.value)}
                placeholder={t('lotteries.followTargetPlaceholder')}
                autoComplete="off"
              />
            </label>
            <div className="small-text muted-text">
              {t(targetIdentity.displayName
                ? 'lotteries.followTargetAutoHint'
                : 'lotteries.followTargetHint')}
            </div>
          </fieldset>
        )}

        {actions.filter(action => ['commented', 'reposted'].includes(action)).map(action => {
          const payload = draftPayloads[action] || { text: '' };
          const topicTags = actionRequirementValues(semanticSource, action, 'topic_tags');
          const sourceMentions = actionRequirementValues(semanticSource, action, 'mentions');
          const friendRequirement = friendMentionRequirements[action];
          const friendMentions = (Array.isArray(payload.mentions) ? payload.mentions : [])
            .filter(mention => !sourceMentions.includes(mention));
          const auxiliaryContentAction = actions.includes('commented') ? 'commented' : 'reposted';
          const requiresMedia = action === auxiliaryContentAction
            && sourceRequires(semanticSource, 'media_submission');
          const requiresTranslation = action === auxiliaryContentAction
            && sourceRequires(semanticSource, 'translation_required');
          const requiresRepostText = action === 'reposted'
            && sourceRequires(semanticSource, 'repost_content');
          const showTextEditor = action === 'commented' || requiresRepostText;
          const showAdvancedRequirements = Boolean(
            friendRequirement || requiresMedia || requiresTranslation,
          );
          return (
            <fieldset className="exact-payload-editor" key={`payload-${action}`}>
              <legend>{t(`lotteries.exactPayloads.${action}`)}</legend>
              {showTextEditor ? (
                <label>
                  <span>{t(manualAssisted ? 'lotteries.manualExactText' : 'lotteries.exactText')}</span>
                  <textarea
                    className="input textarea"
                    value={payload.text || ''}
                    onChange={event => updateTextPayload(action, 'text', event.target.value)}
                    placeholder={t(`lotteries.exactTextPlaceholders.${action}`)}
                  />
                </label>
              ) : (
                <div className="automatic-payload-binding">
                  <span className="badge badge-ready">{t('lotteries.automaticPayload')}</span>
                  <span className="small-text">
                    {payload.text || t('lotteries.repostWithoutAdditionalText')}
                  </span>
                </div>
              )}
              {!!(topicTags.length || sourceMentions.length) && (
                <div className="automatic-requirements">
                  <strong className="small-text">{t('lotteries.autoBoundRuleTokens')}</strong>
                  <div className="blocker-list compact-blockers">
                    {topicTags.map(value => (
                      <span className="badge badge-info" key={`topic-${value}`}>{value}</span>
                    ))}
                    {sourceMentions.map(value => (
                      <span className="badge badge-info" key={`mention-${value}`}>{value}</span>
                    ))}
                  </div>
                </div>
              )}
              {showAdvancedRequirements && (
                <details className="payload-advanced-requirements">
                  <summary>{t('lotteries.explicitAdvancedRequirements')}</summary>
                  <div className="payload-advanced-body">
                    {friendRequirement && (
                      <label>
                        <span>{t('lotteries.friendMentionAccounts')}</span>
                        <textarea
                          className="input textarea"
                          value={metadataLines(friendMentions)}
                          onChange={event => updateTextPayload(
                            action,
                            'mentions',
                            [...sourceMentions, ...parseMetadataLines(event.target.value)].join('\n'),
                          )}
                          placeholder={t('lotteries.onePerLine')}
                        />
                        <span className="small-text muted-text">
                          {formatText(t('lotteries.friendMentionRequirement'), {
                            count: friendRequirement.count,
                            mode: t(`lotteries.friendMentionModes.${friendRequirement.mode}`),
                          })}
                        </span>
                      </label>
                    )}
                    {requiresMedia && (
                      <label>
                        <span>{t('lotteries.payloadFields.media_refs')}</span>
                        <textarea
                          className="input textarea"
                          value={metadataLines(payload.media_refs)}
                          onChange={event => updateTextPayload(action, 'media_refs', event.target.value)}
                          placeholder={t('lotteries.onePerLine')}
                        />
                      </label>
                    )}
                    {requiresTranslation && (
                      <label>
                        <span>{t('lotteries.payloadFields.translation')}</span>
                        <textarea
                          className="input textarea"
                          value={translationText(payload.translation)}
                          onChange={event => updateTextPayload(action, 'translation', event.target.value)}
                          placeholder={t('lotteries.translationPlaceholder')}
                        />
                      </label>
                    )}
                  </div>
                </details>
              )}
            </fieldset>
          );
        })}

        {!!unresolvedRequirements.length && (
          <div className="notice notice-warning" role="alert">
            {t('lotteries.unresolvedRequirements')}: {unresolvedRequirements.map(code => (
              t(`lotteries.unsupportedActions.${code}`)
            )).join(' / ')}
          </div>
        )}
        {!!payloadErrors.length && (
          <div className="notice notice-warning" role="alert">
            {payloadErrors.map(code => actionPlanBlockerText(code, t)).join(' / ')}
          </div>
        )}
        {fixedManualActions && !requiredActionSetComplete && (
          <div className="notice notice-warning" role="alert">
            {t('lotteries.xiaohongshuFourActionsRequired')}
          </div>
        )}
        {mediaRequired && (
          <div className="notice notice-warning">{mediaRequirementNotice}</div>
        )}
        <label className="notice notice-warning">
          <input
            type="checkbox"
            checked={ruleCompleteConfirmed}
            onChange={event => {
              operatorEditedRef.current = true;
              setRuleCompleteConfirmed(event.target.checked);
            }}
          />
          <span>{t('lotteries.completeRuleConfirmation')}</span>
        </label>
        <label className="notice notice-warning">
          <input
            type="checkbox"
            checked={reviewedConfirmed}
            onChange={event => {
              operatorEditedRef.current = true;
              setReviewedConfirmed(event.target.checked);
            }}
          />
          <span>{t(manualAssisted
            ? 'lotteries.manualReviewedPlanConfirmation'
            : 'lotteries.reviewedPlanConfirmation')}</span>
        </label>
        {saveDisabled && (
          <div className="rule-save-blockers" id={`lottery-${lottery.id}-save-blockers`} role="alert">
            <strong>{t('lotteries.ruleSaveMissingTitle')}</strong>
            <ul>
              {saveBlockerLabels.map((label, index) => (
                <li key={`${saveBlockers[index]}-${index}`}>{label}</li>
              ))}
            </ul>
          </div>
        )}
        <button
          className={`btn-primary ${saveDisabled ? 'is-blocked' : ''}`}
          type="button"
          disabled={saving}
          aria-disabled={saving}
          aria-describedby={saveDisabled ? `lottery-${lottery.id}-save-blockers` : undefined}
          title={saveDisabled ? saveBlockerLabels.join('；') : ''}
          onClick={handleSave}
        >
          {saving ? t('lotteries.savingCurrentRule') : t('lotteries.saveCurrentRule')}
        </button>
      </div>
    </details>
  );
}

function actionPayloadDraft(
  actions,
  payloads,
  contentRequirements = null,
  platform = 'bilibili',
  {
    followTargetFallback = '',
    rulePlan = null,
    prepareForEditing = false,
  } = {},
) {
  const source = payloads && typeof payloads === 'object' ? payloads : {};
  const requirements = contentRequirements && typeof contentRequirements === 'object'
    ? contentRequirements
    : {};
  const auxiliaryContentAction = actions.includes('commented') ? 'commented' : 'reposted';
  return lotteryActionsForPlatform(platform).reduce((result, action) => {
    if (!actions.includes(action)) return result;
    if (action === 'followed') {
      const followTargets = Array.isArray(requirements.follow_targets)
        ? requirements.follow_targets
        : [];
      const sourceTarget = source.followed && typeof source.followed.target_handle === 'string'
        ? source.followed.target_handle
        : '';
      result.followed = {
        target_handle: followTargets.length === 1
          ? followTargets[0]
          : (validLotteryHandle(sourceTarget) ? sourceTarget : followTargetFallback),
      };
      return result;
    }
    if (!['commented', 'reposted'].includes(action)) {
      result[action] = {};
      return result;
    }
    const payload = source[action] && typeof source[action] === 'object' ? source[action] : {};
    const sourceText = typeof payload.text === 'string' ? payload.text : '';
    const useDefaultRepost = prepareForEditing
      && action === 'reposted'
      && !sourceRequires(rulePlan, 'repost_content');
    result[action] = {
      text: useDefaultRepost ? defaultRepostText(platform) : sourceText,
    };
    for (const field of ['topic_tags', 'mentions', 'media_refs']) {
      const exactRequirement = requirements[action]?.[field];
      if (Array.isArray(exactRequirement)) {
        const values = [...exactRequirement];
        if (
          prepareForEditing
          && field === 'mentions'
          && rulePlan?.friend_mention_requirements?.[action]
          && Array.isArray(payload[field])
        ) {
          values.push(...payload[field].filter(item => !values.includes(item)));
        }
        if (values.length) result[action][field] = [...new Set(values)];
      } else if (!prepareForEditing && Array.isArray(payload[field]) && payload[field].length) {
        result[action][field] = [...payload[field]];
      } else if (
        prepareForEditing
        && field === 'media_refs'
        && action === auxiliaryContentAction
        && sourceRequires(rulePlan, 'media_submission')
        && Array.isArray(payload[field])
        && payload[field].length
      ) {
        result[action][field] = [...payload[field]];
      }
    }
    if (
      payload.translation !== undefined
      && payload.translation !== null
      && payload.translation !== ''
      && (
        !prepareForEditing
        || (
          action === auxiliaryContentAction
          && sourceRequires(rulePlan, 'translation_required')
        )
      )
    ) {
      result[action].translation = payload.translation;
    }
    return result;
  }, {});
}

function parseMetadataLines(value) {
  return [...new Set(String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean))];
}

function metadataLines(value) {
  return Array.isArray(value) ? value.join('\n') : '';
}

function translationText(value) {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function ManualAssistedChecklist({
  plan,
  gate,
  platform,
  lotteryId,
  lotteryStatus,
  onMarkResult,
  t,
}) {
  const items = manualAssistedChecklist(plan, platform);
  const shadowObservation = manualShadowObservation(gate);
  const confirmationEnabled = manualParticipationConfirmationEnabled(platform);
  const finalized = manualParticipationIsFinalized(lotteryStatus);
  const itemSignature = items
    .map(item => `${item.action}:${item.required ? '1' : '0'}:${item.exactValue}`)
    .join('|');
  const [confirmedActions, setConfirmedActions] = useState({});
  const [saving, setSaving] = useState(false);
  const confirmedActionCodes = Object.entries(confirmedActions)
    .filter(([, confirmed]) => confirmed)
    .map(([action]) => action);
  const canSubmit = confirmationEnabled
    && typeof onMarkResult === 'function'
    && manualParticipationCanSubmit(items, confirmedActionCodes, lotteryStatus);
  const resultStatusKey = `lotteries.${String(lotteryStatus || '').trim().toLowerCase()}`;
  const resultStatusText = finalized ? t(resultStatusKey) : '';

  useEffect(() => {
    setConfirmedActions({});
    setSaving(false);
  }, [lotteryId, itemSignature, lotteryStatus]);

  const toggleConfirmation = action => {
    if (finalized || saving) return;
    setConfirmedActions(current => ({
      ...current,
      [action]: !current[action],
    }));
  };

  const recordParticipation = async () => {
    if (!canSubmit) return;
    setSaving(true);
    try {
      await onMarkResult(
        lotteryId,
        'participated',
        manualParticipationResultNote(platform, items),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="manual-assisted-checklist" aria-label={t('lotteries.manualChecklistTitle')}>
      <div className="capability-row">
        <strong>{t('lotteries.manualChecklistTitle')}</strong>
        <span className="badge badge-warn">{t('lotteries.manualAssistedOnly')}</span>
      </div>
      <p className="small-text muted-text">
        {t(confirmationEnabled
          ? 'lotteries.manualChecklistInteractiveHint'
          : 'lotteries.manualChecklistHint')}
      </p>
      <ol>
        {items.map((item) => {
          const confirmed = finalized || confirmedActions[item.action] === true;
          return (
            <li className={confirmed ? 'is-confirmed' : ''} key={item.action}>
              {confirmationEnabled ? (
                <label className="manual-checklist-confirmation">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={finalized || saving}
                    onChange={() => toggleConfirmation(item.action)}
                  />
                  <span className={`badge ${confirmed ? 'badge-ready' : 'badge-warn'}`}>
                    {t(confirmed ? 'lotteries.manualConfirmed' : 'lotteries.manualPending')}
                  </span>
                  <span className="manual-checklist-content">
                    <strong>{t(`lotteries.actions.${item.action}`)}</strong>
                    {item.evidenceKey && (
                      <span className="small-text">{t(item.evidenceKey)}</span>
                    )}
                    {item.exactValue && (
                      <span className="small-text manual-exact-value">{item.exactValue}</span>
                    )}
                  </span>
                </label>
              ) : (
                <>
                  <span className={`badge ${item.required ? 'badge-ready' : 'badge-danger'}`}>
                    {item.required ? t('lotteries.planIncluded') : t('lotteries.planMissing')}
                  </span>
                  <strong>{t(`lotteries.actions.${item.action}`)}</strong>
                  {item.exactValue && (
                    <span className="small-text manual-exact-value">{item.exactValue}</span>
                  )}
                </>
              )}
            </li>
          );
        })}
      </ol>
      {confirmationEnabled && (
        <div className="manual-participation-confirmation">
          {finalized ? (
            <div className="capability-row" role="status">
              <span>{t('lotteries.manualParticipationAlreadyRecorded')}</span>
              <span className="badge badge-ready">{resultStatusText}</span>
            </div>
          ) : (
            <button
              className="btn-primary"
              type="button"
              disabled={!canSubmit || saving}
              onClick={recordParticipation}
            >
              {saving
                ? t('lotteries.manualParticipationSaving')
                : t('lotteries.manualParticipationSubmit')}
            </button>
          )}
        </div>
      )}
      <div className="small-text mono">
        {t('lotteries.shadowEvidence')}: {shadowObservation.complete
          ? t('lotteries.shadowEvidenceReady')
          : t('lotteries.shadowEvidenceMissing')}
        {shadowObservation.taskId ? ` / ${shortIdentity(shadowObservation.taskId)}` : ''}
      </div>
      <p className="small-text warning-text">
        {t(confirmationEnabled
          ? 'lotteries.manualChecklistConfirmationNoMutation'
          : 'lotteries.manualChecklistNoMutation')}
      </p>
    </section>
  );
}

function SavedExactPayloads({ payloads, t }) {
  const rows = ['commented', 'reposted'].filter(action => payloads[action]?.text);
  const followTarget = payloads.followed?.target_handle;
  if (!rows.length && !followTarget) return null;
  return (
    <div className="rule-plan-snapshots">
      {followTarget && (
        <div className="rule-plan-snapshot" key="saved-payload-followed">
          <span>{t('lotteries.followTarget')}</span>
          <strong className="small-text">{followTarget}</strong>
        </div>
      )}
      {rows.map(action => (
        <div className="rule-plan-snapshot" key={`saved-payload-${action}`}>
          <span>{t(`lotteries.exactPayloads.${action}`)}</span>
          <strong className="small-text">{payloads[action].text}</strong>
        </div>
      ))}
    </div>
  );
}

function formatRate(value) {
  if (value === null || value === undefined) return '-';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function formatNumber(value) {
  if (value === null || value === undefined) return '-';
  return Number(value).toFixed(2);
}
