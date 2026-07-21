import { useEffect, useMemo, useRef, useState } from 'react';

import { authenticatedApiPath, fetchJSON, postJSON, putJSON } from '../api';
import StatusBadge from '../components/StatusBadge';
import { formatText } from '../i18n/format';
import {
  actionPlanHasMediaRequirement,
  actionPlanV2Blockers,
  actionPlanV2Ready,
  actionPlanV2ReviewBlockers,
  actionPlanV2ReviewReady,
  buildActionPlanV2Update,
  dispatchSafetyBlocker,
  executionEvidencePresentation,
  isManualAssistedPlatform,
  lotteryActionsForPlatform,
  platformDispatchBlocker,
  platformExecutionPathId,
  realRunEvidencePath,
  sourceRuleCorrectionPath,
  targetTransportCompatibilityIssue,
  targetValidationErrorCode,
  unresolvedRuleRequirements,
  xiaohongshuManualChecklist,
  xiaohongshuShadowObservation,
} from '../lotteryCompatibility';
import { useUi } from '../uiContext';

const API_ACTION_PHASES = {
  follow: 'followed',
  like: 'liked',
  comment: 'commented',
  repost: 'reposted',
};
const REAL_RUN_EVIDENCE_TTL_MS = 65000;

export default function Lotteries() {
  const { notify, t } = useUi();
  const loadInFlightRef = useRef(false);
  const evidenceRefreshPendingRef = useRef(false);
  const evidenceExpiryTimerRef = useRef(null);
  const selectedAccountRef = useRef('');
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
  const [discoveryMessage, setDiscoveryMessage] = useState('');
  const [dispatchMode, setDispatchMode] = useState('dry_run');
  const [selectedAccount, setSelectedAccount] = useState('');
  const [form, setForm] = useState({ platform: 'bilibili', raw_url: '', value_score: 50 });
  const [targetImport, setTargetImport] = useState({ platform: 'bilibili', content: '', value_score: 50 });
  const [targetImportResult, setTargetImportResult] = useState(null);
  const [sourceForm, setSourceForm] = useState({
    platform: 'bilibili',
    source_type: 'url_list',
    source_value: '',
    scan_interval_minutes: 30,
  });

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

  const realRunEnabled = Boolean(realRunEvidence[0]?.real_run_enabled);
  const safeAccountCount = platformId => safeAccounts.filter(account => account.platform === platformId).length;
  const selectedSafeAccount = safeAccounts.find(account => String(account.id) === String(selectedAccount));
  const manualTargetIssue = targetTransportCompatibilityIssue(form.platform, form.raw_url);

  const load = async (includeRealRunEvidence = true) => {
    if (!mountedRef.current) return;
    if (includeRealRunEvidence) {
      window.clearTimeout(evidenceExpiryTimerRef.current);
      setRealRunEvidence([]);
    }
    if (loadInFlightRef.current) {
      evidenceRefreshPendingRef.current ||= includeRealRunEvidence;
      return;
    }
    loadInFlightRef.current = true;
    try {
      const [lotteryRows, runRows, probeRows, sourceRows, strategyRows, accountRows, platformRows, adapterRows] = await Promise.all([
        fetchJSON('/lotteries/'),
        fetchJSON('/lotteries/tasks/runs'),
        fetchJSON('/lotteries/probes'),
        fetchJSON('/lotteries/sources'),
        fetchJSON('/lotteries/strategy/queue'),
        fetchJSON('/accounts/'),
        fetchJSON('/accounts/platforms'),
        fetchJSON('/lotteries/adapters'),
      ]);
      const [readinessRows, evidenceRows] = await Promise.all([
        fetchJSON('/metrics/readiness'),
        includeRealRunEvidence
          ? fetchJSON(realRunEvidencePath(selectedAccountRef.current))
          : Promise.resolve(null),
      ]);
      if (!mountedRef.current) return;
      setLotteries(lotteryRows);
      setRuns(runRows);
      setProbes(probeRows);
      setSources(sourceRows);
      setStrategyQueue(strategyRows.items || []);
      setAccounts(accountRows);
      setPlatforms(platformRows);
      setAdapters(adapterRows);
      setReadiness(readinessRows);
      if (evidenceRows) {
        setRealRunEvidence(evidenceRows.items || []);
        window.clearTimeout(evidenceExpiryTimerRef.current);
        evidenceExpiryTimerRef.current = window.setTimeout(() => {
          if (mountedRef.current) setRealRunEvidence([]);
        }, REAL_RUN_EVIDENCE_TTL_MS);
      }
    } catch (err) {
      if (!mountedRef.current) return;
      if (includeRealRunEvidence) setRealRunEvidence([]);
      const message = targetValidationErrorText(err.message, t);
      setError(message);
      notify(message, 'error');
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
    setError('');
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
      const message = targetValidationErrorText(err.message, t);
      setError(message);
      notify(message, 'error');
    }
  };

  const importTargets = async (e) => {
    e.preventDefault();
    setError('');
    setTargetImportResult(null);
    try {
      const result = await postJSON('/lotteries/targets/import', {
        platform: targetImport.platform,
        content: targetImport.content,
        value_score: Number(targetImport.value_score || 0),
      });
      setTargetImportResult(result);
      const message = formatText(t('lotteries.targetsImported'), result);
      notify(message, result.invalid_count ? 'warning' : 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const readTargetFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      setTargetImport(prev => ({ ...prev, content: text }));
      notify(formatText(t('lotteries.targetFileLoaded'), { name: file.name }), 'success');
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      e.target.value = '';
    }
  };

  const createSource = async (e) => {
    e.preventDefault();
    setError('');
    setDiscoveryMessage('');
    try {
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
      const result = await postJSON('/lotteries/sources/scan', {});
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
      const manualAssisted = isManualAssistedPlatform(lottery.platform);
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
    safeAccountAvailable: Boolean(lottery && safeAccountCount(lottery.platform)),
    accountScopeBound: Boolean(
      lottery
      && selectedSafeAccount
      && selectedSafeAccount.platform === lottery.platform
    ),
  });

  const dispatchBlockerMessage = (blocker, lottery) => {
    if (blocker === 'xiaohongshu_manual_only') return t('lotteries.xiaohongshuManualOnlyHint');
    if (blocker === 'xiaohongshu_manual_shadow_only') return t('lotteries.xiaohongshuManualShadowOnlyHint');
    if (blocker === 'legacy_http_target') return t('lotteries.legacyHttpTargetHint');
    if (blocker === 'no_safe_account') return t('lotteries.noSafeAccount');
    if (blocker === 'account_scope_required') return t('lotteries.accountScopeRequired');
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
      const selectedMatches = selectedSafeAccount && lottery && selectedSafeAccount.platform === lottery.platform;
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
      const selectedMatches = selectedSafeAccount && lottery && selectedSafeAccount.platform === lottery.platform;
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
      const selectedMatches = selectedSafeAccount && lottery && selectedSafeAccount.platform === lottery.platform;
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

  const markResult = async (id, status) => {
    setError('');
    try {
      await putJSON(`/lotteries/${id}/result`, { status, note: `Manual result set to ${status}` });
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
            {safeAccounts.map(account => <option value={account.id} key={account.id}>A{account.id} / {account.platform} / {t('lotteries.calibrated')}</option>)}
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
            <div className="capability-row"><span>{t('lotteries.adapter')}</span><span className="mono small-text">{platform.adapter_status || 'planned'}</span></div>
            <div className="capability-row"><span>{t('lotteries.safeAccounts')}</span><span className="mono small-text">{safeAccountCount(platform.id)}</span></div>
            <div className="capability-row"><span>{t('lotteries.selectorObservation')}</span><span className="mono small-text">{ready?.latest_probe ? `${ready.latest_probe.ready_phase_count || 0}/4` : '-'}</span></div>
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
                const strategyBlocker = item.recommended_mode === 'blocked'
                  ? 'mode_blocked'
                  : dispatchBlockerFor(lottery, item.recommended_mode);
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
                    <div className="blocker-list">
                      {item.blockers?.map(reason => <span className="badge badge-danger" key={reason}>{reason}</span>)}
                      {item.reason_codes?.map(code => <span className="badge badge-muted" key={code}>{reasonText(code, t)}</span>)}
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
              <select className="input" value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value })}>
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <label className="url-field">
              <span>{t('lotteries.activityUrl')}</span>
              <input className="input" value={form.raw_url} onChange={e => setForm({ ...form, raw_url: e.target.value })} />
              {manualTargetIssue && (
                <span className="small-text warning-text" role="note">
                  {t('lotteries.targetErrors.https_required')}
                </span>
              )}
            </label>
            <label>
              <span>{t('lotteries.score')}</span>
              <input className="input" type="number" value={form.value_score} onChange={e => setForm({ ...form, value_score: e.target.value })} />
            </label>
            <button className="btn-primary" type="submit">{t('lotteries.create')}</button>
          </form>
          <form onSubmit={importTargets} className="form-grid target-import-form">
            <label>
              <span>{t('lotteries.defaultPlatform')}</span>
              <select className="input" value={targetImport.platform} onChange={e => setTargetImport({ ...targetImport, platform: e.target.value })}>
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <label>
              <span>{t('lotteries.defaultScore')}</span>
              <input className="input" type="number" value={targetImport.value_score} onChange={e => setTargetImport({ ...targetImport, value_score: e.target.value })} />
            </label>
            <label className="btn-ghost file-button target-file-button">
              {t('lotteries.uploadTargets')}
              <input type="file" accept=".txt,.csv,text/plain,text/csv" onChange={readTargetFile} hidden />
            </label>
            <label className="target-text-field">
              <span>{t('lotteries.targetList')}</span>
              <textarea
                className="input textarea"
                value={targetImport.content}
                onChange={e => setTargetImport({ ...targetImport, content: e.target.value })}
                placeholder={t('lotteries.targetPlaceholder')}
              />
            </label>
            <button className="btn-primary" type="submit">{t('lotteries.importTargets')}</button>
          </form>
        </div>
        {targetImportResult && (
          <div className="notice import-result">
            {formatText(t('lotteries.importSummary'), targetImportResult)}
            {!!targetImportResult.invalid?.length && (
              <div className="small-text">
                {targetImportResult.invalid
                  .slice(0, 3)
                  .map(item => `#${item.line}: ${targetValidationErrorText(item.error, t)}`)
                  .join(' / ')}
              </div>
            )}
          </div>
        )}
        {error && <div className="alert-danger">{error}</div>}
      </div>

      <div className="panel">
        <div className="panel-title">{t('lotteries.discoverySources')}</div>
        <form onSubmit={createSource} className="form-grid discovery-form">
          <label>
            <span>{t('lotteries.platform')}</span>
            <select className="input" value={sourceForm.platform} onChange={e => setSourceForm({ ...sourceForm, platform: e.target.value })}>
              {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
            </select>
          </label>
          <label>
            <span>{t('lotteries.type')}</span>
            <select className="input" value={sourceForm.source_type} onChange={e => setSourceForm({ ...sourceForm, source_type: e.target.value })}>
              <option value="url_list">URL list</option>
              <option value="keyword">Keyword</option>
              <option value="up">Creator</option>
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
              onChange={e => setSourceForm({ ...sourceForm, source_value: e.target.value })}
              placeholder={sourceForm.source_type === 'up' ? t('lotteries.upUidPlaceholder') : t('lotteries.sourceValuePlaceholder')}
            />
          </label>
          <div className="toolbar form-actions">
            <button className="btn-primary" type="submit">{t('lotteries.saveSource')}</button>
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
                  <td><StatusBadge status={source.active ? 'ready' : 'pending'} /></td>
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
                const manualAssisted = isManualAssistedPlatform(lottery.platform);
                const platformModeBlocker = platformDispatchBlocker(lottery.platform, dispatchMode);
                const targetIssue = targetTransportCompatibilityIssue(lottery.platform, lottery.raw_url);
                const transportBlocksSelectedMode = Boolean(targetIssue && dispatchMode !== 'dry_run');
                const safeAccountAvailable = safeAccountCount(lottery.platform) > 0;
                const accountScopeReady = Boolean(
                  selectedSafeAccount && selectedSafeAccount.platform === lottery.platform
                );
                const accountScopeBlocked = dispatchMode !== 'dry_run' && !accountScopeReady;
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
                const repairAvailable = Boolean(repairPlan?.eligible);
                const repairBlocked = repairAvailable && (
                  manualAssisted
                  || Boolean(platformModeBlocker)
                  || Boolean(targetIssue)
                  || !gate?.allowed
                  || !safeAccountAvailable
                  || !accountScopeReady
                  || !actionPlanReady
                );
                const repairBlockReason = manualAssisted
                  ? t('lotteries.xiaohongshuManualOnlyHint')
                  : (targetIssue ? t('lotteries.legacyHttpTargetHint')
                  : (!accountScopeReady
                    ? t('lotteries.accountScopeRequired')
                    : (!actionPlanReady ? t('lotteries.actionPlanV2Required') : (repairBlocked ? gateTitle(gate, t) : ''))));
                const dispatchDisabled = Boolean(platformModeBlocker)
                  || transportBlocksSelectedMode
                  || !safeAccountAvailable
                  || accountScopeBlocked
                  || actionPlanBlocked
                  || (gateBlocked && !gateCanRunNext);
                let dispatchTitle = '';
                if (platformModeBlocker) dispatchTitle = dispatchBlockerMessage(platformModeBlocker, lottery);
                else if (transportBlocksSelectedMode) dispatchTitle = t('lotteries.legacyHttpTargetHint');
                else if (actionPlanBlocked) dispatchTitle = t('lotteries.actionPlanV2Required');
                else if (gateBlocked) dispatchTitle = gateTitle(gate, t);
                let dispatchLabel = t(`lotteries.dispatch_${dispatchMode}`);
                if (platformModeBlocker) dispatchLabel = t('lotteries.manualAssistedOnly');
                else if (transportBlocksSelectedMode) dispatchLabel = t('lotteries.compatibilityBlocked');
                else if (!safeAccountAvailable) dispatchLabel = t('lotteries.noSafeAccount');
                else if (accountScopeBlocked) dispatchLabel = t('lotteries.selectAccount');
                else if (actionPlanBlocked) dispatchLabel = t('lotteries.nextActions.review_rule');
                else if (gateBlocked) dispatchLabel = t(`lotteries.nextActions.${gate?.next_action || 'blocked'}`);
                return (
                  <tr key={lottery.id}>
                  <td className="mono">L{lottery.id}</td>
                  <td>{lottery.platform}</td>
                  <td className="truncate-cell" title={lottery.rule_text || lottery.raw_url}>
                    {lottery.title && <div className="table-primary">{lottery.title}</div>}
                    <div className="small-text">{lottery.raw_url}</div>
                    {targetIssue && <span className="badge badge-danger">{t('lotteries.legacyHttpTarget')}</span>}
                  </td>
                  <td><RulePlanEditor lottery={lottery} gate={gate} onSave={saveActionPlan} t={t} /></td>
                  <td><StatusBadge status={lottery.status} /></td>
                  <td>{lottery.value_score}</td>
                  <td>
                    <RealGateCell gate={gate} platform={lottery.platform} targetIssue={targetIssue} t={t} />
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
                      disabled={Boolean(targetIssue) || !safeAccountAvailable || !accountScopeReady || !actionPlanReady}
                      title={targetIssue
                        ? t('lotteries.legacyHttpTargetHint')
                        : (!accountScopeReady
                          ? t('lotteries.accountScopeRequired')
                          : (!actionPlanReady ? t('lotteries.actionPlanV2Required') : ''))}
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
                    <td>{summary ? `${summary.ready_phase_count}/4` : '-'}</td>
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
                        <a
                          className="badge badge-warn evidence-link"
                          href={authenticatedApiPath(`/lotteries/probes/${item.probe_id}/screenshot`)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {t('lotteries.openProbe')}
                        </a>
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
                      <a
                        className="badge badge-warn evidence-link"
                        href={authenticatedApiPath(`/lotteries/tasks/runs/${run.task_id}/screenshot`)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {t('lotteries.openEvidence')}
                      </a>
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
  return label === `lotteries.strategyReasons.${code}` ? code : label;
}

function gateBlockerText(code, t) {
  const label = t(`lotteries.realGateBlockers.${code}`);
  if (label !== `lotteries.realGateBlockers.${code}`) return label;
  return actionPlanBlockerText(code, t);
}

function actionPlanBlockerText(code, t) {
  const label = t(`lotteries.actionPlanBlockers.${code}`);
  return label === `lotteries.actionPlanBlockers.${code}` ? code : label;
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
  const code = targetValidationErrorCode(value);
  if (!code) return value;
  const key = `lotteries.targetErrors.${code}`;
  const translated = t(key);
  return translated === key ? value : translated;
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
  const account = risk.latest_event?.account_id ? `A${risk.latest_event.account_id}` : t('lotteries.account');
  return formatText(t('lotteries.riskCooldownUntil'), {
    account,
    time: displayTime(risk.cooldown_until),
  });
}

function RealGateCell({ gate, platform, targetIssue, t }) {
  if (targetIssue) {
    return (
      <div className="gate-cell">
        <span className="badge badge-danger">{t('lotteries.compatibilityBlocked')}</span>
        <div className="small-text warning-text">{t('lotteries.legacyHttpTargetHint')}</div>
        {!!gate?.blockers?.length && (
          <div className="blocker-list compact-blockers">
            {gate.blockers.slice(0, 3).map(code => (
              <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
            ))}
          </div>
        )}
      </div>
    );
  }
  if (isManualAssistedPlatform(platform)) {
    const shadowObservation = xiaohongshuShadowObservation(gate);
    return (
      <div className="gate-cell">
        <span className="badge badge-warn">{t('lotteries.manualAssistedOnly')}</span>
        <div className="small-text warning-text">{t('lotteries.xiaohongshuManualOnlyHint')}</div>
        <div className="capability-row">
          <span>{t('lotteries.shadowEvidence')}</span>
          <span className={`badge ${shadowObservation.complete ? 'badge-ready' : 'badge-muted'}`}>
            {shadowObservation.complete
              ? t('lotteries.shadowEvidenceReady')
              : t('lotteries.shadowEvidenceMissing')}
          </span>
        </div>
        {!!gate?.blockers?.length && (
          <div className="blocker-list compact-blockers">
            {gate.blockers.slice(0, 4).map(code => (
              <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
            ))}
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
      <div className="blocker-list compact-blockers">
        {gate.blockers?.slice(0, 3).map(code => <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>)}
      </div>
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
      <ActionLedgerSummary ledger={gate.action_ledger} repairPlan={repairPlan} t={t} />
    </div>
  );
}

function ExecutionEvidenceDetails({ gate, t }) {
  const evidence = executionEvidencePresentation(gate);
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
      {!evidence.bound && !!evidence.reasons.length && (
        <div className="blocker-list compact-blockers">
          {evidence.reasons.slice(0, 4).map(code => (
            <span className="badge badge-muted" key={code}>{gateBlockerText(code, t)}</span>
          ))}
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

function RulePlanEditor({ lottery, gate, onSave, t }) {
  const { notify } = useUi();
  const plan = lottery.action_plan || {};
  const manualAssisted = isManualAssistedPlatform(lottery.platform);
  const availableActions = lotteryActionsForPlatform(lottery.platform);
  const savedActions = Array.isArray(plan.required_actions) ? plan.required_actions : [];
  const initialActions = manualAssisted ? availableActions : savedActions;
  const savedPayloads = actionPayloadDraft(
    savedActions,
    plan.action_payloads,
    null,
    lottery.platform,
  );
  const initialPayloads = actionPayloadDraft(
    initialActions,
    plan.action_payloads,
    null,
    lottery.platform,
  );
  const planSignature = JSON.stringify({
    actions: savedActions,
    payloads: savedPayloads,
    contentRequirements: plan.content_requirements,
    version: plan.version,
    hash: plan.plan_hash,
  });
  const [actions, setActions] = useState(initialActions);
  const [payloads, setPayloads] = useState(initialPayloads);
  const [ruleText, setRuleText] = useState(lottery.rule_text || '');
  const [ruleCompleteConfirmed, setRuleCompleteConfirmed] = useState(false);
  const [reviewedConfirmed, setReviewedConfirmed] = useState(false);
  const [suggestion, setSuggestion] = useState(null);
  const [suggesting, setSuggesting] = useState(false);
  const suggestionActions = Array.isArray(suggestion?.required_actions) ? suggestion.required_actions : [];
  const draftPayloads = actionPayloadDraft(actions, payloads, null, lottery.platform);
  const semanticSource = suggestion || plan;
  const unresolvedRequirements = unresolvedRuleRequirements(semanticSource, draftPayloads);
  const payloadErrors = exactPayloadErrors(actions, draftPayloads);
  const executionPathId = platformExecutionPathId(lottery.platform, plan.execution_path_id);
  const planBlockers = [...new Set([
    ...(manualAssisted
      ? actionPlanV2ReviewBlockers(plan, lottery.platform)
      : actionPlanV2Blockers(plan, lottery.platform)),
    ...(Array.isArray(plan.payload_validation_errors) ? plan.payload_validation_errors : []),
    ...(Array.isArray(plan.capability_blockers) ? plan.capability_blockers : []),
  ])];
  const planReady = manualAssisted
    ? actionPlanV2ReviewReady(plan, lottery.platform)
    : actionPlanV2Ready(plan, lottery.platform);
  const draftChanged = !sameActionSet(actions, savedActions)
    || JSON.stringify(draftPayloads) !== JSON.stringify(savedPayloads);
  const missingSuggestedActions = suggestionActions.filter(action => !savedActions.includes(action));
  const sourceRuleLocked = Boolean(String(lottery.rule_text || '').trim());
  const discoveryManagedSource = sourceRuleCorrectionPath(lottery.platform, lottery.source_type) === 'discovery_refresh';
  const sourceRuleHelpId = `lottery-${lottery.id}-source-rule-help`;
  const mediaRequired = actionPlanHasMediaRequirement({ action_payloads: draftPayloads });
  const mediaRequirementNotice = t(manualAssisted
    ? 'lotteries.xiaohongshuMediaRequirementManual'
    : 'lotteries.mediaRuleStoredButUnsupported');
  const requiredActionSetComplete = !manualAssisted
    || (actions.length === availableActions.length && sameActionSet(actions, availableActions));
  const saveDisabled = !actions.length
    || !ruleText.trim()
    || !executionPathId
    || !ruleCompleteConfirmed
    || !reviewedConfirmed
    || !requiredActionSetComplete
    || unresolvedRequirements.length > 0
    || payloadErrors.length > 0;

  useEffect(() => {
    const persistedActions = Array.isArray(plan.required_actions) ? plan.required_actions : [];
    const nextActions = isManualAssistedPlatform(lottery.platform)
      ? lotteryActionsForPlatform(lottery.platform)
      : persistedActions;
    setActions(nextActions);
    setPayloads(actionPayloadDraft(nextActions, plan.action_payloads, null, lottery.platform));
    setRuleText(lottery.rule_text || '');
    setRuleCompleteConfirmed(false);
    setReviewedConfirmed(false);
    setSuggestion(null);
  }, [lottery.id, lottery.platform, lottery.rule_text, planSignature]);

  const toggle = action => {
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
    setPayloads(current => ({
      ...current,
      followed: { target_handle: value },
    }));
    setRuleCompleteConfirmed(false);
    setReviewedConfirmed(false);
  };

  const requestSuggestion = async () => {
    setSuggesting(true);
    try {
      const response = await fetchJSON(`/lotteries/${lottery.id}/action-plan/suggest?rule_text=${encodeURIComponent(ruleText)}`);
      const suggested = response.suggested_action_plan || {};
      const suggestedActions = Array.isArray(suggested.required_actions)
        ? suggested.required_actions
        : [];
      const nextActions = manualAssisted
        ? availableActions
        : availableActions.filter(action => suggestedActions.includes(action));
      setActions(nextActions);
      setPayloads(current => actionPayloadDraft(
        nextActions,
        current,
        suggested.content_requirements,
        lottery.platform,
      ));
      setRuleCompleteConfirmed(false);
      setReviewedConfirmed(false);
      setSuggestion(suggested);
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setSuggesting(false);
    }
  };

  return (
    <details className="rule-plan-editor">
      <summary>
        <span className={`badge ${planReady && !draftChanged ? 'badge-ready' : 'badge-warn'}`}>
          {planReady
            ? t(manualAssisted ? 'lotteries.manualRuleReady' : 'lotteries.ruleReady')
            : t('lotteries.ruleNeedsReview')}
        </span>
        <span className="small-text">{actionSummary(savedActions, t)}</span>
        {draftChanged && <span className="badge badge-warn">{t('lotteries.ruleDraftUnsavedBadge')}</span>}
      </summary>
      <div className="rule-plan-body">
        <div className="rule-plan-snapshots">
          <div className="rule-plan-snapshot">
            <span>{t(manualAssisted ? 'lotteries.manualSavedPlan' : 'lotteries.savedExecutionPlan')}</span>
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
        {manualAssisted && (
          <ManualAssistedChecklist plan={plan} gate={gate} platform={lottery.platform} t={t} />
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
        <textarea
          className="input textarea"
          value={ruleText}
          onChange={event => {
            setRuleText(event.target.value);
            setRuleCompleteConfirmed(false);
            setReviewedConfirmed(false);
            setSuggestion(null);
          }}
          readOnly={sourceRuleLocked}
          aria-readonly={sourceRuleLocked}
          aria-describedby={sourceRuleLocked ? sourceRuleHelpId : undefined}
          placeholder={t('lotteries.ruleTextPlaceholder')}
        />
        {sourceRuleLocked && (
          <div className="notice notice-warning small-text" id={sourceRuleHelpId} role="note">
            <div>{t('lotteries.sourceRuleReadOnly')}</div>
            <div>{t(discoveryManagedSource
              ? 'lotteries.sourceRuleDiscoveryCorrectionHint'
              : 'lotteries.sourceRuleCorrectionUnavailable')}</div>
          </div>
        )}
        <button className="btn-ghost" type="button" disabled={!ruleText.trim() || suggesting} onClick={requestSuggestion}>
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
                disabled={manualAssisted}
                onChange={() => toggle(action)}
              />
              <span>{t(`lotteries.actions.${action}`)}</span>
            </label>
          ))}
        </div>
        {manualAssisted && (
          <div className="small-text muted-text">{t('lotteries.xiaohongshuFourActionsFixed')}</div>
        )}

        {actions.includes('followed') && (
          <fieldset className="exact-payload-editor">
            <legend>{t('lotteries.followTarget')}</legend>
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
            <div className="small-text muted-text">{t('lotteries.followTargetHint')}</div>
          </fieldset>
        )}

        {actions.filter(action => ['commented', 'reposted'].includes(action)).map(action => {
          const payload = draftPayloads[action] || { text: '' };
          return (
            <fieldset className="exact-payload-editor" key={`payload-${action}`}>
              <legend>{t(`lotteries.exactPayloads.${action}`)}</legend>
              <label>
                <span>{t(manualAssisted ? 'lotteries.manualExactText' : 'lotteries.exactText')}</span>
                <textarea
                  className="input textarea"
                  value={payload.text || ''}
                  onChange={event => updateTextPayload(action, 'text', event.target.value)}
                  placeholder={t(`lotteries.exactTextPlaceholders.${action}`)}
                />
              </label>
              {['topic_tags', 'mentions', 'media_refs'].map(field => (
                <label key={field}>
                  <span>{t(`lotteries.payloadFields.${field}`)}</span>
                  <textarea
                    className="input textarea"
                    value={metadataLines(payload[field])}
                    onChange={event => updateTextPayload(action, field, event.target.value)}
                    placeholder={t('lotteries.onePerLine')}
                  />
                </label>
              ))}
              <label>
                <span>{t('lotteries.payloadFields.translation')}</span>
                <textarea
                  className="input textarea"
                  value={translationText(payload.translation)}
                  onChange={event => updateTextPayload(action, 'translation', event.target.value)}
                  placeholder={t('lotteries.translationPlaceholder')}
                />
              </label>
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
        {manualAssisted && !requiredActionSetComplete && (
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
            onChange={event => setRuleCompleteConfirmed(event.target.checked)}
          />
          <span>{t('lotteries.completeRuleConfirmation')}</span>
        </label>
        <label className="notice notice-warning">
          <input
            type="checkbox"
            checked={reviewedConfirmed}
            onChange={event => setReviewedConfirmed(event.target.checked)}
          />
          <span>{t(manualAssisted
            ? 'lotteries.manualReviewedPlanConfirmation'
            : 'lotteries.reviewedPlanConfirmation')}</span>
        </label>
        <button
          className="btn-primary"
          type="button"
          disabled={saveDisabled}
          title={saveDisabled ? t('lotteries.completeRuleBeforeSave') : ''}
          onClick={() => onSave(lottery, {
            requiredActions: actions,
            actionPayloads: draftPayloads,
            ruleText,
            ruleCompleteConfirmed,
            reviewed: reviewedConfirmed,
            executionPathId,
            platform: lottery.platform,
          })}
        >
          {t('lotteries.saveCurrentRule')}
        </button>
      </div>
    </details>
  );
}

function actionPayloadDraft(actions, payloads, contentRequirements = null, platform = 'bilibili') {
  const source = payloads && typeof payloads === 'object' ? payloads : {};
  const requirements = contentRequirements && typeof contentRequirements === 'object'
    ? contentRequirements
    : {};
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
        target_handle: followTargets.length === 1 ? followTargets[0] : sourceTarget,
      };
      return result;
    }
    if (!['commented', 'reposted'].includes(action)) {
      result[action] = {};
      return result;
    }
    const payload = source[action] && typeof source[action] === 'object' ? source[action] : {};
    result[action] = { text: typeof payload.text === 'string' ? payload.text : '' };
    for (const field of ['topic_tags', 'mentions', 'media_refs']) {
      const exactRequirement = requirements[action]?.[field];
      if (Array.isArray(exactRequirement)) {
        if (exactRequirement.length) result[action][field] = [...exactRequirement];
      } else if (Array.isArray(payload[field]) && payload[field].length) {
        result[action][field] = [...payload[field]];
      }
    }
    if (payload.translation !== undefined && payload.translation !== null && payload.translation !== '') {
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

function exactPayloadErrors(actions, payloads) {
  const errors = [];
  if (actions.includes('followed')) {
    const target = payloads.followed?.target_handle;
    if (typeof target !== 'string' || !/^@[\w\u4e00-\u9fff-]{1,64}$/u.test(target)) {
      errors.push('action_payload_followed_target_invalid');
    }
  }
  for (const action of actions.filter(item => ['commented', 'reposted'].includes(item))) {
    const payload = payloads[action] || {};
    const text = String(payload.text || '');
    if (!text.trim()) errors.push(`action_payload_${action}_text_required`);
    if (utf8ByteLength(text) > 4096) errors.push(`action_payload_${action}_text_too_large`);
    for (const field of ['topic_tags', 'mentions', 'media_refs']) {
      const values = Array.isArray(payload[field]) ? payload[field] : [];
      if (values.length > 32) errors.push(`action_payload_${field}_too_many`);
      if (values.some(item => utf8ByteLength(item) > 512)) {
        errors.push(`action_payload_${field}_invalid`);
      }
    }
    for (const token of [...(payload.topic_tags || []), ...(payload.mentions || [])]) {
      if (!text.includes(token)) errors.push('action_payload_required_token_missing');
    }
    if (payload.translation !== undefined && payload.translation !== '') {
      if (typeof payload.translation !== 'string' || !payload.translation.trim()) {
        errors.push('action_payload_translation_invalid');
      } else if (!text.includes(payload.translation)) {
        errors.push('action_payload_translation_missing');
      }
    }
  }
  return [...new Set(errors)];
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(String(value || '')).length;
}

function ManualAssistedChecklist({ plan, gate, platform, t }) {
  const items = xiaohongshuManualChecklist(plan, platform);
  const shadowObservation = xiaohongshuShadowObservation(gate);
  return (
    <section className="manual-assisted-checklist" aria-label={t('lotteries.manualChecklistTitle')}>
      <div className="capability-row">
        <strong>{t('lotteries.manualChecklistTitle')}</strong>
        <span className="badge badge-warn">{t('lotteries.manualAssistedOnly')}</span>
      </div>
      <p className="small-text muted-text">{t('lotteries.manualChecklistHint')}</p>
      <ol>
        {items.map(item => (
          <li key={item.action}>
            <span className={`badge ${item.required ? 'badge-ready' : 'badge-danger'}`}>
              {item.required ? t('lotteries.planIncluded') : t('lotteries.planMissing')}
            </span>
            <strong>{t(`lotteries.actions.${item.action}`)}</strong>
            {item.exactValue && <span className="small-text manual-exact-value">{item.exactValue}</span>}
          </li>
        ))}
      </ol>
      <div className="small-text mono">
        {t('lotteries.shadowEvidence')}: {shadowObservation.complete
          ? t('lotteries.shadowEvidenceReady')
          : t('lotteries.shadowEvidenceMissing')}
        {shadowObservation.taskId ? ` / ${shortIdentity(shadowObservation.taskId)}` : ''}
      </div>
      <p className="small-text warning-text">{t('lotteries.manualChecklistNoMutation')}</p>
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
