import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

const BAND_COLOR = { high: '#dc2626', elevated: '#d97706', moderate: '#2563eb', low: '#64748b' };
const ACTION_COLOR = {
  allow: '#059669',
  dry_run_only: '#2563eb',
  cooldown: '#d97706',
  relogin: '#d97706',
  manual_review: '#dc2626',
  block: '#dc2626',
};

export default function RiskIntelligence() {
  const { t } = useUi();
  const [items, setItems] = useState([]);
  const [bands, setBands] = useState({ high: 0, elevated: 0, moderate: 0, low: 0 });
  const [error, setError] = useState('');

  const load = () => fetchJSON('/risk/intelligence?limit=100')
    .then(data => {
      setItems(data.items || []);
      setBands(data.bands || { high: 0, elevated: 0, moderate: 0, low: 0 });
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, []);

  const bandLabel = (band) => {
    const value = t(`riskIntel.bands.${band}`);
    return value === `riskIntel.bands.${band}` ? band : value;
  };
  const actionLabel = (action) => {
    const value = t(`riskIntel.actions.${action}`);
    return value === `riskIntel.actions.${action}` ? action : value;
  };
  const reasonLabel = (code) => {
    const value = t(`riskIntel.reasonCodes.${code}`);
    return value === `riskIntel.reasonCodes.${code}` ? code : value;
  };

  const cards = [
    { label: bandLabel('high'), value: bands.high ?? 0, unit: t('riskIntel.units.accounts'), color: BAND_COLOR.high },
    { label: bandLabel('elevated'), value: bands.elevated ?? 0, unit: t('riskIntel.units.accounts'), color: BAND_COLOR.elevated },
    { label: bandLabel('moderate'), value: bands.moderate ?? 0, unit: t('riskIntel.units.accounts'), color: BAND_COLOR.moderate },
    { label: bandLabel('low'), value: bands.low ?? 0, unit: t('riskIntel.units.accounts'), color: BAND_COLOR.low },
  ];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('riskIntel.eyebrow')}</p>
          <h1>{t('riskIntel.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('riskIntel.subtitle')}</p>
      {error && <div className="alert-danger">{error}</div>}

      <div className="metric-grid readiness-metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="panel">
        <div className="panel-title">{t('riskIntel.forecastTable')}</div>
        {items.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('riskIntel.account')}</th>
                  <th>{t('riskIntel.platform')}</th>
                  <th>{t('riskIntel.status')}</th>
                  <th>{t('riskIntel.reputation')}</th>
                  <th>{t('riskIntel.riskScore')}</th>
                  <th>{t('riskIntel.forecast24h')}</th>
                  <th>{t('riskIntel.forecastBand')}</th>
                  <th>{t('riskIntel.recommendedAction')}</th>
                  <th>{t('riskIntel.reasons')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr key={item.account_id}>
                    <td className="mono">A{item.account_id}</td>
                    <td>{item.platform}</td>
                    <td><StatusBadge status={item.status} /></td>
                    <td><ConfidenceBar value={item.reputation_score} /></td>
                    <td className="mono">{item.risk_score}</td>
                    <td className="mono">{Math.round((item.forecast_24h || 0) * 100)}%</td>
                    <td>
                      <span className="badge" style={{ background: BAND_COLOR[item.forecast_band] || '#64748b', color: '#fff' }}>
                        {bandLabel(item.forecast_band)}
                      </span>
                    </td>
                    <td>
                      <span className="badge" style={{ background: ACTION_COLOR[item.recommended_action] || '#64748b', color: '#fff' }}>
                        {actionLabel(item.recommended_action)}
                      </span>
                    </td>
                    <td className="small-text">
                      {(item.reasons || []).map(code => (
                        <span className="badge badge-muted reason-chip" key={code}>{reasonLabel(code)}</span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-cell">{t('riskIntel.noData')}</div>
        )}
      </div>
    </section>
  );
}

function ConfidenceBar({ value }) {
  const pct = Math.max(0, Math.min(100, value || 0));
  const color = pct >= 70 ? '#059669' : pct >= 40 ? '#2563eb' : pct >= 15 ? '#d97706' : '#64748b';
  return (
    <div className="confidence-bar">
      <div className="confidence-fill" style={{ width: `${pct}%`, background: color }} />
      <span className="confidence-label mono">{pct}</span>
    </div>
  );
}
