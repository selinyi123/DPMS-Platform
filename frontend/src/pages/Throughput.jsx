import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import { useUi } from '../uiContext';

const ACTION_COLOR = { scale_up: '#059669', hold: '#2563eb', throttle: '#d97706', pause: '#dc2626' };
const TREND_COLOR = { rising: '#dc2626', flat: '#64748b', falling: '#059669' };

export default function Throughput() {
  const { t } = useUi();
  const [windowMinutes, setWindowMinutes] = useState(1440);
  const [overview, setOverview] = useState(null);
  const [backpressure, setBackpressure] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [ov, bp] = await Promise.all([
        fetchJSON(`/throughput/overview?window_minutes=${windowMinutes}`),
        fetchJSON(`/throughput/backpressure?window_minutes=${windowMinutes}`),
      ]);
      setOverview(ov);
      setBackpressure(bp);
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

  const observed = overview?.observed || {};
  const ceiling = overview?.ceiling || {};
  const recommendations = backpressure?.recommendations || {};
  const alerts = overview?.saturation_alerts || [];

  const pct = (v) => (v == null ? '-' : `${Math.round(v * 100)}%`);

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('throughput.eyebrow')}</p>
          <h1>{t('throughput.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>{t('throughput.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('throughput.subtitle')}</p>
      <div className="alert-warn small-text">{t('throughput.safetyBanner')}</div>

      <div className="panel">
        <div className="stack-form">
          <label>
            <span>{t('throughput.window')}</span>
            <input
              className="input" type="number" min="15" max="10080"
              value={windowMinutes}
              onChange={e => setWindowMinutes(Number(e.target.value))}
              onKeyDown={e => e.key === 'Enter' && load()}
            />
          </label>
          <div className="toolbar">
            <button className="btn-primary" onClick={load} disabled={loading}>{t('throughput.refresh')}</button>
          </div>
        </div>
        {error && <div className="alert-danger">{error}</div>}
      </div>

      {alerts.length > 0 && (
        <div className="alert-danger small-text">
          {t('throughput.saturation')}: {alerts.join(', ')}
        </div>
      )}

      <div className="panel">
        <div className="panel-title">{t('throughput.overviewTitle')}</div>
        {Object.keys(observed).length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('throughput.platform')}</th>
                  <th>{t('throughput.observed')}</th>
                  <th>{t('throughput.perHour')}</th>
                  <th>{t('throughput.successRate')}</th>
                  <th>{t('throughput.riskRate')}</th>
                  <th>{t('throughput.ceiling')}</th>
                  <th>{t('throughput.utilization')}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(observed).map(([platform, b]) => (
                  <tr key={platform}>
                    <td>{platform}</td>
                    <td className="mono">{b.observed}</td>
                    <td className="mono">{b.observed_per_hour}</td>
                    <td className="mono">{pct(b.success_rate)}</td>
                    <td className="mono">{pct(b.risk_rate)}</td>
                    <td className="mono">{(ceiling[platform]?.ceiling ?? b.ceiling) ?? '-'}</td>
                    <td className="mono">{pct(b.utilization)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-cell">{t('throughput.noData')}</div>}
      </div>

      <div className="panel">
        <div className="panel-title">{t('throughput.backpressureTitle')}</div>
        {Object.keys(recommendations).length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('throughput.platform')}</th>
                  <th>{t('throughput.action')}</th>
                  <th>{t('throughput.factor')}</th>
                  <th>{t('throughput.utilization')}</th>
                  <th>{t('throughput.trend')}</th>
                  <th>{t('throughput.reasons')}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(recommendations).map(([platform, r]) => (
                  <tr key={platform}>
                    <td>{platform}</td>
                    <td>
                      <span className="badge" style={{ background: ACTION_COLOR[r.action] || '#64748b', color: '#fff' }}>
                        {t(`throughput.actions.${r.action}`)}
                      </span>
                    </td>
                    <td className="mono">×{r.factor}</td>
                    <td className="mono">{pct(r.utilization)}</td>
                    <td>
                      <span className="badge" style={{ background: TREND_COLOR[r.risk_trend] || '#64748b', color: '#fff' }}>
                        {t(`throughput.trends.${r.risk_trend}`)}
                      </span>
                    </td>
                    <td className="small-text">
                      {(r.reasons || []).map(code => <span className="badge badge-muted reason-chip" key={code}>{code}</span>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-cell">{t('throughput.noData')}</div>}
      </div>
    </section>
  );
}
