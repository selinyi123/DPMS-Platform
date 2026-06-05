import { useEffect, useState } from 'react';

import { apiPath, deleteJSON, fetchJSON, getConfirmedHeaders, postJSON, putJSON } from '../api';
import { useUi } from '../uiContext';

export default function Deploy() {
  const { notify: toast, t } = useUi();
  const [version] = useState('v3.0.2-local');
  const [message, setMessage] = useState('');
  const [reloadArmed, setReloadArmed] = useState(false);
  const [uploadSignature, setUploadSignature] = useState('');
  const [channels, setChannels] = useState([]);
  const [notifyStatus, setNotifyStatus] = useState(null);
  const [notifyGuide, setNotifyGuide] = useState(null);
  const [secretDrafts, setSecretDrafts] = useState({});
  const [logs, setLogs] = useState([]);
  const [probes, setProbes] = useState([]);
  const [adapterConfig, setAdapterConfig] = useState(null);
  const [runtimeSettings, setRuntimeSettings] = useState(null);
  const [realRunArmed, setRealRunArmed] = useState(false);
  const [rollbackArmed, setRollbackArmed] = useState(false);
  const [rollbackReason, setRollbackReason] = useState('manual rollback');
  const [selectorJson, setSelectorJson] = useState(defaultSelectorJson);
  const [selectorB64, setSelectorB64] = useState('');
  const [notify, setNotify] = useState({ channel: 'serverchan', title: 'DPMS test', content: 'Notification channel test' });

  const loadNotify = async () => {
    const [channelRows, statusRows, guideRows, logRows, adapterRows, probeRows, runtimeRows] = await Promise.all([
      fetchJSON('/notify/channels'),
      fetchJSON('/notify/status'),
      fetchJSON('/notify/config-guide'),
      fetchJSON('/notify/logs'),
      fetchJSON('/lotteries/adapters/config'),
      fetchJSON('/lotteries/probes'),
      fetchJSON('/metrics/runtime/settings'),
    ]);
    setChannels(channelRows);
    setNotifyStatus(statusRows);
    setNotifyGuide(guideRows);
    setLogs(logRows);
    setAdapterConfig(adapterRows);
    setProbes(probeRows);
    setRuntimeSettings(runtimeRows);
  };

  useEffect(() => {
    loadNotify().catch((err) => {
      const text = formatText(t('deploy.loadFailed'), { message: err.message });
      setMessage(text);
      toast(text, 'error');
    });
  }, [t, toast]);

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

  const probeCandidates = probes
    .map(probe => ({ probe, draft: buildDraftFromProbe(probe) }))
    .filter(item => item.draft);

  const useProbeDraft = (item) => {
    setSelectorJson(JSON.stringify(item.draft, null, 2));
    setSelectorB64('');
    const text = formatText(t('deploy.draftLoaded'), { probe: item.probe.probe_id?.slice(0, 8) });
    setMessage(text);
    toast(text, 'success');
  };

  const selectedChannel = channels.find(channel => channel.id === notify.channel);
  const selectedChannelConfigured = Boolean(selectedChannel?.configured);
  const toggleRealRun = async (enabled) => {
    try {
      await putJSON('/metrics/runtime/settings/real-run', { enabled }, { confirm: true });
      setRealRunArmed(false);
      const text = enabled ? 'Real-run enabled' : 'Real-run disabled';
      setMessage(text);
      toast(text, enabled ? 'warning' : 'success');
      await loadNotify();
    } catch (err) {
      setMessage(err.message);
      toast(err.message, 'error');
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
            <span>Real-run</span>
            <span className={`badge ${runtimeSettings?.real_run_enabled ? 'badge-danger' : 'badge-muted'}`}>
              {runtimeSettings?.real_run_enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          <div className="version-row">
            <span>{t('deploy.globalBreaker')}</span>
            <span className={`badge ${runtimeSettings?.global_circuit_breaker?.status === 'open' ? 'badge-danger' : 'badge-ready'}`}>
              {runtimeSettings?.global_circuit_breaker?.status || 'closed'}
            </span>
          </div>
          {runtimeSettings?.global_circuit_breaker?.reason && (
            <p className="muted-text tight-text">{runtimeSettings.global_circuit_breaker.reason}</p>
          )}
          <label className="check-row">
            <input type="checkbox" checked={realRunArmed} onChange={e => setRealRunArmed(e.target.checked)} />
            <span>Confirm real-run switch change</span>
          </label>
          <div className="toolbar">
            <button
              className={runtimeSettings?.real_run_enabled ? 'btn-ghost' : 'btn-danger'}
              type="button"
              disabled={!realRunArmed}
              onClick={() => toggleRealRun(!runtimeSettings?.real_run_enabled)}
            >
              {runtimeSettings?.real_run_enabled ? 'Disable real-run' : 'Enable real-run'}
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
              <input className="input" type="password" value={uploadSignature} onChange={e => setUploadSignature(e.target.value)} autoComplete="off" />
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
        <div className="panel-title">{t('deploy.notificationHealth')}</div>
        <div className="notify-health-grid">
          {notifyStatus?.channels?.map(channel => (
            <div className="notify-health-row" key={channel.id}>
              <div>
                <div className="mono">{channel.label}</div>
                <div className="small-text muted-text">
                  {channel.configured ? t('deploy.configured') : t('deploy.missingSecrets')}
                  {channel.last_log ? ` / ${t('deploy.last')}: ${channel.last_log.success ? t('deploy.sent') : t('deploy.failed')}` : ` / ${t('deploy.noLogs')}`}
                </div>
                {channel.last_error && <div className="small-text notify-error">{channel.last_error}</div>}
              </div>
              <span className={`badge ${channel.healthy ? 'badge-ready' : channel.configured ? 'badge-warn' : 'badge-muted'}`}>
                {channel.healthy ? t('deploy.healthy') : channel.configured ? t('deploy.needsCheck') : t('deploy.notSet')}
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
          {notifyGuide?.apply_steps?.map(step => <div className="ops-check-item" key={step}>{step}</div>)}
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
                  <span className={`badge ${item.configured ? 'badge-ready' : 'badge-muted'}`}>{item.configured ? t('deploy.configured') : t('deploy.planned')}</span>
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
                      <td>{probeReadyPhaseCount(item.probe)}/4</td>
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
                  <td><span className={`badge ${log.success ? 'badge-ready' : 'badge-danger'}`}>{log.success ? t('deploy.sent') : t('deploy.failed')}</span></td>
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
  for (const phase of ['followed', 'liked', 'commented', 'reposted']) {
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

function safeJson(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function formatText(template, values) {
  return Object.entries(values).reduce((text, [key, value]) => text.replaceAll(`{${key}}`, value), template);
}

const secretFieldByEnv = {
  SERVERCHAN_KEY: 'serverchan_key',
  FEISHU_WEBHOOK: 'feishu_webhook',
  GENERIC_WEBHOOK_URL: 'generic_webhook_url',
  TELEGRAM_BOT_TOKEN: 'telegram_bot_token',
  TELEGRAM_CHAT_ID: 'telegram_chat_id',
};
