import { useEffect, useState } from 'react';

import { fetchJSON, postJSON, putJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

export default function RiskCenter() {
  const { notify, t } = useUi();
  const [events, setEvents] = useState([]);
  const [proxies, setProxies] = useState([]);
  const [summary, setSummary] = useState(null);
  const [form, setForm] = useState({ proxy_url: '', proxy_type: 'socks5', provider: '', country: '' });
  const [error, setError] = useState('');

  const load = () => Promise.all([
    fetchJSON('/metrics/risk/events'),
    fetchJSON('/proxies/'),
    fetchJSON('/proxies/summary'),
  ])
    .then(([eventRows, proxyRows, summaryRows]) => {
      setEvents(eventRows);
      setProxies(proxyRows);
      setSummary(summaryRows);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const createProxy = async (e) => {
    e.preventDefault();
    try {
      await postJSON('/proxies/', {
        proxy_url: form.proxy_url,
        proxy_type: form.proxy_type,
        provider: form.provider || null,
        country: form.country || null,
      });
      setForm({ proxy_url: '', proxy_type: 'socks5', provider: '', country: '' });
      notify(t('risk.added'), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const coolProxy = async (proxy) => {
    try {
      await putJSON(`/proxies/${proxy.id}/cooldown`, { minutes: 30, reason: 'manual safety cooldown' });
      notify(formatText(t('risk.cooled'), { id: proxy.id }), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const setProxyStatus = async (proxy, status) => {
    try {
      await putJSON(`/proxies/${proxy.id}/status`, { status });
      notify(formatText(t('risk.statusSet'), { id: proxy.id, status }), 'success');
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const checkProxy = async (proxy) => {
    try {
      const result = await postJSON(`/proxies/${proxy.id}/check`, {});
      if (result.status === 'ok') {
        notify(formatText(t('risk.checkOk'), { id: proxy.id, latency: result.latency_ms }), 'success');
      } else {
        notify(formatText(t('risk.checkFailed'), { id: proxy.id, error: result.error || result.status }), 'error');
      }
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    }
  };

  const statusLabel = (status) => {
    const value = t(`status.${status}`);
    return value === `status.${status}` ? status : value;
  };

  const cards = [
    { label: t('risk.proxyPool'), value: summary?.total, unit: t('risk.units.exits'), color: '#2563eb' },
    { label: t('risk.activeExits'), value: summary?.active, unit: t('risk.units.usable'), color: '#059669' },
    { label: t('risk.coolingExits'), value: summary?.cooling, unit: t('risk.units.cooldown'), color: '#d97706' },
    { label: t('risk.assigned'), value: summary?.assigned_accounts, unit: t('risk.units.accounts'), color: '#7c3aed' },
  ];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('risk.eyebrow')}</p>
          <h1>{t('risk.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      {error && <div className="alert-danger">{error}</div>}

      <div className="metric-grid readiness-metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="panel">
        <div className="panel-title">{t('risk.proxyExits')}</div>
        <form className="form-grid proxy-form" onSubmit={createProxy}>
          <label>
            <span>{t('risk.proxyUrl')}</span>
            <input className="input" value={form.proxy_url} onChange={e => setForm({ ...form, proxy_url: e.target.value })} placeholder="socks5://user:pass@host:port" />
          </label>
          <label>
            <span>{t('risk.type')}</span>
            <select className="input" value={form.proxy_type} onChange={e => setForm({ ...form, proxy_type: e.target.value })}>
              <option value="socks5">socks5</option>
              <option value="http">http</option>
              <option value="https">https</option>
            </select>
          </label>
          <label>
            <span>{t('risk.provider')}</span>
            <input className="input" value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })} />
          </label>
          <label>
            <span>{t('risk.region')}</span>
            <input className="input" value={form.country} onChange={e => setForm({ ...form, country: e.target.value })} />
          </label>
          <button className="btn-primary form-actions" type="submit" disabled={!form.proxy_url}>{t('risk.addProxy')}</button>
        </form>
        <div className="table-wrap compact-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>{t('risk.proxy')}</th>
                <th>{t('risk.status')}</th>
                <th>{t('risk.health')}</th>
                <th>{t('risk.cooldown')}</th>
                <th>{t('risk.account')}</th>
                <th>{t('risk.action')}</th>
              </tr>
            </thead>
            <tbody>
              {proxies.map(proxy => (
                <tr key={proxy.id}>
                  <td className="mono">P{proxy.id}</td>
                  <td>
                    <div className="mono small-text">{proxy.proxy_url}</div>
                    <div className="small-text muted-text">{[proxy.provider, proxy.country].filter(Boolean).join(' / ') || '-'}</div>
                    <div className="small-text muted-text">{t('risk.lastCheck')}: {proxy.last_check_at || '-'}</div>
                  </td>
                  <td><StatusBadge status={proxy.status === 'active' ? 'ready' : proxy.status === 'dead' ? 'banned' : 'cooling'} /></td>
                  <td>{Math.round(proxy.health_score ?? 0)}</td>
                  <td className="small-text">{proxy.cooldown_until || '-'}</td>
                  <td>
                    {proxy.assigned_account ? (
                      <div>
                        <div className="mono">A{proxy.assigned_account.id} / {proxy.assigned_account.platform}</div>
                        <div className="small-text muted-text">{statusLabel(proxy.assigned_account.status)} / risk24h {proxy.assigned_account.risk_24h}</div>
                      </div>
                    ) : (
                      <span className="badge badge-muted">{t('risk.unassigned')}</span>
                    )}
                  </td>
                  <td className="action-cell">
                    <button className="btn-primary" onClick={() => checkProxy(proxy)}>{t('risk.check')}</button>
                    <button className="btn-ghost" onClick={() => coolProxy(proxy)}>{t('risk.cool30m')}</button>
                    <button className="btn-ghost" onClick={() => setProxyStatus(proxy, 'active')}>{t('risk.active')}</button>
                    <button className="btn-ghost" onClick={() => setProxyStatus(proxy, 'dead')}>{t('risk.dead')}</button>
                  </td>
                </tr>
              ))}
              {!proxies.length && <tr><td className="empty-cell" colSpan="7">{t('risk.noProxies')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('risk.title')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('risk.time')}</th>
                <th>{t('risk.account')}</th>
                <th>{t('risk.type')}</th>
                <th>{t('risk.detail')}</th>
              </tr>
            </thead>
            <tbody>
              {events.map(event => (
                <tr key={event.id}>
                  <td>{new Date(event.created_at).toLocaleString()}</td>
                  <td>A{event.account_id}</td>
                  <td><span className="badge badge-warn">{statusLabel(event.event_type)}</span></td>
                  <td className="mono small-text">{JSON.stringify(event.detail)}</td>
                </tr>
              ))}
              {!events.length && <tr><td className="empty-cell" colSpan="4">{t('risk.noEvents')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function formatText(template, values) {
  return Object.entries(values).reduce((text, [key, value]) => text.replaceAll(`{${key}}`, value), template);
}
