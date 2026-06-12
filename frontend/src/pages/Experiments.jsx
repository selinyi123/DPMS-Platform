import { useEffect, useState } from 'react';

import { fetchJSON, postJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

export default function Experiments() {
  const { t, notify } = useUi();
  const [items, setItems] = useState([]);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const load = () => fetchJSON('/experiments/')
    .then(data => {
      setItems(data.items || []);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, []);

  const guardrailLabel = (code) => {
    const value = t(`experiments.guardrails.${code}`);
    return value === `experiments.guardrails.${code}` ? code : value;
  };
  const statLabel = (code) => {
    const value = t(`experiments.stopReasons.${code}`);
    return value === `experiments.stopReasons.${code}` ? code : value;
  };

  const openDetail = async (item) => {
    setSelected(item);
    setDetail(null);
    setLoadingDetail(true);
    try {
      const data = await fetchJSON(`/experiments/${item.experiment_id}`);
      setDetail(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingDetail(false);
    }
  };

  const reloadDetail = async (experimentId) => {
    try {
      const data = await fetchJSON(`/experiments/${experimentId}`);
      setDetail(data);
    } catch (err) {
      setError(err.message);
    }
  };

  const closeDetail = () => {
    setSelected(null);
    setDetail(null);
  };

  const stopBranch = async (experimentId, branchKey) => {
    const reason = window.prompt(t('experiments.stopReasonPrompt'), 'operator_review') || 'operator_review';
    try {
      await postJSON(`/experiments/${experimentId}/branches/${branchKey}/stop`, { reason }, { confirm: true });
      notify(t('experiments.branchStopped').replace('{branch}', branchKey), 'success');
      await reloadDetail(experimentId);
      await load();
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  const stopExperiment = async (experimentId) => {
    const reason = window.prompt(t('experiments.stopReasonPrompt'), 'operator_review') || 'operator_review';
    try {
      await postJSON(`/experiments/${experimentId}/stop`, { reason }, { confirm: true });
      notify(t('experiments.experimentStopped'), 'success');
      await reloadDetail(experimentId);
      await load();
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  const runningCount = items.filter(i => i.status === 'running').length;
  const stoppedCount = items.filter(i => i.status === 'stopped').length;

  const cards = [
    { label: t('experiments.totalExperiments'), value: items.length, unit: t('experiments.units.experiments'), color: '#2563eb' },
    { label: t('experiments.running'), value: runningCount, unit: t('experiments.units.experiments'), color: '#059669' },
    { label: t('experiments.stopped'), value: stoppedCount, unit: t('experiments.units.experiments'), color: '#64748b' },
  ];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('experiments.eyebrow')}</p>
          <h1>{t('experiments.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('experiments.subtitle')}</p>
      {error && <div className="alert-danger">{error}</div>}

      <div className="metric-grid readiness-metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="panel">
        <div className="panel-title">{t('experiments.experimentList')}</div>
        {items.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('experiments.id')}</th>
                  <th>{t('experiments.name')}</th>
                  <th>{t('experiments.platform')}</th>
                  <th>{t('experiments.mode')}</th>
                  <th>{t('experiments.status')}</th>
                  <th>{t('experiments.branches')}</th>
                  <th>{t('experiments.action')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr key={item.experiment_id}>
                    <td className="mono small-text">{item.experiment_id.slice(0, 8)}</td>
                    <td>{item.name}</td>
                    <td>{item.platform}</td>
                    <td><span className="badge badge-muted">{item.mode}</span></td>
                    <td><StatusBadge status={item.status} /></td>
                    <td className="small-text">
                      {(item.branches || []).map(branch => (
                        <span className="badge badge-muted reason-chip" key={branch.branch_key}>
                          {branch.label || branch.branch_key} · {branch.status}
                        </span>
                      ))}
                    </td>
                    <td><button className="btn-primary" onClick={() => openDetail(item)}>{t('experiments.details')}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-cell">{t('experiments.noExperiments')}</div>
        )}
      </div>

      {selected && (
        <div className="drawer-backdrop" onClick={closeDetail}>
          <aside className="drawer-panel" onClick={e => e.stopPropagation()}>
            <header className="drawer-header">
              <div>
                <p className="eyebrow">{t('experiments.detailTitle')}</p>
                <h2>{selected.name}</h2>
              </div>
              <button className="btn-ghost" onClick={closeDetail}>{t('common.cancel')}</button>
            </header>
            {loadingDetail && <div className="muted-text">{t('experiments.loadingDetail')}</div>}
            {detail && (
              <ExperimentDetail
                detail={detail}
                t={t}
                guardrailLabel={guardrailLabel}
                statLabel={statLabel}
                onStopBranch={stopBranch}
                onStopExperiment={stopExperiment}
              />
            )}
          </aside>
        </div>
      )}
    </section>
  );
}

function ExperimentDetail({ detail, t, guardrailLabel, statLabel, onStopBranch, onStopExperiment }) {
  const experiment = detail.experiment || {};
  const comparison = detail.comparison || {};
  const branches = comparison.branches || [];
  const stopSignals = detail.stop_signals || {};
  const guardrails = detail.guardrails || [];

  const pct = (value) => (value == null ? '-' : `${Math.round(value * 100)}%`);

  return (
    <div className="drawer-body">
      <div className="panel-subtle">
        <div className="panel-title">{t('experiments.overview')}</div>
        <div className="kv-row"><span>{t('experiments.id')}</span><span className="mono small-text">{experiment.experiment_id}</span></div>
        <div className="kv-row"><span>{t('experiments.platform')}</span><span>{experiment.platform}</span></div>
        <div className="kv-row"><span>{t('experiments.mode')}</span><span className="badge badge-muted">{experiment.mode}</span></div>
        <div className="kv-row"><span>{t('experiments.status')}</span><span><StatusBadgeInline status={experiment.status} t={t} /></span></div>
        {experiment.hypothesis && (
          <div className="kv-row"><span>{t('experiments.hypothesis')}</span><span className="small-text">{experiment.hypothesis}</span></div>
        )}
        {experiment.stopped_reason && (
          <div className="kv-row"><span>{t('experiments.stoppedReason')}</span><span className="small-text">{experiment.stopped_reason}</span></div>
        )}
        {experiment.status === 'running' && (
          <button className="btn-danger" onClick={() => onStopExperiment(experiment.experiment_id)}>{t('experiments.stopExperiment')}</button>
        )}
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('experiments.guardrailsTitle')}</div>
        <div className="reason-list">
          {guardrails.map(code => (
            <span className="badge badge-muted reason-chip" key={code}>{guardrailLabel(code)}</span>
          ))}
          {!guardrails.length && <span className="muted-text small-text">{t('experiments.noGuardrails')}</span>}
        </div>
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('experiments.comparison')}</div>
        <div className="kv-row">
          <span>{t('experiments.comparisonStatus')}</span>
          <span className="badge badge-muted">{t(`experiments.comparisonStatuses.${comparison.status}`) === `experiments.comparisonStatuses.${comparison.status}` ? comparison.status : t(`experiments.comparisonStatuses.${comparison.status}`)}</span>
        </div>
        {comparison.leader && (
          <div className="kv-row">
            <span>{t('experiments.leader')}</span>
            <span className="mono">{comparison.leader} · {pct(comparison.leader_win_rate)}</span>
          </div>
        )}
        <div className="kv-row"><span>{t('experiments.minSamples')}</span><span className="mono">{comparison.min_samples}</span></div>
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('experiments.branchStats')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('experiments.branchKey')}</th>
                <th>{t('experiments.runs')}</th>
                <th>{t('experiments.winRate')}</th>
                <th>{t('experiments.participationRate')}</th>
                <th>{t('experiments.failureRate')}</th>
                <th>{t('experiments.riskRate')}</th>
                <th>{t('experiments.stopSignal')}</th>
                <th>{t('experiments.action')}</th>
              </tr>
            </thead>
            <tbody>
              {branches.map(branch => {
                const signal = stopSignals[branch.key] || {};
                return (
                  <tr key={branch.key}>
                    <td className="mono small-text">{branch.key}</td>
                    <td className="mono">{branch.runs}</td>
                    <td className="mono">{pct(branch.win_rate)}</td>
                    <td className="mono">{pct(branch.participation_rate)}</td>
                    <td className="mono">{pct(branch.failure_rate)}</td>
                    <td className="mono">{pct(branch.risk_rate)}</td>
                    <td>
                      {signal.should_stop ? (
                        <span className="badge badge-danger">{statLabel(signal.reason)}</span>
                      ) : (
                        <span className="badge badge-muted">{t('experiments.ok')}</span>
                      )}
                    </td>
                    <td><button className="btn-ghost" onClick={() => onStopBranch(experiment.experiment_id, branch.key)}>{t('experiments.stopBranch')}</button></td>
                  </tr>
                );
              })}
              {!branches.length && <tr><td className="empty-cell" colSpan="8">{t('experiments.noBranches')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <p className="muted-text small-text">{t('experiments.safetyNote')}</p>
    </div>
  );
}

function StatusBadgeInline({ status, t }) {
  const value = t(`status.${status}`);
  const label = value === `status.${status}` ? status : value;
  const cls = status === 'running' ? 'badge-executing' : status === 'stopped' ? 'badge-muted' : 'badge-muted';
  return <span className={`badge ${cls}`}>{label}</span>;
}
