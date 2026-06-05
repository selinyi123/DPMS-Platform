import { useEffect, useMemo, useState } from 'react';

import { apiPath, fetchJSON, postJSON, putJSON } from '../api';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

export default function Lotteries() {
  const { notify, t } = useUi();
  const [lotteries, setLotteries] = useState([]);
  const [runs, setRuns] = useState([]);
  const [probes, setProbes] = useState([]);
  const [sources, setSources] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [platforms, setPlatforms] = useState([]);
  const [adapters, setAdapters] = useState([]);
  const [error, setError] = useState('');
  const [discoveryMessage, setDiscoveryMessage] = useState('');
  const [dispatchMode, setDispatchMode] = useState('dry_run');
  const [selectedAccount, setSelectedAccount] = useState('');
  const [form, setForm] = useState({ platform: 'bilibili', raw_url: 'https://www.bilibili.com/', value_score: 50 });
  const [targetImport, setTargetImport] = useState({ platform: 'bilibili', content: '', value_score: 50 });
  const [targetImportResult, setTargetImportResult] = useState(null);
  const [sourceForm, setSourceForm] = useState({
    platform: 'bilibili',
    source_type: 'url_list',
    source_value: 'https://www.bilibili.com/',
    scan_interval_minutes: 30,
  });

  const platformById = useMemo(
    () => Object.fromEntries(platforms.map(platform => [platform.id, platform])),
    [platforms],
  );

  const adapterById = useMemo(
    () => Object.fromEntries(adapters.map(adapter => [adapter.platform, adapter])),
    [adapters],
  );

  const safeAccounts = useMemo(
    () => accounts.filter(account => account.status === 'ready' && account.credential_ready && account.latest_calibration?.status === 'succeeded'),
    [accounts],
  );

  const safeAccountCount = platformId => safeAccounts.filter(account => account.platform === platformId).length;
  const selectedSafeAccount = safeAccounts.find(account => String(account.id) === String(selectedAccount));

  const load = async () => {
    try {
      const [lotteryRows, runRows, probeRows, sourceRows, accountRows, platformRows, adapterRows] = await Promise.all([
        fetchJSON('/lotteries/'),
        fetchJSON('/lotteries/tasks/runs'),
        fetchJSON('/lotteries/probes'),
        fetchJSON('/lotteries/sources'),
        fetchJSON('/accounts/'),
        fetchJSON('/accounts/platforms'),
        fetchJSON('/lotteries/adapters'),
      ]);
      setLotteries(lotteryRows);
      setRuns(runRows);
      setProbes(probeRows);
      setSources(sourceRows);
      setAccounts(accountRows);
      setPlatforms(platformRows);
      setAdapters(adapterRows);
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

  const dispatch = async (id) => {
    setError('');
    try {
      const lottery = lotteries.find(item => item.id === id);
      const selectedMatches = selectedSafeAccount && lottery && selectedSafeAccount.platform === lottery.platform;
      await postJSON(`/lotteries/${id}/dispatch`, {
        mode: dispatchMode,
        dry_run: dispatchMode !== 'real_run',
        confirm: dispatchMode === 'real_run',
        account_id: selectedMatches ? Number(selectedAccount) : null,
      }, { confirm: dispatchMode === 'real_run' });
      notify(t('lotteries.dispatchQueued'), 'success');
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

  const realActionReady = platformId => Boolean(platformById[platformId]?.action_adapter);

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
        </div>
      </header>

      <div className="ops-grid three-columns">
        {platforms.map(platform => (
          <div className="panel platform-card" key={platform.id}>
            <div className="panel-kicker">{platform.id}</div>
            <div className="panel-title">{platform.label}</div>
            <div className="capability-row"><span>{t('lotteries.qrLogin')}</span><StatusBadge status={platform.qr_login ? 'ready' : 'failed'} /></div>
            <div className="capability-row"><span>{t('lotteries.cookieLogin')}</span><StatusBadge status={platform.cookie_login ? 'ready' : 'failed'} /></div>
            <div className="capability-row">
              <span>{t('lotteries.realActions')}</span>
              <StatusBadge status={platform.action_adapter ? 'gray' : 'pending'} />
            </div>
            <div className="capability-row"><span>{t('lotteries.adapter')}</span><span className="mono small-text">{platform.adapter_status || 'planned'}</span></div>
            <div className="capability-row"><span>{t('lotteries.safeAccounts')}</span><span className="mono small-text">{safeAccountCount(platform.id)}</span></div>
            <div className="capability-row"><span>{t('lotteries.phases')}</span><span className="mono small-text">{adapterById[platform.id]?.phases?.length || 0}</span></div>
            <p className="muted-text tight-text">{adapterById[platform.id]?.notes || t('lotteries.adapterLoading')}</p>
            <div className="capability-row"><span>{t('lotteries.cookieDomain')}</span><span className="mono small-text">{platform.cookie_domain}</span></div>
          </div>
        ))}
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
            <textarea className="input textarea" value={sourceForm.source_value} onChange={e => setSourceForm({ ...sourceForm, source_value: e.target.value })} />
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
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>{t('lotteries.platform')}</th><th>{t('lotteries.url')}</th><th>{t('lotteries.status')}</th><th>{t('lotteries.score')}</th><th>{t('lotteries.safeAccounts')}</th><th>{t('lotteries.expires')}</th><th>{t('lotteries.action')}</th></tr>
            </thead>
            <tbody>
              {lotteries.map(lottery => (
                <tr key={lottery.id}>
                  <td className="mono">L{lottery.id}</td>
                  <td>{lottery.platform}</td>
                  <td className="truncate-cell" title={lottery.raw_url}>{lottery.raw_url}</td>
                  <td><StatusBadge status={lottery.status} /></td>
                  <td>{lottery.value_score}</td>
                  <td>{safeAccountCount(lottery.platform)}</td>
                  <td className="small-text">{lottery.expires_at || '-'}</td>
                  <td className="action-cell">
                    <button
                      onClick={() => dispatch(lottery.id)}
                      disabled={!safeAccountCount(lottery.platform) || (dispatchMode === 'real_run' && !realActionReady(lottery.platform))}
                      title={dispatchMode === 'real_run' && !realActionReady(lottery.platform) ? t('lotteries.realAdapterMissing') : ''}
                      className={dispatchMode === 'real_run' ? 'btn-danger' : 'btn-primary'}
                    >
                      {!safeAccountCount(lottery.platform)
                        ? t('lotteries.noSafeAccount')
                        : (dispatchMode === 'real_run' && !realActionReady(lottery.platform)
                            ? t('lotteries.adapterPending')
                            : t(`lotteries.dispatch_${dispatchMode}`))}
                    </button>
                    <button onClick={() => markResult(lottery.id, 'won')} className="btn-ghost">{t('lotteries.won')}</button>
                    <button onClick={() => markResult(lottery.id, 'lost')} className="btn-ghost">{t('lotteries.lost')}</button>
                    <button onClick={() => probe(lottery.id)} disabled={!safeAccountCount(lottery.platform)} className="btn-ghost">{t('lotteries.probe')}</button>
                  </td>
                </tr>
              ))}
              {!lotteries.length && <tr><td className="empty-cell" colSpan="8">{t('lotteries.noActivities')}</td></tr>}
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
                          href={apiPath(`/lotteries/probes/${item.probe_id}/screenshot`)}
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
                        href={apiPath(`/lotteries/tasks/runs/${run.task_id}/screenshot`)}
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

function formatText(template, values) {
  return String(template).replace(/\{(\w+)\}/g, (_, key) => values?.[key] ?? '');
}

function modeLabel(run, t) {
  const mode = run.task_mode || (run.dry_run ? 'dry_run' : 'real_run');
  const label = t(`lotteries.${mode}`);
  return label === `lotteries.${mode}` ? mode : label;
}
