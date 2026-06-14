import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import { useUi } from '../uiContext';

export default function Capacity() {
  const { t } = useUi();
  const [overview, setOverview] = useState(null);
  const [bindings, setBindings] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [ov, bd] = await Promise.all([
        fetchJSON('/capacity/overview'),
        fetchJSON('/capacity/bindings'),
      ]);
      setOverview(ov);
      setBindings(bd);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const perPlatform = overview?.per_platform || {};
  const pool = overview?.proxy_pool || {};
  const violations = bindings?.isolation_violations || [];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('capacity.eyebrow')}</p>
          <h1>{t('capacity.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>{t('capacity.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('capacity.subtitle')}</p>
      <div className="alert-warn small-text">{t('capacity.safetyBanner')}</div>
      {error && <div className="alert-danger">{error}</div>}

      <div className="panel">
        <div className="panel-title">{t('capacity.overviewTitle')}</div>
        {Object.keys(perPlatform).length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('capacity.platform')}</th>
                  <th>{t('capacity.safeAccounts')}</th>
                  <th>{t('capacity.totalAccounts')}</th>
                  <th>{t('capacity.healthyProxies')}</th>
                  <th>{t('capacity.sustainable')}</th>
                  <th>{t('capacity.used')}</th>
                  <th>{t('capacity.headroom')}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(perPlatform).map(([platform, b]) => (
                  <tr key={platform}>
                    <td>{platform}</td>
                    <td className="mono">{b.safe_accounts}</td>
                    <td className="mono">{b.total_accounts}</td>
                    <td className="mono">{b.healthy_bound_proxies}</td>
                    <td className="mono">{b.sustainable_daily}</td>
                    <td className="mono">{b.current_daily_used ?? 0}</td>
                    <td className="mono">{b.headroom ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-cell">{t('capacity.noData')}</div>}
      </div>

      <div className="panel">
        <div className="panel-title">{t('capacity.poolTitle')}</div>
        <div className="metric-grid readiness-metric-grid">
          <Metric label={t('capacity.poolTotal')} value={pool.total ?? 0} />
          <Metric label={t('capacity.poolHealthy')} value={pool.healthy ?? 0} />
          <Metric label={t('capacity.poolBound')} value={pool.bound ?? 0} />
          <Metric label={t('capacity.poolFree')} value={pool.free_healthy ?? 0} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('capacity.isolationTitle')}</div>
        {violations.length ? (
          violations.map((v, i) => (
            <div key={i} className="alert-danger small-text">
              [{v.kind}] {v.detail} ({(v.account_ids || []).map(id => `A${id}`).join(', ')})
            </div>
          ))
        ) : <div className="empty-cell">{t('capacity.isolationNone')}</div>}
      </div>

      <div className="panel">
        <div className="panel-title">{t('capacity.bindingsTitle')}</div>
        {(bindings?.bindings || []).length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('capacity.account')}</th>
                  <th>{t('capacity.platform')}</th>
                  <th>{t('capacity.recommendProxy')}</th>
                  <th>{t('capacity.health')}</th>
                </tr>
              </thead>
              <tbody>
                {bindings.bindings.map((b, i) => (
                  <tr key={i}>
                    <td className="mono">A{b.account_id}</td>
                    <td>{b.platform}</td>
                    <td className="mono">P{b.recommended_proxy_id}</td>
                    <td className="mono">{b.health_score}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-cell">{t('capacity.noData')}</div>}
        {(bindings?.unmet || []).length > 0 && (
          <div className="alert-warn small-text">
            {t('capacity.unmet')}: {bindings.unmet.map(u => `A${u.account_id}`).join(', ')}
          </div>
        )}
      </div>
    </section>
  );
}

function Metric({ label, value }) {
  return (
    <div className="metric-card">
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}
