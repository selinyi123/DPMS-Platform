import { useEffect, useMemo, useState } from 'react';

import { authenticatedApiPath, fetchJSON, postJSON, putJSON } from '../api';
import StatusBadge from '../components/StatusBadge';
import { formatText } from '../i18n/format';
import { useUi } from '../uiContext';

const LOTTERY_ACTIONS = ['followed', 'liked', 'commented', 'reposted'];
const API_ACTION_PHASES = {
  follow: 'followed',
  like: 'liked',
  comment: 'commented',
  repost: 'reposted',
};

export default function Lotteries() {
  const { notify, t } = useUi();
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

  const load = async () => {
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
        fetchJSON('/lotteries/real-run/evidence'),
      ]);
      setLotteries(lotteryRows);
      setRuns(runRows);
      setProbes(probeRows);
      setSources(sourceRows);
      setStrategyQueue(strategyRows.items || []);
      setAccounts(accountRows);
      setPlatforms(platformRows);
      setAdapters(adapterRows);
      setReadiness(readinessRows);
      setRealRunEvidence(evidenceRows.items || []);
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
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
      setError(err.message);
      notify(err.message, 'error');
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

  const saveActionPlan = async (lottery, requiredActions, ruleText) => {
    setError('');
    try {
      await putJSON(`/lotteries/${lottery.id}/action-plan`, {
        required_actions: requiredActions,
        rule_text: ruleText,
        reviewed: true,
      });
      notify(t('lotteries.ruleSaved'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const dispatch = async (id, modeOverride = dispatchMode) => {
    setError('');
    try {
      const lottery = lotteries.find(item => item.id === id);
      const mode = modeOverride || dispatchMode;
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
          <select className="input compact-input" value={selectedAccount} onChange={e => setSelectedAccount(e.target.value)}>
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
            <div className="capability-row"><span>{t('lotteries.probe')}</span><span className="mono small-text">{ready?.latest_probe ? `${ready.latest_probe.ready_phase_count || 0}/4` : '-'}</span></div>
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
              {strategyQueue.map(item => (
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
                      disabled={item.recommended_mode === 'blocked'}
                      onClick={() => dispatch(item.lottery_id, item.recommended_mode)}
                    >
                      {t('lotteries.dispatchRecommended')}
                    </button>
                  </td>
                </tr>
              ))}
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
                {targetImportResult.invalid.slice(0, 3).map(item => `#${item.line}: ${item.error}`).join(' / ')}
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
                const gateBlocked = dispatchMode === 'real_run' && !gate?.allowed;
                const gateCanRunNext = gateBlocked && ['probe', 'shadow_run', 'real_run'].includes(gate?.next_action);
                const repairAvailable = Boolean(repairPlan?.eligible);
                const repairBlocked = repairAvailable && (!gate?.allowed || !safeAccountCount(lottery.platform));
                const repairBlockReason = repairBlocked ? gateTitle(gate, t) : '';
                return (
                  <tr key={lottery.id}>
                  <td className="mono">L{lottery.id}</td>
                  <td>{lottery.platform}</td>
                  <td className="truncate-cell" title={lottery.rule_text || lottery.raw_url}>
                    {lottery.title && <div className="table-primary">{lottery.title}</div>}
                    <div className="small-text">{lottery.raw_url}</div>
                  </td>
                  <td><RulePlanEditor lottery={lottery} onSave={saveActionPlan} t={t} /></td>
                  <td><StatusBadge status={lottery.status} /></td>
                  <td>{lottery.value_score}</td>
                  <td>
                    <RealGateCell gate={gate} t={t} />
                  </td>
                  <td className="small-text">{lottery.expires_at || '-'}</td>
                  <td className="action-cell">
                    <button
                      onClick={() => gateBlocked ? runGateNextAction(lottery, gate) : dispatch(lottery.id)}
                      disabled={!safeAccountCount(lottery.platform) || (gateBlocked && !gateCanRunNext)}
                      title={gateBlocked ? gateTitle(gate, t) : ''}
                      className={dispatchMode === 'real_run' && !gateCanRunNext ? 'btn-danger' : 'btn-primary'}
                    >
                      {!safeAccountCount(lottery.platform)
                        ? t('lotteries.noSafeAccount')
                        : (gateBlocked
                            ? t(`lotteries.nextActions.${gate?.next_action || 'blocked'}`)
                            : t(`lotteries.dispatch_${dispatchMode}`))}
                    </button>
                    <button onClick={() => markResult(lottery.id, 'won')} className="btn-ghost">{t('lotteries.won')}</button>
                    <button onClick={() => markResult(lottery.id, 'lost')} className="btn-ghost">{t('lotteries.lost')}</button>
                    <button onClick={() => probe(lottery.id)} disabled={!safeAccountCount(lottery.platform)} className="btn-ghost">{t('lotteries.probe')}</button>
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
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>{t('lotteries.probe')}</th><th>{t('lotteries.platform')}</th><th>{t('lotteries.account')}</th><th>{t('lotteries.activity')}</th><th>{t('lotteries.status')}</th><th>{t('lotteries.visiblePhases')}</th><th>{t('lotteries.quality')}</th><th>{t('lotteries.evidence')}</th></tr>
            </thead>
            <tbody>
              {probes.map(item => {
                const summary = probeSummary(item);
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
                        <StatusBadge status={summary.ready_for_real_actions ? 'ready' : 'pending'} />
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
  return label === `lotteries.realGateBlockers.${code}` ? code : label;
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

function RealGateCell({ gate, t }) {
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

function RulePlanEditor({ lottery, onSave, t }) {
  const { notify } = useUi();
  const plan = lottery.action_plan || {};
  const initialActions = Array.isArray(plan.required_actions) ? plan.required_actions : [];
  const planSignature = JSON.stringify(initialActions);
  const [actions, setActions] = useState(initialActions);
  const [ruleText, setRuleText] = useState(lottery.rule_text || '');
  const [suggestion, setSuggestion] = useState(null);
  const [suggesting, setSuggesting] = useState(false);
  const suggestionActions = Array.isArray(suggestion?.required_actions) ? suggestion.required_actions : [];
  const draftChanged = !sameActionSet(actions, initialActions);
  const missingSuggestedActions = suggestionActions.filter(action => !initialActions.includes(action));

  useEffect(() => {
    setActions(Array.isArray(plan.required_actions) ? plan.required_actions : []);
    setRuleText(lottery.rule_text || '');
    setSuggestion(null);
  }, [lottery.id, lottery.rule_text, planSignature]);

  const toggle = action => {
    setActions(current => current.includes(action)
      ? current.filter(item => item !== action)
      : [...current, action]);
  };

  const requestSuggestion = async () => {
    setSuggesting(true);
    try {
      const response = await fetchJSON(`/lotteries/${lottery.id}/action-plan/suggest?rule_text=${encodeURIComponent(ruleText)}`);
      const suggested = response.suggested_action_plan || {};
      setActions(Array.isArray(suggested.required_actions) ? suggested.required_actions : []);
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
        <span className={`badge ${plan.review_required || !initialActions.length || draftChanged ? 'badge-warn' : 'badge-ready'}`}>
          {plan.review_required || !initialActions.length ? t('lotteries.ruleNeedsReview') : t('lotteries.ruleReady')}
        </span>
        <span className="small-text">{actionSummary(initialActions, t)}</span>
        {draftChanged && <span className="badge badge-warn">{t('lotteries.ruleDraftUnsavedBadge')}</span>}
      </summary>
      <div className="rule-plan-body">
        <div className="rule-plan-snapshots">
          <div className="rule-plan-snapshot">
            <span>{t('lotteries.savedExecutionPlan')}</span>
            <strong>{actionSummary(initialActions, t)}</strong>
          </div>
          <div className={`rule-plan-snapshot ${draftChanged ? 'is-dirty' : ''}`}>
            <span>{t('lotteries.draftExecutionPlan')}</span>
            <strong>{actionSummary(actions, t)}</strong>
          </div>
          {!!suggestionActions.length && (
            <div className={`rule-plan-snapshot ${missingSuggestedActions.length ? 'is-dirty' : ''}`}>
              <span>{t('lotteries.suggestedExecutionPlan')}</span>
              <strong>{actionSummary(suggestionActions, t)}</strong>
            </div>
          )}
        </div>
        {draftChanged && <div className="notice notice-warning">{t('lotteries.ruleDraftNotSaved')}</div>}
        {!!missingSuggestedActions.length && (
          <div className="notice notice-warning">
            {formatText(t('lotteries.suggestedActionsMissingSaved'), { actions: actionSummary(missingSuggestedActions, t) })}
          </div>
        )}
        <textarea
          className="input textarea"
          value={ruleText}
          onChange={event => setRuleText(event.target.value)}
          placeholder={t('lotteries.ruleTextPlaceholder')}
        />
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
                {t('lotteries.suggestionUnsupportedPrefix')}{t(`lotteries.unsupportedActions.${action}`)}
              </div>
            ))}
          </div>
        )}
        <div className="rule-action-grid">
          {LOTTERY_ACTIONS.map(action => (
            <label key={action}>
              <input type="checkbox" checked={actions.includes(action)} onChange={() => toggle(action)} />
              <span>{t(`lotteries.actions.${action}`)}</span>
            </label>
          ))}
        </div>
        <button className="btn-primary" type="button" disabled={!actions.length} onClick={() => onSave(lottery, actions, ruleText)}>
          {t('lotteries.saveCurrentRule')}
        </button>
      </div>
    </details>
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
