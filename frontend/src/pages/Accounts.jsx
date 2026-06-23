import { useEffect, useMemo, useState } from 'react';
import QRCode from 'qrcode';

import { authenticatedApiPath, deleteJSON, fetchJSON, postJSON, putJSON } from '../api';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

export default function Accounts() {
  const { language, notify, t } = useUi();
  const [accounts, setAccounts] = useState([]);
  const [platforms, setPlatforms] = useState([]);
  const [proxies, setProxies] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ platform: 'bilibili', encrypted_credential: '' });
  const [credentialDrafts, setCredentialDrafts] = useState({});
  const [proxyDrafts, setProxyDrafts] = useState({});
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [loginSession, setLoginSession] = useState(null);
  const [imageReady, setImageReady] = useState(false);
  const [qrDataUrl, setQrDataUrl] = useState('');
  const [recheckResult, setRecheckResult] = useState(null);
  const text = accountText[language] || accountText.zh;

  const selectedPlatform = useMemo(
    () => platforms.find(platform => platform.id === form.platform),
    [platforms, form.platform],
  );

  const load = async () => {
    try {
      const [accountRows, platformRows, proxyRows] = await Promise.all([
        fetchJSON('/accounts/'),
        fetchJSON('/accounts/platforms'),
        fetchJSON('/proxies/'),
      ]);
      setAccounts(accountRows);
      setPlatforms(platformRows);
      setProxies(proxyRows);
    } catch (err) {
      setError(err.message);
    }
  };

  useEffect(() => { load(); }, []);

  useEffect(() => {
    if (!loginSession?.session_id || ['confirmed', 'expired', 'failed'].includes(loginSession.status)) return undefined;
    let cancelled = false;
    let timer;
    const poll = async () => {
      try {
        const next = loginSession.login_mode === 'official_qr'
          ? await postJSON(`/accounts/login/qr/${loginSession.session_id}/poll`, {})
          : await fetchJSON(`/accounts/login/qr/${loginSession.session_id}`);
        if (cancelled) return;
        setLoginSession(next);
        if (next.status === 'confirmed') await load();
      } catch (err) {
        if (cancelled) return;
        setError(err.message);
        setLoginSession(current => (
          current?.session_id === loginSession.session_id
            ? { ...current, status: 'failed', error_message: err.message }
            : current
        ));
        return;
      }
      if (!cancelled) timer = window.setTimeout(poll, 2500);
    };
    timer = window.setTimeout(poll, 2500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [loginSession?.session_id, loginSession?.status]);

  useEffect(() => {
    let active = true;
    if (loginSession?.login_mode !== 'official_qr' || !loginSession.qr_content) {
      setQrDataUrl('');
      return undefined;
    }
    QRCode.toDataURL(loginSession.qr_content, {
      errorCorrectionLevel: 'M',
      margin: 2,
      width: 320,
      color: { dark: '#101828', light: '#ffffff' },
    })
      .then(url => {
        if (active) setQrDataUrl(url);
      })
      .catch(err => {
        if (active) setError(err.message);
      });
    return () => { active = false; };
  }, [loginSession?.login_mode, loginSession?.qr_content]);

  const startQrLogin = async () => {
    setBusy(true);
    setError('');
    setImageReady(false);
    setQrDataUrl('');
    try {
      const session = await postJSON('/accounts/login/qr', { platform: form.platform });
      setLoginSession(session);
      notify(`${t('accounts.qrLogin')} ${t(`status.${session.status}`)}`, 'info');
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const createAccount = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await postJSON('/accounts/', form);
      setForm({ ...form, encrypted_credential: '' });
      notify(t('accounts.importQueued'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const saveCredential = async (accountId) => {
    const value = credentialDrafts[accountId] || '';
    setBusy(true);
    setError('');
    try {
      await putJSON(`/accounts/${accountId}/credential`, { encrypted_credential: value });
      setCredentialDrafts(prev => ({ ...prev, [accountId]: '' }));
      notify(t('accounts.credentialSaved'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleAction = async (account, target) => {
    setError('');
    try {
      await putJSON(`/accounts/${account.id}/status`, { target, version: account.version });
      notify(t('accounts.statusUpdated'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const calibrateAccount = async (account) => {
    setBusy(true);
    setError('');
    try {
      await postJSON(`/accounts/${account.id}/calibrate`, { force: false });
      notify(t('accounts.calibrationQueued'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const recheckHealth = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await postJSON('/accounts/health/recheck', { cooldown_minutes: 15, stale_execution_minutes: 10 });
      setRecheckResult(result);
      notify(t('accounts.healthDone'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const saveProxy = async (account) => {
    const draftValue = proxyDrafts[account.id];
    const proxyId = draftValue === undefined ? account.proxy_id : draftValue;
    setBusy(true);
    setError('');
    try {
      await putJSON(`/accounts/${account.id}/proxy`, { proxy_id: proxyId ? Number(proxyId) : null });
      notify(proxyId ? text.proxyAssigned : text.proxyCleared, 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const deleteAccount = async (account) => {
    setBusy(true);
    setError('');
    try {
      await deleteJSON(`/accounts/${account.id}`, { confirm: true });
      notify(t('accounts.deleted'), 'success');
      setDeleteTarget(null);
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const renderRiskSummary = (account) => {
    const event = account.latest_risk_event;
    if (!event) return <span className="muted-text">{t('accounts.noRisk')}</span>;
    const detail = typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail || {});
    const eventLabel = t(`status.${event.event_type}`) === `status.${event.event_type}` ? event.event_type : t(`status.${event.event_type}`);
    return (
      <div className="risk-summary">
        <span className="badge badge-warn">{eventLabel}</span>
        <span className="mono small-text">{detail}</span>
      </div>
    );
  };

  const canMarkReady = account => account.status === 'cooling' && account.credential_ready;
  const canMarkCooling = account => ['ready', 'warming', 'executing'].includes(account.status);
  const canFreeze = account => !['frozen', 'banned'].includes(account.status);
  const canCalibrate = account => account.credential_ready && !['executing', 'banned'].includes(account.status);
  const proxyOptionsFor = account => proxies.filter(proxy => (
    !proxy.assigned_account || proxy.assigned_account.id === account.id
  ));

  const renderCalibration = (account) => {
    const calibration = account.latest_calibration;
    if (!calibration) return <span className="badge badge-muted">{t('accounts.notChecked')}</span>;
    return (
      <div className="calibration-summary">
        <StatusBadge status={calibration.status} />
        {calibration.screenshot_path && calibration.calibration_id && (
          <a
            className="badge badge-info evidence-link"
            href={authenticatedApiPath(`/accounts/calibrations/${calibration.calibration_id}/screenshot`)}
            target="_blank"
            rel="noreferrer"
          >
            {t('accounts.evidence')}
          </a>
        )}
        {calibration.error_message && <span className="mono small-text">{calibration.error_message}</span>}
      </div>
    );
  };

  const renderRuntime = (account) => {
    const current = account.current_task_run;
    const latest = account.latest_task_run;
    if (current) {
      return (
        <div className="runtime-summary">
          <StatusBadge status={current.status} />
          <span className="mono small-text">T{String(current.task_id || '').slice(0, 8)} / L{current.lottery_id}</span>
          <span className="small-text">{modeLabel(current, t)}</span>
        </div>
      );
    }
    if (latest) {
      return (
        <div className="runtime-summary">
          <StatusBadge status={latest.status} />
          <span className="mono small-text">T{String(latest.task_id || '').slice(0, 8)} / L{latest.lottery_id}</span>
          <span className="small-text">{latest.finished_at || latest.started_at || '-'}</span>
        </div>
      );
    }
    return <span className="badge badge-muted">{t('lotteries.noRuns')}</span>;
  };

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('accounts.eyebrow')}</p>
          <h1>{t('accounts.title')}</h1>
        </div>
        <div className="toolbar">
          <button className="btn-ghost" disabled={busy} onClick={recheckHealth}>{t('accounts.healthRecheck')}</button>
          <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
        </div>
      </header>

      {recheckResult && (
        <div className="notice inline-status">
          <span>{text.restoredCooling}: {recheckResult.cooling_accounts_restored}</span>
          <span>{text.missingCredentialMoved}: {recheckResult.ready_without_credential_moved_to_login_required}</span>
          <span>{text.staleExecutingCooled}: {recheckResult.stale_executing_moved_to_cooling}</span>
        </div>
      )}

      <div className="ops-grid two-columns">
        <div className="panel login-panel">
          <div className="panel-kicker">{t('accounts.qrLogin')}</div>
          <div className="panel-title">{t('accounts.qrBroker')}</div>
          <div className="form-row">
            <label>
              <span>{t('accounts.platform')}</span>
              <select className="input" value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value })}>
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <button className="btn-primary" disabled={busy || !selectedPlatform?.qr_login} onClick={startQrLogin}>{t('accounts.generateQr')}</button>
          </div>
          <div className="qr-frame">
            {loginSession?.session_id ? (
              <>
                {loginSession.login_mode === 'official_qr' ? (
                  qrDataUrl
                    ? <img className="official-qr" alt={t('accounts.qrOfficialAlt')} src={qrDataUrl} />
                    : <div className="qr-empty">{t('accounts.qrOpening')}</div>
                ) : (
                  <>
                    {!imageReady && <div className="qr-empty">{t('accounts.qrOpening')}</div>}
                    <img
                      alt="QR login screen"
                      src={authenticatedApiPath(`/accounts/login/qr/${loginSession.session_id}/image?ts=${loginSession.updated_at || Date.now()}`)}
                      style={{ display: imageReady ? 'block' : 'none' }}
                      onLoad={() => setImageReady(true)}
                      onError={() => setImageReady(false)}
                    />
                  </>
                )}
                <div className="qr-status">
                  <StatusBadge status={loginSession.status} />
                  {loginSession.login_mode === 'official_qr' && <span>{t('accounts.qrOfficialHint')}</span>}
                  {loginSession.account_id && <span>{t('accounts.accountCreated')} A{loginSession.account_id}</span>}
                  {loginSession.error_message && <span>{loginSession.error_message}</span>}
                </div>
              </>
            ) : (
              <div className="qr-empty">{t('accounts.qrEmpty')}</div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-kicker">{t('accounts.cookieLogin')}</div>
          <div className="panel-title">{t('accounts.cookieImport')}</div>
          <form onSubmit={createAccount} className="stack-form">
            <label>
              <span>{t('accounts.platform')}</span>
              <select className="input" value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value })}>
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <label>
              <span>{t('accounts.cookie')}</span>
              <textarea
                className="input textarea tall-textarea"
                value={form.encrypted_credential}
                onChange={e => setForm({ ...form, encrypted_credential: e.target.value })}
                placeholder={t('accounts.cookiePlaceholder')}
              />
            </label>
            <p className="muted-text tight-text">{t('accounts.rawCookieHint')}</p>
            <button className="btn-primary" disabled={busy || !form.encrypted_credential} type="submit">{t('accounts.importCreate')}</button>
          </form>
          {error && <div className="alert-danger">{error}</div>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('accounts.accountPool')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>{text.platform}</th>
                <th>{text.credential}</th>
                <th>{text.status}</th>
                <th>{text.calibration}</th>
                <th>{t('accounts.runtime')}</th>
                <th>{text.proxy}</th>
                <th>{text.risk}</th>
                <th>{text.latestSignal}</th>
                <th>{text.today}</th>
                <th>{text.actions}</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map(account => (
                <tr key={account.id}>
                  <td className="mono">A{account.id}</td>
                  <td>{platforms.find(platform => platform.id === account.platform)?.label || account.platform}</td>
                  <td>{account.credential_ready ? <span className="badge badge-ready">{t('accounts.imported')}</span> : <span className="badge badge-warn">{t('accounts.missing')}</span>}</td>
                  <td><StatusBadge status={account.status} /></td>
                  <td>{renderCalibration(account)}</td>
                  <td>{renderRuntime(account)}</td>
                  <td>
                    <div className="inline-edit">
                      <select
                        className="input compact-input"
                        value={proxyDrafts[account.id] ?? account.proxy_id ?? ''}
                        onChange={e => setProxyDrafts(prev => ({ ...prev, [account.id]: e.target.value }))}
                        disabled={busy || account.status === 'executing'}
                      >
                        <option value="">{text.noProxy}</option>
                        {proxyOptionsFor(account).map(proxy => (
                          <option value={proxy.id} key={proxy.id}>
                            P{proxy.id} / {proxy.proxy_type} / {proxy.status}
                          </option>
                        ))}
                      </select>
                      <button
                        className="btn-ghost"
                        disabled={busy || account.status === 'executing' || (proxyDrafts[account.id] === undefined && (account.proxy_id ?? '') === '')}
                        onClick={() => saveProxy(account)}
                      >
                        {text.saveProxy}
                      </button>
                    </div>
                  </td>
                  <td>{account.risk_score}</td>
                  <td>{renderRiskSummary(account)}</td>
                  <td>{account.daily_task_count}</td>
                  <td className="action-cell">
                    <button className="btn-ghost" disabled={!canMarkReady(account)} onClick={() => handleAction(account, 'ready')}>{text.ready}</button>
                    <button className="btn-ghost" disabled={busy || !canCalibrate(account)} onClick={() => calibrateAccount(account)}>{text.calibrate}</button>
                    <button className="btn-ghost" disabled={!canMarkCooling(account)} onClick={() => handleAction(account, 'cooling')}>{text.cool}</button>
                    <button className="btn-ghost" disabled={!canFreeze(account)} onClick={() => handleAction(account, 'frozen')}>{text.freeze}</button>
                    {deleteTarget === account.id ? (
                      <>
                        <button className="btn-danger" disabled={busy || account.status === 'executing'} onClick={() => deleteAccount(account)}>{text.confirmDelete}</button>
                        <button className="btn-ghost" disabled={busy} onClick={() => setDeleteTarget(null)}>{t('common.cancel')}</button>
                      </>
                    ) : (
                      <button className="btn-danger" disabled={busy || account.status === 'executing'} onClick={() => setDeleteTarget(account.id)}>{t('common.delete')}</button>
                    )}
                  </td>
                </tr>
              ))}
              {!accounts.length && <tr><td colSpan="11" className="empty-cell">{t('accounts.noAccounts')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('accounts.refreshCredentials')}</div>
        <div className="credential-grid">
          {accounts.map(account => (
            <div className="credential-row" key={account.id}>
              <div className="credential-meta">
                <span className="mono">A{account.id}</span>
                <StatusBadge status={account.status} />
              </div>
              <textarea
                className="input textarea"
                value={credentialDrafts[account.id] || ''}
                onChange={e => setCredentialDrafts(prev => ({ ...prev, [account.id]: e.target.value }))}
                placeholder={t('accounts.cookiePlaceholder')}
              />
              <button className="btn-primary" disabled={busy || !credentialDrafts[account.id]} onClick={() => saveCredential(account.id)}>{text.save}</button>
            </div>
          ))}
          {!accounts.length && <div className="empty-cell">{t('accounts.noRefresh')}</div>}
        </div>
      </div>
    </section>
  );
}

const accountText = {
  zh: {
    platform: '平台',
    credential: '凭据',
    status: '状态',
    calibration: '校准',
    proxy: '代理出口',
    risk: '风险',
    latestSignal: '最新信号',
    today: '今日',
    actions: '操作',
    ready: '可用',
    calibrate: '校准',
    cool: '冷却',
    freeze: '冻结',
    confirmDelete: '确认删除',
    save: '保存',
    saveProxy: '保存代理',
    noProxy: '不使用代理',
    proxyAssigned: '账号代理已绑定',
    proxyCleared: '账号代理已清空',
    restoredCooling: '已恢复冷却账号',
    missingCredentialMoved: '缺少凭据并移入需登录',
    staleExecutingCooled: '超时执行账号已冷却',
  },
  en: {
    platform: 'Platform',
    credential: 'Credential',
    status: 'Status',
    calibration: 'Calibration',
    proxy: 'Proxy exit',
    risk: 'Risk',
    latestSignal: 'Latest signal',
    today: 'Today',
    actions: 'Actions',
    ready: 'Ready',
    calibrate: 'Calibrate',
    cool: 'Cool',
    freeze: 'Freeze',
    confirmDelete: 'Confirm delete',
    save: 'Save',
    saveProxy: 'Save proxy',
    noProxy: 'No proxy',
    proxyAssigned: 'Account proxy assigned',
    proxyCleared: 'Account proxy cleared',
    restoredCooling: 'Restored cooling',
    missingCredentialMoved: 'Missing credential moved',
    staleExecutingCooled: 'Stale executing cooled',
  },
};

function modeLabel(run, t) {
  const mode = run.task_mode || (run.dry_run ? 'dry_run' : 'real_run');
  const label = t(`lotteries.${mode}`);
  return label === `lotteries.${mode}` ? mode : label;
}
