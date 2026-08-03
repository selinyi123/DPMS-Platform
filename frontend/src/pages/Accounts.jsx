import { useEffect, useMemo, useState } from 'react';
import QRCode from 'qrcode';

import {
  deleteJSON,
  fetchJSON,
  postJSON,
  putJSON,
} from '../api';
import {
  calibrationNeedsIdentityReview,
  isWeiboOAuthAccount,
  weiboOAuthCapabilityPresentation,
} from '../accountCalibration';
import { accountIdentityPresentation } from '../accountIdentityPresentation';
import StatusBadge from '../components/StatusBadge';
import AuthenticatedAssetLink from '../components/AuthenticatedAssetLink';
import AuthenticatedImage from '../components/AuthenticatedImage';
import {
  DOUYIN_DEVICE_CREDENTIAL_INVALID,
  normalizeDouyinDeviceCredential,
} from '../douyinDeviceCredential';
import {
  browserLoginImagePath,
  isActiveLoginSession,
  isTerminalLoginStatus,
  loginSessionPollRetryDelay,
} from '../loginSessionPresentation';
import { useUi } from '../uiContext';

const WEIBO_ACTIONS = ['followed', 'liked', 'commented', 'favorited', 'reposted'];

function defaultWeiboAttestationDraft() {
  return {
    app_review_status: 'unknown',
    client_type: 'other',
    granted_actions: Object.fromEntries(WEIBO_ACTIONS.map(action => [action, false])),
  };
}

function usesDouyinDeviceCredential(account) {
  return account?.platform === 'douyin'
    && account?.credential_kind !== 'browser_session';
}

export default function Accounts() {
  const { language, notify, t } = useUi();
  const [accounts, setAccounts] = useState([]);
  const [platforms, setPlatforms] = useState([]);
  const [proxies, setProxies] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ platform: 'bilibili', encrypted_credential: '' });
  const [qrPlatform, setQrPlatform] = useState('bilibili');
  const [credentialDrafts, setCredentialDrafts] = useState({});
  const [proxyDrafts, setProxyDrafts] = useState({});
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [loginSession, setLoginSession] = useState(null);
  const [imageReady, setImageReady] = useState(false);
  const [qrImageRevision, setQrImageRevision] = useState(0);
  const [qrDataUrl, setQrDataUrl] = useState('');
  const [recheckResult, setRecheckResult] = useState(null);
  const [weiboAttestationDrafts, setWeiboAttestationDrafts] = useState({});
  const text = accountText[language] || accountText.zh;

  const selectedPlatform = useMemo(
    () => platforms.find(platform => platform.id === form.platform),
    [platforms, form.platform],
  );
  const selectedQrPlatform = useMemo(
    () => platforms.find(platform => platform.id === qrPlatform),
    [platforms, qrPlatform],
  );
  const loginSessionActive = isActiveLoginSession(loginSession);
  const selectedQrSessionActive = loginSessionActive
    && loginSession?.platform === qrPlatform;
  const formUsesWeiboOAuth = form.platform === 'weibo';
  const formUsesDouyinDevice = form.platform === 'douyin';
  const formCredentialKeys = formUsesWeiboOAuth
    ? {
      kicker: 'accounts.weiboOAuthLogin',
      title: 'accounts.weiboOAuthImport',
      label: 'accounts.weiboOAuthCredential',
      placeholder: 'accounts.weiboOAuthCredentialPlaceholder',
      hint: 'accounts.weiboOAuthCredentialHint',
    }
    : formUsesDouyinDevice
      ? {
        kicker: 'accounts.douyinDeviceLogin',
        title: 'accounts.douyinDeviceImport',
        label: 'accounts.douyinDeviceCredential',
        placeholder: 'accounts.douyinDeviceCredentialPlaceholder',
        hint: 'accounts.douyinDeviceCredentialHint',
      }
      : {
        kicker: 'accounts.cookieLogin',
        title: 'accounts.cookieImport',
        label: 'accounts.cookie',
        placeholder: 'accounts.cookiePlaceholder',
        hint: 'accounts.rawCookieHint',
      };

  const describeCredentialError = err => (
    err?.code === DOUYIN_DEVICE_CREDENTIAL_INVALID
      ? t('accounts.douyinDeviceCredentialInvalid')
      : err.message
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
    if (!loginSession?.session_id || isTerminalLoginStatus(loginSession.status)) return undefined;
    let cancelled = false;
    let timer;
    let transientError = '';
    const poll = async () => {
      try {
        const next = loginSession.login_mode === 'official_qr'
          ? await postJSON(`/accounts/login/qr/${loginSession.session_id}/poll`, {})
          : await fetchJSON(`/accounts/login/qr/${loginSession.session_id}`);
        if (cancelled) return;
        setLoginSession(next);
        if (next.login_mode !== 'official_qr') {
          setQrImageRevision(current => current + 1);
        }
        if (transientError) {
          setError(current => (current === transientError ? '' : current));
          transientError = '';
        }
        if (next.status === 'confirmed') await load();
      } catch (err) {
        if (cancelled) return;
        const retryDelay = loginSessionPollRetryDelay(err);
        if (retryDelay !== null) {
          transientError = err.message;
          setError(transientError);
          timer = window.setTimeout(poll, retryDelay);
          return;
        }
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
    setQrImageRevision(0);
    setQrDataUrl('');
    try {
      const session = await postJSON('/accounts/login/qr', { platform: qrPlatform });
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
      const encryptedCredential = formUsesDouyinDevice
        ? normalizeDouyinDeviceCredential(form.encrypted_credential)
        : form.encrypted_credential;
      await postJSON('/accounts/', {
        ...form,
        encrypted_credential: encryptedCredential,
      });
      setForm({ ...form, encrypted_credential: '' });
      notify(t('accounts.importQueued'), 'success');
      await load();
    } catch (err) {
      const message = describeCredentialError(err);
      setError(message);
      notify(message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const saveCredential = async (account) => {
    const value = credentialDrafts[account.id] || '';
    setBusy(true);
    setError('');
    try {
      const encryptedCredential = usesDouyinDeviceCredential(account)
        ? normalizeDouyinDeviceCredential(value)
        : value;
      await putJSON(`/accounts/${account.id}/credential`, {
        encrypted_credential: encryptedCredential,
      });
      setCredentialDrafts(prev => ({ ...prev, [account.id]: '' }));
      notify(t('accounts.credentialSaved'), 'success');
      await load();
    } catch (err) {
      const message = describeCredentialError(err);
      setError(message);
      notify(message, 'error');
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

  const updateWeiboAttestation = (accountId, update) => {
    setWeiboAttestationDrafts(previous => ({
      ...previous,
      [accountId]: {
        ...defaultWeiboAttestationDraft(),
        ...(previous[accountId] || {}),
        ...update,
      },
    }));
  };

  const updateWeiboGrant = (accountId, action, granted) => {
    const current = weiboAttestationDrafts[accountId] || defaultWeiboAttestationDraft();
    updateWeiboAttestation(accountId, {
      granted_actions: { ...current.granted_actions, [action]: granted },
    });
  };

  const attestWeiboCapabilities = async (account) => {
    if (!window.confirm(t('accounts.weiboOAuthAttestationConfirm'))) return;
    const draft = weiboAttestationDrafts[account.id] || defaultWeiboAttestationDraft();
    setBusy(true);
    setError('');
    try {
      await postJSON(
        `/accounts/${account.id}/weibo-oauth-capability-attestation`,
        { ...draft, confirm: true },
        { confirm: true },
      );
      notify(t('accounts.weiboOAuthAttestationQueued'), 'success');
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

  const canMarkReady = account => {
    const weiboCapability = weiboOAuthCapabilityPresentation(account);
    return ['warming', 'cooling'].includes(account.status)
      && account.credential_ready
      && account.latest_calibration?.status === 'succeeded'
      && (!weiboCapability || (
        weiboCapability.blockers.length === 0
        && weiboCapability.grantedActions.length > 0
      ));
  };
  const canMarkCooling = account => ['ready', 'warming', 'executing'].includes(account.status);
  const canFreeze = account => !['frozen', 'banned'].includes(account.status);
  // A generic identity-only OAuth calibration becomes the newest evidence and
  // intentionally invalidates an older capability attestation. OAuth accounts
  // therefore refresh through the explicit admin-attestation workflow below.
  const canCalibrate = account => account.credential_ready
    && !isWeiboOAuthAccount(account)
    && !['executing', 'banned'].includes(account.status);
  const proxyOptionsFor = account => proxies.filter(proxy => (
    !proxy.assigned_account || proxy.assigned_account.id === account.id
  ));

  const renderCalibration = (account) => {
    const calibration = account.latest_calibration;
    const weiboCapability = weiboOAuthCapabilityPresentation(account);
    const weiboOAuthAccount = isWeiboOAuthAccount(account);
    const attestationDraft = weiboAttestationDrafts[account.id] || defaultWeiboAttestationDraft();
    if (!calibration && !weiboOAuthAccount) {
      return <span className="badge badge-muted">{t('accounts.notChecked')}</span>;
    }
    return (
      <div className="calibration-summary">
        {calibration
          ? <StatusBadge status={calibration.status} />
          : <span className="badge badge-muted">{t('accounts.notChecked')}</span>}
        {calibrationNeedsIdentityReview(calibration) && (
          <span className="badge badge-warn" title={t('accounts.sessionOnlyHint')}>
            {t('accounts.sessionOnly')}
          </span>
        )}
        {calibration?.screenshot_path && calibration.calibration_id && (
          <AuthenticatedAssetLink
            className="badge badge-info evidence-link"
            path={`/accounts/calibrations/${calibration.calibration_id}/screenshot`}
            onError={(assetError) => {
              setError(assetError.message);
              notify(assetError.message, 'error');
            }}
          >
            {t('accounts.evidence')}
          </AuthenticatedAssetLink>
        )}
        {calibration?.error_message && <span className="mono small-text">{calibration.error_message}</span>}
        {weiboCapability && (
          <div className="runtime-summary">
            <span className={`badge ${weiboCapability.blockers.length ? 'badge-warn' : 'badge-ready'}`}>
              {t(weiboCapability.blockers.length
                ? 'accounts.weiboOAuthNeedsEvidence'
                : 'accounts.weiboOAuthVerified')}
            </span>
            {weiboCapability.present && (
              <>
                <span className="small-text">
                  {t('accounts.weiboOAuthClient')}: {weiboCapability.clientType || '-'}
                </span>
                <span className="small-text">
                  {t('accounts.weiboOAuthGranted')}: {weiboCapability.grantedActions.length
                    ? weiboCapability.grantedActions.map(action => t(`lotteries.actions.${action}`)).join(' / ')
                    : t('common.none')}
                </span>
                {!!weiboCapability.deniedActions.length && (
                  <span className="small-text warning-text">
                    {t('accounts.weiboOAuthDenied')}: {weiboCapability.deniedActions
                      .map(action => t(`lotteries.actions.${action}`)).join(' / ')}
                  </span>
                )}
                {weiboCapability.verifiedAt && (
                  <span className="mono small-text">
                    {t('accounts.weiboOAuthVerifiedAt')}: {weiboCapability.verifiedAt}
                  </span>
                )}
                {weiboCapability.attestedAt && (
                  <span className="mono small-text">
                    {t('accounts.weiboOAuthAttestedAt')}: {weiboCapability.attestedAt}
                  </span>
                )}
                {weiboCapability.attestedBy && (
                  <span className="small-text">
                    {t('accounts.weiboOAuthAttestedBy')}: {weiboCapability.attestedBy}
                  </span>
                )}
              </>
            )}
            {weiboCapability.blockers.map(code => (
              <span className="badge badge-warn" key={code}>
                {t(`accounts.weiboOAuthBlockers.${code}`)}
              </span>
            ))}
          </div>
        )}
        {weiboOAuthAccount && (
          <details className="runtime-summary">
            <summary>{t('accounts.weiboOAuthAttestationTitle')}</summary>
            <span className="small-text warning-text">
              {t('accounts.weiboOAuthAttestationWarning')}
            </span>
            <label>
              <span>{t('accounts.weiboOAuthAppReviewStatus')}</span>
              <select
                className="input compact-input"
                value={attestationDraft.app_review_status}
                onChange={event => updateWeiboAttestation(account.id, {
                  app_review_status: event.target.value,
                })}
              >
                {['unknown', 'test_only', 'approved'].map(value => (
                  <option value={value} key={value}>
                    {t(`accounts.weiboOAuthAppReviewStatuses.${value}`)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>{t('accounts.weiboOAuthClientType')}</span>
              <select
                className="input compact-input"
                value={attestationDraft.client_type}
                onChange={event => updateWeiboAttestation(account.id, {
                  client_type: event.target.value,
                })}
              >
                {['other', 'weibo'].map(value => (
                  <option value={value} key={value}>
                    {t(`accounts.weiboOAuthClientTypes.${value}`)}
                  </option>
                ))}
              </select>
            </label>
            <div className="check-grid">
              {WEIBO_ACTIONS.map(action => (
                <label className="check-item" key={action}>
                  <input
                    type="checkbox"
                    checked={attestationDraft.granted_actions[action]}
                    onChange={event => updateWeiboGrant(account.id, action, event.target.checked)}
                  />
                  <span>{t(`lotteries.actions.${action}`)}</span>
                </label>
              ))}
            </div>
            <button
              className="btn-danger"
              disabled={busy || !account.credential_ready || ['executing', 'banned'].includes(account.status)}
              onClick={() => attestWeiboCapabilities(account)}
            >
              {t('accounts.weiboOAuthAttestationSubmit')}
            </button>
          </details>
        )}
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

  const renderPlatformIdentity = (account) => {
    const identity = accountIdentityPresentation(account);
    if (!identity.verified) {
      return (
        <div className="runtime-summary account-identity-summary">
          <span className="badge badge-muted">
            {t(`accounts.identityStates.${identity.state}`)}
          </span>
        </div>
      );
    }
    return (
      <div className="runtime-summary account-identity-summary">
        <span className="badge badge-ready">{t('accounts.identityVerified')}</span>
        {identity.nickname && <strong>{identity.nickname}</strong>}
        {identity.uid && (
          <span className="mono small-text">
            {t('accounts.platformUid')}: {identity.uid}
          </span>
        )}
        {identity.title && <span className="badge badge-info">{identity.title}</span>}
        {identity.level && (
          <span className="small-text">
            {t('accounts.platformLevel')}: {identity.level}
          </span>
        )}
        {identity.state === 'verified_without_public_profile' && (
          <span className="small-text muted-text">
            {t('accounts.identityStates.verified_without_public_profile')}
          </span>
        )}
      </div>
    );
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
              <select className="input" value={qrPlatform} onChange={e => setQrPlatform(e.target.value)}>
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <button
              className="btn-primary"
              disabled={busy || !selectedQrPlatform?.qr_login || selectedQrSessionActive}
              onClick={startQrLogin}
            >
              {t('accounts.generateQr')}
            </button>
          </div>
          {selectedQrPlatform?.qr_login_blocker && (
            <div className="alert-warn">
              {t(`accounts.qrLoginBlockers.${selectedQrPlatform.qr_login_blocker}`)}
            </div>
          )}
          <div className="qr-frame">
            {loginSession?.session_id ? (
              <>
                {loginSession.login_mode === 'official_qr' ? (
                  qrDataUrl
                    ? <img className="official-qr" alt={t('accounts.qrOfficialAlt')} src={qrDataUrl} />
                    : <div className="qr-empty">{t('accounts.qrOpening')}</div>
                ) : (
                  <>
                    {!imageReady && loginSessionActive && (
                      <div className="qr-empty">{t('accounts.qrOpening')}</div>
                    )}
                    {!imageReady && !loginSessionActive && (
                      <div className="qr-empty">{loginSession.error_message || t(`status.${loginSession.status}`)}</div>
                    )}
                    {loginSessionActive && (
                      <AuthenticatedImage
                        alt="QR login screen"
                        path={browserLoginImagePath(loginSession, qrImageRevision)}
                        style={{ display: imageReady ? 'block' : 'none' }}
                        onLoad={() => setImageReady(true)}
                        onError={() => setImageReady(false)}
                      />
                    )}
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
          <div className="panel-kicker">{t(formCredentialKeys.kicker)}</div>
          <div className="panel-title">{t(formCredentialKeys.title)}</div>
          <form onSubmit={createAccount} className="stack-form">
            <label>
              <span>{t('accounts.platform')}</span>
              <select
                className="input"
                value={form.platform}
                onChange={e => setForm({
                  ...form,
                  platform: e.target.value,
                  encrypted_credential: '',
                })}
              >
                {platforms.map(platform => <option value={platform.id} key={platform.id}>{platform.label}</option>)}
              </select>
            </label>
            <label>
              <span>{t(formCredentialKeys.label)}</span>
              <textarea
                className="input textarea tall-textarea"
                value={form.encrypted_credential}
                onChange={e => setForm({ ...form, encrypted_credential: e.target.value })}
                placeholder={t(formCredentialKeys.placeholder)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            <p className="muted-text tight-text">{t(formCredentialKeys.hint)}</p>
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
                <th>{t('accounts.internalId')}</th>
                <th>{text.platform}</th>
                <th>{t('accounts.platformIdentity')}</th>
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
                  <td>{renderPlatformIdentity(account)}</td>
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
              {!accounts.length && <tr><td colSpan="12" className="empty-cell">{t('accounts.noAccounts')}</td></tr>}
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
                placeholder={t(usesDouyinDeviceCredential(account)
                  ? 'accounts.douyinDeviceCredentialPlaceholder'
                  : account.platform === 'weibo'
                    ? (isWeiboOAuthAccount(account)
                      ? 'accounts.weiboOAuthCredentialPlaceholder'
                      : 'accounts.cookiePlaceholder')
                    : 'accounts.cookiePlaceholder')}
                autoComplete="off"
                spellCheck={false}
              />
              {usesDouyinDeviceCredential(account) && (
                <p className="muted-text tight-text">{t('accounts.douyinDeviceCredentialHint')}</p>
              )}
              <button className="btn-primary" disabled={busy || !credentialDrafts[account.id]} onClick={() => saveCredential(account)}>{text.save}</button>
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
