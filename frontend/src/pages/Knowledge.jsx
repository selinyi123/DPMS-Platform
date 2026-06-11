import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import { useUi } from '../uiContext';

const LEVEL_COLOR = {
  learning_ready: '#059669',
  usable: '#2563eb',
  warming: '#d97706',
  cold_start: '#64748b',
};
const PRIORITY_COLOR = { P0: '#dc2626', P1: '#d97706', P2: '#2563eb' };

export default function Knowledge() {
  const { t } = useUi();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const load = () => fetchJSON('/knowledge/summary?window_days=30')
    .then(payload => {
      setData(payload);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, []);

  const summary = data?.summary || {};
  const platforms = data?.platform_profiles || [];
  const accounts = data?.account_profiles || [];
  const gaps = data?.learning_gaps || [];

  const levelLabel = (level) => {
    const value = t(`knowledge.levels.${level}`);
    return value === `knowledge.levels.${level}` ? level : value;
  };

  const cards = [
    {
      label: t('knowledge.maturity'),
      value: summary.data_maturity_score ?? 0,
      unit: levelLabel(summary.data_maturity_level || 'cold_start'),
      color: LEVEL_COLOR[summary.data_maturity_level] || '#64748b',
    },
    { label: t('knowledge.events'), value: summary.total_events ?? 0, unit: t('knowledge.units.events'), color: '#2563eb' },
    { label: t('knowledge.resultLabels'), value: summary.result_labels ?? 0, unit: t('knowledge.units.labels'), color: '#7c3aed' },
    { label: t('knowledge.shadowSuccess'), value: summary.shadow_success ?? 0, unit: t('knowledge.units.runs'), color: '#059669' },
  ];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('knowledge.eyebrow')}</p>
          <h1>{t('knowledge.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('knowledge.subtitle')}</p>
      {error && <div className="alert-danger">{error}</div>}

      <div className="metric-grid readiness-metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="panel">
        <div className="panel-title">{t('knowledge.platformProfiles')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('knowledge.platform')}</th>
                <th>{t('knowledge.confidence')}</th>
                <th>{t('knowledge.lotteries')}</th>
                <th>{t('knowledge.winRate')}</th>
                <th>{t('knowledge.runs')}</th>
                <th>{t('knowledge.successRate')}</th>
                <th>{t('knowledge.shadowReal')}</th>
                <th>{t('knowledge.risk')}</th>
              </tr>
            </thead>
            <tbody>
              {platforms.map(p => (
                <tr key={p.platform}>
                  <td>{p.label || p.platform}</td>
                  <td><ConfidenceBar value={p.knowledge_confidence} /></td>
                  <td className="mono">{p.total_lotteries}</td>
                  <td className="mono">{p.win_rate == null ? '-' : `${Math.round(p.win_rate * 100)}%`}</td>
                  <td className="mono">{p.total_runs}</td>
                  <td className="mono">{p.task_success_rate == null ? '-' : `${Math.round(p.task_success_rate * 100)}%`}</td>
                  <td className="mono">{p.shadow_success} / {p.real_success}</td>
                  <td className="mono">{p.risk_events}</td>
                </tr>
              ))}
              {!platforms.length && <tr><td className="empty-cell" colSpan="8">{t('knowledge.noData')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('knowledge.accountProfiles')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('knowledge.account')}</th>
                <th>{t('knowledge.platform')}</th>
                <th>{t('knowledge.reputation')}</th>
                <th>{t('knowledge.runs')}</th>
                <th>{t('knowledge.successRate')}</th>
                <th>{t('knowledge.risk')}</th>
              </tr>
            </thead>
            <tbody>
              {accounts.slice(0, 12).map(a => (
                <tr key={a.account_id}>
                  <td className="mono">A{a.account_id}</td>
                  <td>{a.platform}</td>
                  <td><ConfidenceBar value={a.reputation_score} /></td>
                  <td className="mono">{a.total_runs}</td>
                  <td className="mono">{a.task_success_rate == null ? '-' : `${Math.round(a.task_success_rate * 100)}%`}</td>
                  <td className="mono">{a.risk_events}</td>
                </tr>
              ))}
              {!accounts.length && <tr><td className="empty-cell" colSpan="6">{t('knowledge.noAccounts')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('knowledge.learningGaps')}</div>
        <div className="gap-list">
          {gaps.map(g => (
            <div className="gap-item" key={g.code}>
              <span className="badge" style={{ background: PRIORITY_COLOR[g.priority] || '#64748b', color: '#fff' }}>{g.priority}</span>
              <div>
                <div className="gap-title">{g.title}</div>
                <div className="small-text muted-text">{g.detail}</div>
                {g.evidence?.platforms && (
                  <div className="small-text muted-text">{g.evidence.platforms.join(', ')}</div>
                )}
              </div>
            </div>
          ))}
          {!gaps.length && <div className="empty-cell">{t('knowledge.noGaps')}</div>}
        </div>
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
