import { useEffect, useRef, useState } from 'react';

import { apiPath, deleteJSON, fetchJSON, getConfirmedHeaders, postJSON, putJSON } from '../api';
import { formatText } from '../i18n/format';
import { realRunControlState } from '../realRunControl';
import {
  isManualAssistedPlan,
  lotteryActionsForPlatform,
  manualAssistedChecklist,
  refreshedWorkflowBindings,
  selectorExecutionEvidenceReady,
  workflowActivityIdentity,
  manualShadowObservation as readManualShadowObservation,
} from '../lotteryCompatibility';
import { useUi } from '../uiContext';

const REAL_RUN_EVIDENCE_TTL_MS = 65000;
const REAL_RUN_EVIDENCE_REFRESH_MS = 60000;
const RUNTIME_STATUS_REFRESH_MS = 15000;
function manualOnlyHintKey(platform) {
  if (platform === 'douyin') return 'deploy.douyinManualOnlyHint';
  if (platform === 'weibo') return 'deploy.weiboManualOnlyHint';
  return 'deploy.xiaohongshuManualOnlyHint';
}

function reviewedManualChecklist(evidence, platform) {
  const rawPlan = evidence?.action_plan;
  const plan = typeof rawPlan === 'string' ? safeJson(rawPlan) : rawPlan;
  return manualAssistedChecklist(plan, platform);
}

export default function Deploy() {
  const { notify: toast, t } = useUi();
  const version = `v${import.meta.env.VITE_DPMS_VERSION || '0.3.13'}`;
  const [message, setMessage] = useState('');
  const [reloadArmed, setReloadArmed] = useState(false);
  const [uploadSignature, setUploadSignature] = useState('');
  const [uploadSignatureUnlocked, setUploadSignatureUnlocked] = useState(false);
  const [channels, setChannels] = useState([]);
  const [notifyStatus, setNotifyStatus] = useState(null);
  const [notifyGuide, setNotifyGuide] = useState(null);
  const [secretDrafts, setSecretDrafts] = useState({});
  const [secretBundle, setSecretBundle] = useState('');
  const [logs, setLogs] = useState([]);
  const [probes, setProbes] = useState([]);
  const [taskRuns, setTaskRuns] = useState([]);
  const [adapterConfig, setAdapterConfig] = useState(null);
  const [runtimeSettings, setRuntimeSettings] = useState(null);
  const [productionReadiness, setProductionReadiness] = useState(null);
  const [realRunEvidence, setRealRunEvidence] = useState([]);
  const [externalIntents, setExternalIntents] = useState([]);
  const [reconciliationItems, setReconciliationItems] = useState([]);
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [realRunArmed, setRealRunArmed] = useState(false);
  const [rollbackArmed, setRollbackArmed] = useState(false);
  const [rollbackReason, setRollbackReason] = useState('manual rollback');
  const [selectorJson, setSelectorJson] = useState(defaultSelectorJson);
  const [selectorB64, setSelectorB64] = useState('');
  const [notify, setNotify] = useState({ channel: 'serverchan', title: 'DPMS test', content: 'Notification channel test' });
  const [platforms, setPlatforms] = useState([]);
  const [readinessPlatform, setReadinessPlatform] = useState('bilibili');
  const workflowActivityIdentityRef = useRef('');
  const evidenceBindingByPlatformRef = useRef(new Map());
  const loadGenerationRef = useRef(0);
  const evidenceLoadsInFlightRef = useRef(0);
  const evidenceRefreshPendingRef = useRef(false);
  const evidenceExpiryTimerRef = useRef(null);
  const mountedRef = useRef(true);
  const realRunControl = realRunControlState(runtimeSettings, productionReadiness);

  const reportLoadError = (err) => {
    if (!mountedRef.current) return;
    const text = formatText(t('deploy.loadFailed'), { message: err.message });
    setMessage(text);
    toast(text, 'error');
  };

  const loadNotify = async (includeRealRunEvidence = true) => {
    if (includeRealRunEvidence && evidenceLoadsInFlightRef.current > 0) {
      evidenceRefreshPendingRef.current = true;
      return;
    }
    if (!includeRealRunEvidence && evidenceLoadsInFlightRef.current > 0) return;
    if (includeRealRunEvidence) {
      evidenceLoadsInFlightRef.current += 1;
      window.clearTimeout(evidenceExpiryTimerRef.current);
      if (mountedRef.current) setRealRunEvidence([]);
    }
    const generation = ++loadGenerationRef.current;
    try {
      const [
        channelRows, statusRows, guideRows, logRows, adapterRows, probeRows,
        taskRows, runtimeRows, evidenceRows, platformRows, intentRows,
        reconciliationRows, readinessRows,
      ] = await Promise.all([
        fetchJSON('/notify/channels'),
        fetchJSON('/notify/status'),
        fetchJSON('/notify/config-guide'),
        fetchJSON('/notify/logs'),
        fetchJSON('/lotteries/adapters/config'),
        fetchJSON('/lotteries/probes'),
        fetchJSON('/lotteries/tasks/runs'),
        fetchJSON('/metrics/runtime/settings'),
        includeRealRunEvidence
          ? fetchJSON('/lotteries/real-run/evidence')
          : Promise.resolve(null),
        fetchJSON('/accounts/platforms'),
        fetchJSON('/metrics/external-action-intents?limit=50'),
        fetchJSON('/metrics/reconciliation?limit=50'),
        fetchJSON('/metrics/readiness'),
      ]);
      // A slow earlier poll must never overwrite a newer completion/evidence
      // response. This also makes the active-light -> completed-heavy handoff
      // deterministic without cancelling requests in the shared API wrapper.
      if (generation !== loadGenerationRef.current) return;
      setChannels(channelRows);
      setNotifyStatus(statusRows);
      setNotifyGuide(guideRows);
      setLogs(logRows);
      setAdapterConfig(adapterRows);
      setProbes(probeRows);
      setTaskRuns(taskRows);
      setRuntimeSettings(runtimeRows);
      setProductionReadiness(readinessRows);
      setExternalIntents(intentRows.items || []);
      setReconciliationItems(reconciliationRows.items || []);
      if (evidenceRows) {
        const evidenceItems = evidenceRows.items || [];
        evidenceBindingByPlatformRef.current = refreshedWorkflowBindings(
          evidenceBindingByPlatformRef.current,
          evidenceItems,
          probeRows,
          taskRows,
        );
        setRealRunEvidence(evidenceItems);
        window.clearTimeout(evidenceExpiryTimerRef.current);
        evidenceExpiryTimerRef.current = window.setTimeout(() => {
          if (mountedRef.current) setRealRunEvidence([]);
        }, REAL_RUN_EVIDENCE_TTL_MS);
      }
      setPlatforms(platformRows);
    } catch (err) {
      if (generation !== loadGenerationRef.current) return;
      throw err;
    } finally {
      if (includeRealRunEvidence) {
        evidenceLoadsInFlightRef.current -= 1;
        if (evidenceRefreshPendingRef.current && mountedRef.current) {
          evidenceRefreshPendingRef.current = false;
          void loadNotify(true).catch(reportLoadError);
        }
      }
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    const refreshEvidence = () => loadNotify(true).catch(reportLoadError);
    void refreshEvidence();
    const evidenceTimer = window.setInterval(refreshEvidence, REAL_RUN_EVIDENCE_REFRESH_MS);
    return () => {
      mountedRef.current = false;
      evidenceRefreshPendingRef.current = false;
      loadGenerationRef.current += 1;
      window.clearInterval(evidenceTimer);
      window.clearTimeout(evidenceExpiryTimerRef.current);
    };
  }, [t, toast]);

  useEffect(() => {
    let cancelled = false;
    const refreshRuntime = async () => {
      try {
        const [nextRuntime, nextReadiness] = await Promise.all([
          fetchJSON('/metrics/runtime/settings'),
          fetchJSON('/metrics/readiness'),
        ]);
        if (!cancelled && mountedRef.current) {
          setRuntimeSettings(nextRuntime);
          setProductionReadiness(nextReadiness);
        }
      } catch {
        // The full page refresh reports actionable API errors. This lightweight
        // status poll stays silent, but readiness must fail closed rather than
        // leave an old green prerequisite snapshot actionable.
        if (!cancelled && mountedRef.current) setProductionReadiness(null);
      }
    };
    const runtimeTimer = window.setInterval(refreshRuntime, RUNTIME_STATUS_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(runtimeTimer);
    };
  }, []);

  useEffect(() => {
    if (!realRunControl.canArm && realRunArmed) setRealRunArmed(false);
  }, [realRunArmed, realRunControl.canArm]);

  const restart = async () => {
    const res = await fetch(apiPath('/metrics/worker/restart'), { method: 'POST', headers: getConfirmedHeaders() });
    const text = res.ok ? t('deploy.reloadSent') : t('deploy.operationFailed');
    setMessage(text);
    toast(text, res.ok ? 'success' : 'error');
    if (res.ok) setReloadArmed(false);
  };

  const upload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (!uploadSignature) {
      const text = t('deploy.uploadReady');
      setMessage(text);
      toast(text, 'error');
      e.target.value = '';
      return;
    }
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(apiPath('/update/upload'), { method: 'POST', headers: getConfirmedHeaders({ signature: uploadSignature }), body: form });
    const text = res.ok ? t('deploy.uploadDone') : t('deploy.uploadFailed');
    setMessage(text);
    toast(text, res.ok ? 'success' : 'error');
    e.target.value = '';
  };

  const sendTest = async (e) => {
    e.preventDefault();
    try {
      const res = await postJSON('/notify/send', notify);
      const text = formatText(t('deploy.notificationQueued'), { logId: res.log_id });
      setMessage(text);
      toast(text, 'success');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    }
  };

  const saveChannelSecret = async (channel) => {
    const draft = secretDrafts[channel.id] || {};
    const payload = {};
    for (const item of channel.env) {
      const field = secretFieldByEnv[item.name];
      const value = draft[item.name];
      if (field && value) payload[field] = value;
    }
    if (!Object.keys(payload).length) {
      const text = t('deploy.secretRequired');
      setMessage(text);
      toast(text, 'error');
      return;
    }
    try {
      const result = await putJSON(`/notify/secrets/${channel.id}`, payload);
      const text = formatText(t('deploy.secretSaved'), { channel: channel.label });
      setSecretDrafts(prev => ({ ...prev, [channel.id]: {} }));
      setMessage(text);
      toast(text, result.configured ? 'success' : 'info');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    }
  };

  const saveSecretBundle = async () => {
    const content = secretBundle.trim();
    if (!content) {
      const text = t('deploy.secretBundleRequired');
      setMessage(text);
      toast(text, 'error');
      return;
    }
    try {
      const result = await putJSON('/notify/secrets', { content });
      const savedKeys = result.saved_keys?.join(', ') || '-';
      const text = formatText(t('deploy.secretBundleSaved'), { keys: savedKeys });
      setSecretBundle('');
      setMessage(text);
      toast(text, result.configured_channels?.length ? 'success' : 'info');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    }
  };

  const clearChannelSecret = async (channel) => {
    try {
      await deleteJSON(`/notify/secrets/${channel.id}`, { confirm: true });
      const text = formatText(t('deploy.secretCleared'), { channel: channel.label });
      setSecretDrafts(prev => ({ ...prev, [channel.id]: {} }));
      setMessage(text);
      toast(text, 'success');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    }
  };

  const buildSelectorConfig = () => {
    try {
      const parsed = JSON.parse(selectorJson);
      const normalized = JSON.stringify(parsed);
      setSelectorB64(toBase64Utf8(normalized));
      setMessage(t('deploy.selectorEncoded'));
      toast(t('deploy.selectorEncoded'), 'success');
    } catch (err) {
      setSelectorB64('');
      const text = formatText(t('deploy.invalidSelector'), { message: err.message });
      setMessage(text);
      toast(text, 'error');
    }
  };

  const saveSelectorConfig = async () => {
    try {
      const parsed = JSON.parse(selectorJson);
      const result = await putJSON('/lotteries/adapters/config', { config: parsed });
      const text = formatText(t('deploy.selectorSaved'), { count: result.saved?.length || 0 });
      setMessage(text);
      toast(text, result.invalid?.length ? 'warning' : 'success');
      await loadNotify();
    } catch (err) {
      const text = formatText(t('deploy.invalidSelector'), { message: err.message });
      setMessage(text);
      toast(text, 'error');
    }
  };

  const clearSelectorConfig = async (platform) => {
    try {
      await deleteJSON(`/lotteries/adapters/config/${platform}`, { confirm: true });
      const text = formatText(t('deploy.selectorCleared'), { platform });
      setMessage(text);
      toast(text, 'success');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    }
  };

  const validTargetIds = new Set(
    realRunEvidence.filter(item => item.target_valid).map(item => item.lottery_id),
  );
  const probeCandidates = probes
    .map(probe => ({ probe, draft: buildDraftFromProbe(probe) }))
    .filter(item => item.draft && validTargetIds.has(item.probe.lottery_id));

  const buildPlatformWorkflow = (platform) => {
    const boundLotteryId = evidenceBindingByPlatformRef.current.get(platform) || null;
    const evidence = realRunEvidence.find(item => (
      item.platform === platform
      && item.target_valid
      && ['pending', 'claimed'].includes(item.status)
      && (!boundLotteryId || String(item.lottery_id) === String(boundLotteryId))
    )) || (!boundLotteryId ? realRunEvidence.find(item => (
      item.platform === platform
      && item.target_valid
      && ['pending', 'claimed'].includes(item.status)
    )) : null);
    const platformEvidence = realRunEvidence.find(item => item.platform === platform);
    const manualAssisted = isManualAssistedPlan(
      platform,
      evidence?.action_plan || evidence?.execution_path_id || '',
    );
    const invalidTarget = realRunEvidence.find(item => item.platform === platform && !item.target_valid);
    const adapter = adapterConfig?.platforms?.find(item => item.platform === platform);
    const probeCandidate = probeCandidates.find(item => (
      item.probe.platform === platform
      && item.probe.lottery_id === evidence?.lottery_id
    ));
    const selectorDraftReady = hasSelectorDraft(selectorJson, platform);
    const reportedNextAction = evidence?.next_action || 'add_target';
    const nextAction = manualAssisted && ['enable_real_run', 'real_run'].includes(reportedNextAction)
      ? 'manual_assisted'
      : reportedNextAction;
    const activeProbe = probes.find(item => (
      item.platform === platform
      && ['queued', 'running'].includes(item.status)
      && (!boundLotteryId || String(item.lottery_id) === String(boundLotteryId))
    ));
    const activeShadow = taskRuns.find(item => (
      item.platform === platform
      && item.task_mode === 'shadow_run'
      && ['queued', 'running'].includes(item.status)
      && (!boundLotteryId || String(item.lottery_id) === String(boundLotteryId))
    ));
    const workflowActive = Boolean(activeProbe || activeShadow);
    const activityIdentity = workflowActivityIdentity(platform, activeProbe, activeShadow);
    const readiness = buildReadiness({
      platform,
      evidence,
      platformEvidence,
      adapter,
      runtimeSettings,
      probeCandidate,
    });
    return {
      evidence, platformEvidence, invalidTarget, adapter, probeCandidate,
      selectorDraftReady, nextAction, activeProbe, activeShadow, workflowActive,
      activityIdentity, readiness, manualAssisted,
    };
  };

  const workflow = buildPlatformWorkflow(readinessPlatform);
  const workflowReadyForReal = Boolean(!workflow.manualAssisted && workflow.evidence?.allowed);
  const manualShadowObservation = readManualShadowObservation(workflow.evidence);
  const manualChecklist = reviewedManualChecklist(workflow.evidence, readinessPlatform);
  const readinessPlatformLabel = platforms.find(item => item.id === readinessPlatform)?.label || readinessPlatform;

  useEffect(() => {
    if (!workflow.activityIdentity) return undefined;
    let cancelled = false;
    let timer;
    const poll = async () => {
      // Active workflow status is lightweight; the completion transition below
      // performs one authoritative evidence refresh instead of hashing files
      // every four seconds.
      try {
        await loadNotify(false);
      } catch (err) {
        setMessage(err.message);
      }
      if (!cancelled) timer = window.setTimeout(poll, 4000);
    };
    timer = window.setTimeout(poll, 4000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [workflow.activityIdentity]);

  useEffect(() => {
    const previousIdentity = workflowActivityIdentityRef.current;
    workflowActivityIdentityRef.current = workflow.activityIdentity;
    if (previousIdentity && previousIdentity !== workflow.activityIdentity) {
      loadNotify(true).catch(err => setMessage(err.message));
    }
  }, [workflow.activityIdentity]);

  const useProbeDraft = (item) => {
    setSelectorJson(JSON.stringify(item.draft, null, 2));
    setSelectorB64('');
    const text = formatText(t('deploy.draftLoaded'), { probe: item.probe.probe_id?.slice(0, 8) });
    setMessage(text);
    toast(text, 'success');
  };

  const advanceWorkflow = async () => {
    const { evidence, probeCandidate } = workflow;
    if (!evidence) {
      const text = formatText(t('deploy.noTargetForPlatform'), { platform: readinessPlatformLabel });
      setMessage(text);
      toast(text, 'error');
      return;
    }

    const action = workflow.nextAction;
    const lotteryId = evidence.lottery_id;
    if (workflow.manualAssisted && ['manual_assisted', 'enable_real_run', 'real_run'].includes(action)) {
      const text = t(manualOnlyHintKey(readinessPlatform));
      setMessage(text);
      toast(text, 'warning');
      return;
    }
    setWorkflowBusy(true);
    try {
      let result;
      if (action === 'probe') {
        result = await postJSON(`/lotteries/${lotteryId}/probe`, { account_id: null });
      } else if (action === 'configure_adapter') {
        if (!probeCandidate) throw new Error(t('deploy.noProbeDraft'));
        useProbeDraft(probeCandidate);
        document.querySelector('.probe-candidate-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      } else if (action === 'shadow_run') {
        result = await postJSON(`/lotteries/${lotteryId}/dispatch`, {
          mode: 'shadow_run',
          dry_run: true,
          confirm: false,
          account_id: null,
        });
      } else if (action === 'enable_real_run') {
        setRealRunArmed(true);
        const text = t('deploy.reviewRealRunSwitch');
        setMessage(text);
        toast(text, 'warning');
        window.scrollTo({ top: 0, behavior: 'smooth' });
        return;
      } else {
        const text = evidence.allowed
          ? t(runtimeSettings?.autopilot?.real_run_authorized
            ? 'deploy.autopilotRealRunReadyNotice'
            : 'deploy.realRunReadyNotice')
          : t('deploy.workflowManualReview');
        setMessage(text);
        toast(text, evidence.allowed ? 'success' : 'warning');
        return;
      }

      const text = formatText(t('deploy.workflowQueued'), {
        action: t(`lotteries.nextActions.${action}`),
        id: result.probe_id?.slice(0, 8) || result.task_id?.slice(0, 8) || '-',
      });
      setMessage(text);
      toast(text, 'success');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    } finally {
      setWorkflowBusy(false);
    }
  };

  const selectedChannel = channels.find(channel => channel.id === notify.channel);
  const selectedChannelConfigured = Boolean(selectedChannel?.configured);
  const toggleRealRun = async (enabled) => {
    if (enabled && !realRunControl.canEnable) {
      const text = realRunControlMessage(realRunControl, t);
      setRealRunArmed(false);
      setMessage(text);
      toast(text, 'warning');
      return;
    }
    try {
      await putJSON('/metrics/runtime/settings/real-run', { enabled }, { confirm: true });
      setRealRunArmed(false);
      const text = t(enabled ? 'deploy.realRunEnabled' : 'deploy.realRunDisabled');
      setMessage(text);
      toast(text, enabled ? 'warning' : 'success');
      await loadNotify();
    } catch (err) {
      const text = localizeRealRunUpdateError(err, t);
      setMessage(text);
      toast(text, 'error');
    }
  };

  const runtimeRollback = async () => {
    try {
      const result = await postJSON('/metrics/runtime/rollback', { reason: rollbackReason }, { confirm: true });
      setRollbackArmed(false);
      const text = formatText(t('deploy.rollbackDone'), { count: result.queued_real_runs_cancelled || 0 });
      setMessage(text);
      toast(text, 'warning');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
    }
  };

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('deploy.eyebrow')}</p>
          <h1>{t('deploy.title')}</h1>
        </div>
      </header>

      <div className="ops-grid two-columns">
        <div className="panel">
          <div className="panel-title">{t('deploy.runtime')}</div>
          <div className="version-row">
            <span>{t('deploy.currentVersion')}</span>
            <span className="badge badge-info">{version}</span>
          </div>
          <div className="version-row">
            <span>{t('deploy.realRun')}</span>
            <span className={`badge ${realRunControl.currentlyEnabled ? 'badge-danger' : 'badge-muted'}`}>
              {t(realRunControl.currentlyEnabled ? 'deploy.enabled' : 'deploy.disabled')}
            </span>
          </div>
          <div className="version-row">
            <span>{t('deploy.deploymentRealRunCapability')}</span>
            <span className={`badge ${realRunControl.deploymentCapability ? 'badge-ready' : 'badge-danger'}`}>
              {t(realRunControl.deploymentCapability
                ? 'deploy.capabilityAvailable'
                : 'deploy.capabilityUnavailable')}
            </span>
          </div>
          <div className="version-row">
            <span>{t('deploy.globalBreaker')}</span>
            <span className={`badge ${globalBreakerBadge(runtimeSettings?.global_circuit_breaker)}`}>
              {t(`deploy.breakerStatuses.${globalBreakerStatus(runtimeSettings?.global_circuit_breaker)}`)}
            </span>
          </div>
          {runtimeSettings?.global_circuit_breaker?.reason && (
            <p className="muted-text tight-text">
              {localizeBreakerReason(runtimeSettings.global_circuit_breaker.reason, t)}
            </p>
          )}
          <div className="version-row">
            <span>{t('deploy.autopilot')}</span>
            <span className={`badge ${autopilotStatusBadge(runtimeSettings?.autopilot)}`}>
              {t(`deploy.autopilotStatuses.${autopilotStatusKey(runtimeSettings?.autopilot)}`)}
            </span>
          </div>
          {runtimeSettings?.autopilot?.reported && (
            <div className="stack-list compact-list">
              <div className="small-text muted-text">
                {formatText(t('deploy.autopilotPlatforms'), {
                  platforms: runtimeSettings.autopilot.platform_allowlist?.join(', ') || t('deploy.none'),
                })}
              </div>
              <div className="small-text muted-text">
                {formatText(t('deploy.autopilotLastRound'), {
                  selected: runtimeSettings.autopilot.last_round?.selected ?? 0,
                  dispatched: runtimeSettings.autopilot.last_round?.dispatched ?? 0,
                  probes: runtimeSettings.autopilot.last_round?.probes_requested ?? 0,
                  failures: runtimeSettings.autopilot.last_round?.failures ?? 0,
                })}
              </div>
              <div className="small-text muted-text">
                {formatText(t('deploy.autopilotHeartbeat'), {
                  age: runtimeSettings.autopilot.heartbeat_age_seconds ?? '-',
                  cadence: runtimeSettings.autopilot.poll_interval_seconds ?? '-',
                })}
              </div>
            </div>
          )}
          <div className={`notice ${runtimeSettings?.autopilot?.real_run_authorized ? '' : 'notice-warning'}`}>
            {autopilotNextAction(runtimeSettings, realRunEvidence, t)}
          </div>
          <div className={`notice ${realRunControl.canEnable ? '' : 'notice-warning'}`}>
            {realRunControlMessage(realRunControl, t)}
          </div>
          <label className="check-row">
            <input
              type="checkbox"
              checked={realRunArmed}
              disabled={!realRunControl.canArm}
              onChange={e => setRealRunArmed(e.target.checked)}
            />
            <span>{t('deploy.confirmRealRunChange')}</span>
          </label>
          <div className="toolbar">
            <button
              className={realRunControl.currentlyEnabled ? 'btn-ghost' : 'btn-danger'}
              type="button"
              disabled={!realRunArmed || !realRunControl.canArm}
              onClick={() => toggleRealRun(!realRunControl.currentlyEnabled)}
            >
              {t(realRunControl.currentlyEnabled ? 'deploy.disableRealRun' : 'deploy.enableRealRun')}
            </button>
          </div>
          <div className="rollback-box">
            <div className="panel-kicker">{t('deploy.runtimeRollback')}</div>
            <p className="muted-text tight-text">{t('deploy.rollbackHint')}</p>
            <label>
              <span>{t('deploy.rollbackReason')}</span>
              <input className="input" value={rollbackReason} onChange={e => setRollbackReason(e.target.value)} />
            </label>
            <label className="check-row">
              <input type="checkbox" checked={rollbackArmed} onChange={e => setRollbackArmed(e.target.checked)} />
              <span>{t('deploy.rollbackArmed')}</span>
            </label>
            <button
              type="button"
              className="btn-danger"
              disabled={!rollbackArmed}
              onClick={runtimeRollback}
            >
              {t('deploy.applyRollback')}
            </button>
          </div>
          <label className="check-row">
            <input type="checkbox" checked={reloadArmed} onChange={e => setReloadArmed(e.target.checked)} />
            <span>{t('deploy.reloadArmed')}</span>
          </label>
          <div className="toolbar">
            <button onClick={restart} className="btn-danger" disabled={!reloadArmed}>{t('deploy.workerReload')}</button>
          </div>
          <form className="stack-form" onSubmit={e => e.preventDefault()}>
            <label>
              <span>{t('deploy.hmacSignature')}</span>
              <input
                className="input"
                type="password"
                name="dpms-update-package-signature"
                value={uploadSignature}
                onChange={e => setUploadSignature(e.target.value)}
                onFocus={() => setUploadSignatureUnlocked(true)}
                readOnly={!uploadSignatureUnlocked}
                autoComplete="new-password"
                data-1p-ignore="true"
                data-lpignore="true"
              />
            </label>
            <div className="toolbar">
              <label className="btn-primary file-button">
                {t('deploy.uploadPackage')}
                <input type="file" onChange={upload} disabled={!uploadSignature} hidden />
              </label>
            </div>
          </form>
        </div>

        <div className="panel">
          <div className="panel-title">{t('deploy.notificationTest')}</div>
          <div className="channel-list">
            {channels.map(channel => (
              <span className={`badge ${channel.configured ? 'badge-ready' : 'badge-muted'}`} key={channel.id}>
                {channel.label}
              </span>
            ))}
          </div>
          <form className="stack-form" onSubmit={sendTest}>
            <label>
              <span>{t('deploy.channel')}</span>
              <select className="input" value={notify.channel} onChange={e => setNotify({ ...notify, channel: e.target.value })}>
                {channels.map(channel => <option key={channel.id} value={channel.id}>{channel.label}</option>)}
              </select>
            </label>
            <label>
              <span>{t('deploy.testTitle')}</span>
              <input className="input" value={notify.title} onChange={e => setNotify({ ...notify, title: e.target.value })} />
            </label>
            <label>
              <span>{t('deploy.content')}</span>
              <textarea className="input textarea" value={notify.content} onChange={e => setNotify({ ...notify, content: e.target.value })} />
            </label>
            <button className="btn-primary" type="submit" disabled={!selectedChannelConfigured}>{t('deploy.sendTest')}</button>
            {!selectedChannelConfigured && <p className="muted-text tight-text">{t('deploy.missingSelected')}</p>}
          </form>
        </div>
      </div>

      {message && <div className="notice">{message}</div>}

      <div className="panel">
        <div className="notify-guide-head">
          <div>
            <div className="panel-title">
              {workflow.manualAssisted
                ? formatText(t('deploy.manualAssistedReadiness'), { platform: readinessPlatformLabel })
                : formatText(t('deploy.realRunReadiness'), { platform: readinessPlatformLabel })}
            </div>
            <p className="muted-text tight-text">
              {t(workflow.manualAssisted
                ? manualOnlyHintKey(readinessPlatform)
                : 'deploy.realRunReadinessHint')}
            </p>
          </div>
          <span className={`badge ${workflowReadyForReal ? 'badge-ready' : 'badge-warn'}`}>
            {workflow.manualAssisted
              ? t('lotteries.manualAssistedOnly')
              : (workflowReadyForReal
                ? t('deploy.readyForReal')
                : t(`lotteries.nextActions.${workflow.nextAction}`))}
          </span>
        </div>
        <div className="segmented platform-tabs">
          {platforms.map(platform => (
            <button
              key={platform.id}
              type="button"
              className={readinessPlatform === platform.id ? 'active' : ''}
              onClick={() => setReadinessPlatform(platform.id)}
            >
              {platform.label}
            </button>
          ))}
        </div>
        <div className="bilibili-workflow-bar">
          <div>
            <div className="panel-kicker">{t('deploy.workflowTarget')}</div>
            <div className="mono">
              {workflow.evidence ? `L${workflow.evidence.lottery_id} / ${readinessPlatform}` : formatText(t('deploy.noTargetForPlatform'), { platform: readinessPlatformLabel })}
            </div>
            {!workflow.evidence && workflow.invalidTarget && (
              <div className="small-text notify-error">{t('deploy.invalidTargetsIgnored')}</div>
            )}
          </div>
          <div>
            <div className="panel-kicker">{t('deploy.currentSafeAction')}</div>
            <div>{t(`lotteries.nextActions.${workflow.nextAction}`)}</div>
          </div>
          <div>
            <div className="panel-kicker">{t('deploy.workflowState')}</div>
            <div>
              {workflow.activeProbe
                ? formatText(t('deploy.probeInProgress'), { id: workflow.activeProbe.probe_id?.slice(0, 8) })
                : workflow.activeShadow
                  ? formatText(t('deploy.shadowInProgress'), { id: workflow.activeShadow.task_id?.slice(0, 8) })
                  : t('deploy.workflowIdle')}
            </div>
          </div>
        </div>
        {workflow.manualAssisted && (
          <div className="manual-assisted-checklist" role="note">
            <div className="capability-row">
              <strong>{t('deploy.manualChecklistTitle')}</strong>
              <span className={`badge ${manualShadowObservation.complete ? 'badge-ready' : 'badge-muted'}`}>
                {manualShadowObservation.complete
                  ? t('lotteries.shadowEvidenceReady')
                  : t('lotteries.shadowEvidenceMissing')}
              </span>
            </div>
            <p className="small-text muted-text">{t('deploy.manualChecklistHint')}</p>
            {manualChecklist.length ? (
              <ol>
                {manualChecklist.map(item => (
                  <li key={item.action}>
                    <span className="badge badge-warn">{t('lotteries.manualPending')}</span>
                    <strong>{t(`lotteries.actions.${item.action}`)}</strong>
                    {item.exactValue && (
                      <span className="small-text manual-exact-value">{item.exactValue}</span>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="small-text muted-text">{t('deploy.manualChecklistUnavailable')}</p>
            )}
          </div>
        )}
        <div className="bilibili-readiness-grid">
          {workflow.readiness.map(step => (
            <div className="bilibili-readiness-step" key={step.code}>
              <div>
                <span className={`badge ${step.ready ? 'badge-ready' : step.severity === 'danger' ? 'badge-danger' : 'badge-warn'}`}>
                  {step.ready ? t('deploy.ready') : t('deploy.needsAction')}
                </span>
                <div className="action-title">{t(`deploy.readinessSteps.${step.code}Title`)}</div>
                <p className="muted-text tight-text">{formatText(t(`deploy.readinessSteps.${step.code}Detail`), { platform: readinessPlatformLabel })}</p>
              </div>
              <div className="small-text mono">{localizeReadinessMeta(step.meta, t)}</div>
            </div>
          ))}
        </div>
        <div className="toolbar">
          <button
            className="btn-primary"
            type="button"
            disabled={
              workflowBusy
              || Boolean(workflow.activeProbe)
              || Boolean(workflow.activeShadow)
              || !workflow.evidence
              || workflow.nextAction === 'manual_assisted'
              || ['add_account', 'review_risk', 'blocked'].includes(workflow.evidence?.next_action)
            }
            onClick={advanceWorkflow}
          >
            {workflowBusy
              ? t('deploy.workflowSubmitting')
              : workflow.activeProbe
                ? t('deploy.probeRunning')
                : workflow.activeShadow
                  ? t('deploy.shadowRunning')
                  : workflow.nextAction === 'manual_assisted'
                    ? t('lotteries.manualAssistedOnly')
                    : workflow.evidence?.next_action === 'real_run'
                    ? t('deploy.reviewRealRunTask')
                    : workflow.evidence?.next_action === 'enable_real_run'
                      ? t('deploy.reviewRealRunSwitchButton')
                      : formatText(t('deploy.runSafeNextStep'), {
                          action: t(`lotteries.nextActions.${workflow.nextAction}`),
                        })}
          </button>
          <button
            className="btn-ghost"
            type="button"
            disabled={!workflow.probeCandidate}
            onClick={() => useProbeDraft(workflow.probeCandidate)}
          >
            {t('deploy.loadProbeDraft')}
          </button>
          <button className="btn-ghost" type="button" disabled={!workflow.selectorDraftReady} onClick={saveSelectorConfig}>
            {t('deploy.saveRuntimeSelectors')}
          </button>
          <button className="btn-ghost" type="button" onClick={loadNotify}>
            {t('common.refresh')}
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="notify-guide-head">
          <div>
            <div className="panel-title">{t('deploy.reconciliationTitle')}</div>
            <p className="muted-text tight-text">{t('deploy.reconciliationHint')}</p>
          </div>
          <span className={`badge ${reconciliationItems.length ? 'badge-danger' : 'badge-ready'}`}>
            {formatText(t('deploy.reconciliationCount'), { count: reconciliationItems.length })}
          </span>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('deploy.task')}</th>
                <th>{t('deploy.platform')}</th>
                <th>{t('deploy.result')}</th>
                <th>{t('deploy.unknownIntents')}</th>
                <th>{t('deploy.succeededIntents')}</th>
                <th>{t('deploy.updated')}</th>
              </tr>
            </thead>
            <tbody>
              {reconciliationItems.map(item => (
                <tr key={item.task_id}>
                  <td className="mono" title={item.task_id}>{String(item.task_id || '').slice(0, 12)}</td>
                  <td>{item.platform || '-'}</td>
                  <td>
                    <span className="badge badge-danger">
                      {item.reconciliation_required ? t('deploy.reconciliationRequired') : item.task_status}
                    </span>
                  </td>
                  <td>{item.unknown_effect_count || item.unknown_intent_count || 0}</td>
                  <td>{item.succeeded_intent_count || 0}</td>
                  <td className="small-text">{item.latest_intent_at || item.finished_at || '-'}</td>
                </tr>
              ))}
              {!reconciliationItems.length && (
                <tr><td colSpan="6" className="empty-cell">{t('deploy.noReconciliationItems')}</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="panel-title compact-title">{t('deploy.externalIntentTitle')}</div>
        <p className="muted-text tight-text">{t('deploy.externalIntentHint')}</p>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('deploy.task')}</th>
                <th>{t('deploy.action')}</th>
                <th>{t('deploy.result')}</th>
                <th>{t('deploy.effectCertainty')}</th>
                <th>{t('deploy.attempt')}</th>
                <th>{t('deploy.payloadHash')}</th>
                <th>{t('deploy.updated')}</th>
              </tr>
            </thead>
            <tbody>
              {externalIntents.map(item => (
                <tr key={item.intent_id}>
                  <td className="mono" title={item.task_id}>{String(item.task_id || '').slice(0, 12)}</td>
                  <td>{item.action || '-'}</td>
                  <td>
                    <span className={`badge ${intentStatusClass(item.status)}`}>{item.status || '-'}</span>
                  </td>
                  <td className="mono">{item.effect_certainty || '-'}</td>
                  <td>{item.attempt_no || 0}</td>
                  <td className="mono" title={item.payload_hash}>{shortHash(item.payload_hash)}</td>
                  <td className="small-text">{item.updated_at || '-'}</td>
                </tr>
              ))}
              {!externalIntents.length && (
                <tr><td colSpan="7" className="empty-cell">{t('deploy.noExternalIntents')}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('deploy.notificationHealth')}</div>
        <div className="notify-health-grid">
          {notifyStatus?.channels?.map(channel => (
            <div className="notify-health-row" key={channel.id}>
              <div>
                <div className="mono">{channel.label}</div>
                <div className="small-text muted-text">
                  {channel.configured ? t('deploy.configured') : t('deploy.missingSecrets')}
                  {channel.last_log
                    ? ` / ${t('deploy.last')}: ${t(`deploy.${notificationLogStatus(channel.last_log)}`)}`
                    : ` / ${t('deploy.noLogs')}`}
                </div>
                {channel.last_error && (
                  <div className="small-text notify-error">
                    {localizeNotificationError(channel.last_error, t)}
                  </div>
                )}
              </div>
              <span className={`badge ${channel.healthy ? 'badge-ready' : channel.configured ? 'badge-warn' : 'badge-muted'}`}>
                {channel.healthy
                  ? t('deploy.healthy')
                  : channel.verification_required
                    ? t('deploy.verificationRequired')
                    : channel.configured
                      ? t('deploy.needsCheck')
                      : t('deploy.notSet')}
              </span>
            </div>
          ))}
        </div>
        <p className="muted-text tight-text">
          {formatText(t('deploy.configuredChannels'), { count: notifyStatus?.configured_count ?? 0 })}
        </p>
        {notifyStatus?.last_dispatch_skip && (
          <div className="notice">{formatText(t('deploy.lastDispatchSkip'), { title: notifyStatus.last_dispatch_skip.title })}</div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">{t('deploy.notificationGuide')}</div>
        <div className="notify-env-summary">
          <div className="notify-guide-head">
            <div>
              <div className="mono">{t('deploy.envBundle')}</div>
              <div className="small-text muted-text">
                {notifyGuide?.production_ready ? t('deploy.productionReady') : t('deploy.productionMissing')}
              </div>
            </div>
            <span className={`badge ${notifyGuide?.production_ready ? 'badge-ready' : 'badge-warn'}`}>
              {notifyGuide?.production_ready ? t('deploy.configured') : t('deploy.missing')}
            </span>
          </div>
          <div className="small-text muted-text">
            {t('deploy.missingRequired')}: {notifyGuide?.missing_required?.length ? notifyGuide.missing_required.join(', ') : t('deploy.none')}
          </div>
          <label>
            <span>{t('deploy.minimumEnv')}</span>
            <textarea className="input textarea code-textarea notify-env-output" readOnly value={notifyGuide?.minimum_env_bundle || ''} />
          </label>
          <label>
            <span>{t('deploy.fullEnv')}</span>
            <textarea className="input textarea code-textarea notify-env-output" readOnly value={notifyGuide?.env_bundle || ''} />
          </label>
          <label>
            <span>{t('deploy.secretBundle')}</span>
            <textarea
              className="input textarea code-textarea notify-env-output"
              value={secretBundle}
              onChange={e => setSecretBundle(e.target.value)}
              placeholder={t('deploy.secretBundlePlaceholder')}
              autoComplete="off"
            />
          </label>
          <div className="toolbar">
            <button className="btn-primary" type="button" onClick={saveSecretBundle}>
              {t('deploy.saveSecretBundle')}
            </button>
            <button className="btn-ghost" type="button" onClick={() => setSecretBundle(notifyGuide?.minimum_env_bundle || '')}>
              {t('deploy.useMinimumEnv')}
            </button>
            <button className="btn-ghost" type="button" onClick={() => setSecretBundle('')}>
              {t('common.cancel')}
            </button>
          </div>
        </div>
        <div className="notify-guide-grid">
          {notifyGuide?.channels?.map(channel => (
            <div className="notify-guide-row" key={channel.id}>
              <div className="notify-guide-head">
                <div>
                  <div className="mono">{channel.label}</div>
                  <div className="small-text muted-text">{channel.env.map(item => item.name).join(' + ')}</div>
                </div>
                <span className={`badge ${channel.env.every(item => item.configured) ? 'badge-ready' : 'badge-muted'}`}>
                  {channel.env.every(item => item.configured) ? t('deploy.configured') : t('deploy.missing')}
                </span>
              </div>
              <textarea className="input textarea code-textarea notify-env-output" readOnly value={channel.env_example} />
              <div className="stack-form">
                {channel.env.map(item => (
                  <label key={item.name}>
                    <span>{item.name}</span>
                    <input
                      className="input"
                      type="password"
                      value={secretDrafts[channel.id]?.[item.name] || ''}
                      onChange={e => setSecretDrafts(prev => ({
                        ...prev,
                        [channel.id]: { ...(prev[channel.id] || {}), [item.name]: e.target.value },
                      }))}
                      placeholder={item.configured ? t('deploy.secretConfigured') : t('deploy.secretPlaceholder')}
                      autoComplete="off"
                    />
                  </label>
                ))}
                <div className="toolbar">
                  <button className="btn-primary" type="button" onClick={() => saveChannelSecret(channel)}>
                    {t('deploy.saveSecret')}
                  </button>
                  <button className="btn-ghost" type="button" onClick={() => clearChannelSecret(channel)}>
                    {t('deploy.clearSecret')}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="ops-checklist">
          {notifyGuide?.apply_steps?.map(step => (
            <div className="ops-check-item" key={step}>{localizeNotifyApplyStep(step, t)}</div>
          ))}
        </div>
        <p className="muted-text tight-text">
          {formatText(t('deploy.testEndpoint'), { endpoint: `POST ${apiPath('/notify/send')}` })}
        </p>
      </div>

      <div className="panel">
        <div className="panel-title">{t('deploy.adapterBuilder')}</div>
        <div className="ops-grid two-columns">
          <div className="stack-form">
            <label>
              <span>{t('deploy.selectorJson')}</span>
              <textarea
                className="input textarea code-textarea"
                value={selectorJson}
                onChange={e => setSelectorJson(e.target.value)}
              />
            </label>
            <div className="toolbar">
              <button className="btn-primary" type="button" onClick={buildSelectorConfig}>{t('deploy.generateBase64')}</button>
              <button className="btn-primary" type="button" onClick={saveSelectorConfig}>{t('deploy.saveRuntimeSelectors')}</button>
              <button className="btn-ghost" type="button" onClick={() => { setSelectorJson(defaultSelectorJson); setSelectorB64(''); }}>{t('deploy.resetSample')}</button>
            </div>
          </div>
          <div className="stack-form">
            <div className="config-status-grid">
              {adapterConfig?.platforms?.map(item => (
                <div className="config-status-row" key={item.platform}>
                  <span className="mono">{item.platform}</span>
                  <span className={`badge ${item.configured ? 'badge-ready' : 'badge-muted'}`}>
                    {item.configured
                      ? t(item.configuration_kind === 'observation' ? 'deploy.observationConfigured' : 'deploy.configured')
                      : t('deploy.planned')}
                  </span>
                  <button className="btn-ghost" type="button" onClick={() => clearSelectorConfig(item.platform)}>{t('deploy.clearRuntimeSelectors')}</button>
                </div>
              ))}
            </div>
            <label>
              <span>{t('deploy.envOutput')}</span>
              <textarea
                className="input textarea code-textarea"
                readOnly
                value={selectorB64 ? `DPMS_ADAPTER_SELECTORS_B64=${selectorB64}` : t('deploy.envPlaceholder')}
              />
            </label>
            <p className="muted-text tight-text">
              {formatText(t('deploy.preferredKey'), { key: adapterConfig?.preferred_env || 'DPMS_ADAPTER_SELECTORS_B64' })}
            </p>
          </div>
        </div>
        <div className="probe-candidate-panel">
          <div className="panel-title compact-title">{t('deploy.probeCandidates')}</div>
          {probeCandidates.length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr><th>{t('deploy.probe')}</th><th>{t('deploy.platform')}</th><th>{t('deploy.activity')}</th><th>{t('deploy.visiblePhases')}</th><th>{t('deploy.action')}</th></tr>
                </thead>
                <tbody>
                  {probeCandidates.map(item => (
                    <tr key={item.probe.probe_id}>
                      <td className="mono">{item.probe.probe_id?.slice(0, 8)}</td>
                      <td>{item.probe.platform}</td>
                      <td>{item.probe.lottery_id ? `L${item.probe.lottery_id}` : '-'}</td>
                      <td>{probeReadyPhaseCount(item.probe)}/{probePhaseTotal(item.probe)}</td>
                      <td><button className="btn-ghost" type="button" onClick={() => useProbeDraft(item)}>{t('deploy.useDraft')}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-cell">{t('deploy.noProbeCandidates')}</div>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('deploy.notificationLogs')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>{t('deploy.channel')}</th><th>{t('deploy.result')}</th><th>{t('deploy.testTitle')}</th><th>{t('deploy.created')}</th></tr>
            </thead>
            <tbody>
              {logs.map(log => (
                <tr key={log.id}>
                  <td className="mono">N{log.id}</td>
                  <td>{log.channel}</td>
                  <td>
                    <span className={`badge ${notificationLogBadge(log)}`}>
                      {t(`deploy.${notificationLogStatus(log)}`)}
                    </span>
                  </td>
                  <td className="truncate-cell" title={log.content}>{log.title}</td>
                  <td className="small-text">{log.created_at}</td>
                </tr>
              ))}
              {!logs.length && <tr><td colSpan="5" className="empty-cell">{t('deploy.noNotificationLogs')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

const defaultSelectorJson = JSON.stringify(
  {
    weibo: {
      followed: ["text=followed"],
      liked: ["text=liked"],
      commented: { input: ["textarea"], submit: ["text=submit"], text: "Participate" },
      favorited: ["text=favorited"],
      reposted: ["text=reposted"],
    },
  },
  null,
  2,
);

function toBase64Utf8(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.slice(i, i + chunkSize));
  }
  return btoa(binary);
}

function buildDraftFromProbe(probe) {
  if (!probe?.result) return null;
  let parsed;
  try {
    parsed = typeof probe.result === 'string' ? JSON.parse(probe.result) : probe.result;
  } catch {
    return null;
  }
  if (parsed?._recommended_config?.[probe.platform]) {
    return parsed._recommended_config;
  }
  const phases = {};
  for (const phase of lotteryActionsForPlatform(probe.platform)) {
    const candidates = Array.isArray(parsed?.[phase]) ? parsed[phase] : [];
    const visible = candidates.find(item => item?.visible && item?.selector);
    if (visible) phases[phase] = [visible.selector];
  }
  if (!Object.keys(phases).length) return null;
  return { [probe.platform]: phases };
}

function probeReadyPhaseCount(probe) {
  const result = typeof probe?.result === 'string' ? safeJson(probe.result) : probe?.result;
  return result?._summary?.ready_phase_count ?? Object.keys(buildDraftFromProbe(probe)?.[probe.platform] || {}).length;
}

function probePhaseTotal(probe) {
  const result = typeof probe?.result === 'string' ? safeJson(probe.result) : probe?.result;
  const requiredPhases = result?._summary?.required_phases;
  return Array.isArray(requiredPhases) && requiredPhases.length
    ? requiredPhases.length
    : lotteryActionsForPlatform(probe?.platform).length;
}

const NOTIFY_APPLY_STEP_KEYS = {
  'Edit .env with one or more notification channel values.': 'configure',
  'Run docker compose up -d --build so core-api receives the new environment.': 'rebuild',
  'Open Operations & Notify and send a manual notification test.': 'sendTest',
  'Confirm the latest notify_logs row is Sent before relying on production alerts.': 'verifyDelivery',
};

function localizeNotifyApplyStep(step, t) {
  const key = NOTIFY_APPLY_STEP_KEYS[String(step || '').trim()];
  return key ? t(`deploy.notifyApplySteps.${key}`) : t('deploy.notifyApplySteps.review');
}

const READINESS_META_KEYS = {
  missing: 'missing',
  dynamic: 'dynamic',
  video: 'video',
  note: 'note',
  status: 'status',
  'OAuth capabilities verified': 'oauthCapabilitiesVerified',
  'OAuth capability proof required': 'oauthCapabilityProofRequired',
  complete: 'complete',
  'official OAuth adapter': 'officialOauthAdapter',
  'OAuth adapter unavailable': 'oauthAdapterUnavailable',
  'evidence binding required': 'evidenceBindingRequired',
  configured: 'configured',
  'observation complete': 'observationComplete',
  'observation required': 'observationRequired',
  '24h ok': 'recentShadowReady',
  required: 'required',
  unknown: 'unknown',
  'manual only': 'manualOnly',
  enabled: 'enabled',
  disabled: 'disabled',
};

function localizeReadinessMeta(meta, t) {
  const value = String(meta || 'unknown').trim();
  const safeCount = value.match(/^(\d+) safe$/);
  if (safeCount) {
    return formatText(t('deploy.readinessMeta.safeAccounts'), { count: safeCount[1] });
  }
  if (/^\d+\/\d+$/.test(value)) return value;
  const key = READINESS_META_KEYS[value];
  return key ? t(`deploy.readinessMeta.${key}`) : t('deploy.readinessMeta.unknown');
}

function buildReadiness({ platform, evidence, platformEvidence, adapter, runtimeSettings, probeCandidate }) {
  const blockers = new Set(evidence?.blockers || []);
  const manualAssisted = isManualAssistedPlan(
    platform,
    evidence?.action_plan || evidence?.execution_path_id || '',
  );
  const oauthExecution = evidence?.execution_mode === 'oauth';
  const manualShadowObservation = readManualShadowObservation(evidence);
  const phaseCount = probeCandidate ? probeReadyPhaseCount(probeCandidate.probe) : 0;
  const phaseTotal = probeCandidate
    ? probePhaseTotal(probeCandidate.probe)
    : lotteryActionsForPlatform(platform).length;
  const selectorEvidenceReady = selectorExecutionEvidenceReady(adapter, evidence);
  const selectorEvidenceUnbound = blockers.has('api_path_probe_evidence_not_implemented')
    || blockers.has('selector_config_evidence_binding_not_implemented');
  const readiness = [
    {
      code: 'target',
      ready: Boolean(evidence?.target_valid),
      severity: 'danger',
      meta: evidence?.target_kind || 'missing',
    },
    {
      code: 'account',
      ready: Boolean(platformEvidence?.safe_accounts),
      severity: 'danger',
      meta: platformEvidence?.safe_accounts ? `${platformEvidence.safe_accounts} safe` : '0 safe',
    },
    {
      code: 'probe',
      ready: Boolean(evidence?.probe_ready),
      severity: 'warning',
      meta: oauthExecution
        ? (evidence?.probe_ready ? 'OAuth capabilities verified' : 'OAuth capability proof required')
        : (evidence?.probe_ready ? 'complete' : `${phaseCount}/${phaseTotal}`),
    },
    {
      code: 'selector',
      ready: oauthExecution ? Boolean(evidence?.oauth_adapter_ready) : selectorEvidenceReady,
      severity: 'warning',
      meta: oauthExecution
        ? (evidence?.oauth_adapter_ready ? 'official OAuth adapter' : 'OAuth adapter unavailable')
        : (selectorEvidenceUnbound
          ? 'evidence binding required'
          : (selectorEvidenceReady ? 'configured' : 'missing')),
    },
    {
      code: 'shadow',
      ready: manualAssisted
        ? manualShadowObservation.complete
        : Boolean(evidence?.shadow_ready),
      severity: 'warning',
      meta: manualAssisted
        ? (manualShadowObservation.complete ? 'observation complete' : 'observation required')
        : (evidence?.shadow_ready
          ? '24h ok'
          : blockers.has('recent_shadow_run_required') ? 'required' : 'unknown'),
    },
  ];
  readiness.push(manualAssisted ? {
    code: 'manual',
    ready: false,
    severity: 'warning',
    meta: 'manual only',
  } : {
    code: 'global',
    ready: Boolean(runtimeSettings?.real_run_enabled),
    severity: 'danger',
    meta: runtimeSettings?.real_run_enabled ? 'enabled' : 'disabled',
  });
  return readiness;
}

function safeJson(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function hasSelectorDraft(value, platform) {
  const parsed = safeJson(value);
  return Boolean(parsed?.[platform] && Object.keys(parsed[platform]).length);
}

function shortHash(value) {
  const text = String(value || '');
  return text ? `${text.slice(0, 12)}…` : '-';
}

function notificationLogStatus(log) {
  if (['sent', 'failed', 'skipped'].includes(log?.delivery_status)) {
    return log.delivery_status;
  }
  return log?.success ? 'sent' : 'failed';
}

function notificationLogBadge(log) {
  const status = notificationLogStatus(log);
  if (status === 'sent') return 'badge-ready';
  if (status === 'skipped') return 'badge-muted';
  return 'badge-danger';
}

function localizeNotificationError(value, t) {
  const code = String(value || '').trim();
  const statusMatch = code.match(/^notification_http_status:(\d{3})$/);
  if (statusMatch) {
    return formatText(t('deploy.notificationErrors.httpStatus'), { status: statusMatch[1] });
  }
  const known = {
    notification_http_status_error: 'httpStatusUnknown',
    notification_timeout: 'timeout',
    notification_transport_error: 'transport',
  };
  return t(`deploy.notificationErrors.${known[code] || 'unknown'}`);
}

function localizedProductionCheck(check, t) {
  const titleKey = `dashboard.checksMap.${check?.code}Title`;
  const exampleKey = `dashboard.checksMap.${check?.code}Example`;
  const title = t(titleKey);
  const example = t(exampleKey);
  return {
    title: title === titleKey ? t('dashboard.checksMap.unknownTitle') : title,
    example: example === exampleKey ? t('dashboard.checksMap.unknownExample') : example,
  };
}

function realRunControlMessage(control, t) {
  const firstBlocker = control.blockers[0];
  const localized = firstBlocker ? localizedProductionCheck(firstBlocker, t) : null;
  if (control.currentlyEnabled) {
    if (!control.deploymentCapability) {
      return t('deploy.realRunControl.enabledWithoutCapability');
    }
    if (!control.readinessAvailable) {
      return t('deploy.realRunControl.enabledWithoutReadiness');
    }
    if (localized) {
      return formatText(t('deploy.realRunControl.enabledWithBlocker'), {
        count: control.blockers.length,
        check: localized.title,
        example: localized.example,
      });
    }
    return t('deploy.realRunControl.enabled');
  }
  if (!control.deploymentCapability) {
    return t(control.deploymentCapabilityReported
      ? 'deploy.realRunControl.deploymentDisabled'
      : 'deploy.realRunControl.deploymentUnknown');
  }
  if (!control.readinessAvailable) {
    return t('deploy.realRunControl.readinessUnavailable');
  }
  if (localized) {
    return formatText(t('deploy.realRunControl.prerequisitesBlocked'), {
      count: control.blockers.length,
      check: localized.title,
      example: localized.example,
    });
  }
  return t('deploy.realRunControl.readyForOwnerReview');
}

function localizedProductionBlockerCode(code, t) {
  const checkKey = `dashboard.checksMap.${code}Title`;
  const check = t(checkKey);
  if (check !== checkKey) return check;
  const gateKey = `lotteries.realGateBlockers.${code}`;
  const gate = t(gateKey);
  return gate === gateKey ? t('dashboard.checksMap.unknownTitle') : gate;
}

function localizeRealRunUpdateError(error, t) {
  const serverCode = error?.serverCode || error?.details?.code;
  if (
    serverCode === 'real_run_deployment_capability_disabled'
    || (error?.status === 409 && /REAL_RUN_ENABLED capability is disabled/i.test(error?.message || ''))
  ) {
    return t('deploy.realRunErrors.real_run_deployment_capability_disabled');
  }
  if (serverCode === 'real_run_prerequisites_not_ready') {
    const blockerCodes = Array.isArray(error?.details?.blocker_codes)
      ? error.details.blocker_codes
      : [];
    const blockers = [...new Set(
      blockerCodes.slice(0, 5).map(code => localizedProductionBlockerCode(code, t)),
    )];
    return formatText(t('deploy.realRunErrors.real_run_prerequisites_not_ready'), {
      blockers: blockers.join('、') || t('deploy.realRunErrors.unknownPrerequisites'),
    });
  }
  if (error?.status === 409) return t('deploy.realRunErrors.runtimeConflict');
  return error?.message || t('deploy.operationFailed');
}

function globalBreakerStatus(breaker) {
  const status = String(breaker?.status || '').trim().toLowerCase();
  return ['closed', 'open', 'half_open'].includes(status) ? status : 'unknown';
}

function globalBreakerBadge(breaker) {
  const status = globalBreakerStatus(breaker);
  if (status === 'closed') return 'badge-ready';
  if (status === 'open') return 'badge-danger';
  if (status === 'half_open') return 'badge-warn';
  return 'badge-warn';
}

function autopilotStatusKey(autopilot) {
  if (!autopilot?.available || !autopilot?.reported) return 'unreported';
  if (!autopilot.fresh) return 'stale';
  if (!autopilot.enabled || autopilot.status === 'disabled') return 'disabled';
  if (autopilot.status === 'degraded' || (autopilot.last_round?.failures ?? 0) > 0) return 'degraded';
  return 'running';
}

function autopilotStatusBadge(autopilot) {
  const status = autopilotStatusKey(autopilot);
  if (status === 'running') return 'badge-ready';
  if (status === 'degraded' || status === 'stale') return 'badge-warn';
  return 'badge-muted';
}

function autopilotNextAction(runtimeSettings, realRunEvidence, t) {
  const autopilot = runtimeSettings?.autopilot;
  const status = autopilotStatusKey(autopilot);
  if (status === 'unreported') return t('deploy.autopilotNext.unreported');
  if (status === 'stale') return t('deploy.autopilotNext.stale');
  if (status === 'disabled') return t('deploy.autopilotNext.disabled');
  if (!autopilot.dispatch_configured || !autopilot.platform_allowlist_valid) {
    return t('deploy.autopilotNext.configure');
  }
  if ((autopilot.last_round?.failures ?? 0) > 0) return t('deploy.autopilotNext.failures');
  const platformAllowlist = new Set(
    (Array.isArray(autopilot.platform_allowlist) ? autopilot.platform_allowlist : [])
      .map(platform => String(platform || '').trim().toLowerCase())
      .filter(Boolean),
  );
  const blockers = (realRunEvidence || [])
    .filter(item => platformAllowlist.has(String(item?.platform || '').trim().toLowerCase()))
    .filter(item => ['pending', 'claimed'].includes(String(item?.status || '').trim().toLowerCase()))
    .flatMap(item => Array.isArray(item?.blockers) ? item.blockers : [])
    .filter(Boolean)
    .filter(item => ![
      'global_real_run_disabled',
      'global_circuit_breaker_open',
      'global_circuit_breaker_not_closed',
    ].includes(item));
  const blocker = blockers.find(item => /action_plan|rule_snapshot/.test(item))
    || blockers[0];
  if ((autopilot.last_round?.selected ?? 0) === 0 && blocker) {
    return formatText(t('deploy.autopilotNext.blockedTarget'), {
      blocker: localizeRealRunBlocker(blocker, t),
      example: autopilotBlockerExample(blocker, t),
    });
  }
  if (globalBreakerStatus(runtimeSettings?.global_circuit_breaker) !== 'closed') {
    return t('deploy.autopilotNext.breakerOpen');
  }
  if (!autopilot.real_run_authorized) return t('deploy.autopilotNext.validationOnly');
  return t('deploy.autopilotNext.active');
}

function localizeRealRunBlocker(blocker, t) {
  const mapped = t(`lotteries.realGateBlockers.${blocker}`);
  return mapped === `lotteries.realGateBlockers.${blocker}`
    ? t('deploy.autopilotNext.unknownBlocker')
    : mapped;
}

function autopilotBlockerExample(blocker, t) {
  const mapped = t(`deploy.autopilotBlockerExamples.${blocker}`);
  return mapped === `deploy.autopilotBlockerExamples.${blocker}`
    ? t('deploy.autopilotBlockerExamples.default')
    : mapped;
}

function localizeBreakerReason(reason, t) {
  const knownReasons = {
    'local migration and test safety hold': 'localMigrationHold',
  };
  const key = knownReasons[String(reason || '').trim()] || 'recorded';
  return t(`deploy.breakerReasons.${key}`);
}

function intentStatusClass(status) {
  if (status === 'succeeded') return 'badge-ready';
  if (['unknown', 'started'].includes(status)) return 'badge-danger';
  if (status === 'failed') return 'badge-warn';
  return 'badge-muted';
}

const secretFieldByEnv = {
  SERVERCHAN_KEY: 'serverchan_key',
  FEISHU_WEBHOOK: 'feishu_webhook',
  GENERIC_WEBHOOK_URL: 'generic_webhook_url',
  TELEGRAM_BOT_TOKEN: 'telegram_bot_token',
  TELEGRAM_CHAT_ID: 'telegram_chat_id',
};
