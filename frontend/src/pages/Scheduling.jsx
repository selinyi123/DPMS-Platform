import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import { useUi } from '../uiContext';

export default function Scheduling() {
  const { t } = useUi();
  const [windowMinutes, setWindowMinutes] = useState(240);
  const [plan, setPlan] = useState(null);
  const [limits, setLimits] = useState({});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const tf = (key, raw) => {
    const value = t(key);
    return value === key ? raw : value;
  };

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [planData, limitData] = await Promise.all([
        fetchJSON(`/scheduling/plan?window_minutes=${windowMinutes}`),
        fetchJSON('/scheduling/limits'),
      ]);
      setPlan(planData);
      setLimits(limitData.limits || {});
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

  const slots = plan?.slots || [];
  const unscheduled = plan?.unscheduled || [];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('scheduling.eyebrow')}</p>
          <h1>{t('scheduling.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>{t('scheduling.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('scheduling.subtitle')}</p>
      <div className="alert-warn small-text">{t('scheduling.safetyBanner')}</div>

      <div className="panel">
        <div className="stack-form">
          <label>
            <span>{t('scheduling.window')}</span>
            <input
              className="input" type="number" min="15" max="1440"
              value={windowMinutes}
              onChange={e => setWindowMinutes(Number(e.target.value))}
              onKeyDown={e => e.key === 'Enter' && load()}
            />
          </label>
          <div className="toolbar">
            <button className="btn-primary" onClick={load} disabled={loading}>{t('scheduling.refresh')}</button>
          </div>
        </div>
        {error && <div className="alert-danger">{error}</div>}
      </div>

      {plan && (
        <>
          <div className="metric-grid readiness-metric-grid">
            <Metric label={t('scheduling.planned')} value={plan.summary?.planned ?? 0} />
            <Metric label={t('scheduling.unscheduled')} value={plan.summary?.unscheduled ?? 0} />
            <Metric label={t('scheduling.dispatchable')} value={plan.dispatchable_accounts ?? 0} />
            <Metric label={t('scheduling.candidates')} value={plan.candidate_count ?? 0} />
          </div>

          {(plan.overloaded_platforms || []).length > 0 && (
            <div className="alert-warn small-text">
              {t('scheduling.overloaded')}: {plan.overloaded_platforms.join(', ')}
            </div>
          )}

          <div className="panel">
            <div className="panel-title">{t('scheduling.slotsTitle')}</div>
            {slots.length ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('scheduling.scheduledAt')}</th>
                      <th>{t('scheduling.platform')}</th>
                      <th>{t('scheduling.lottery')}</th>
                      <th>{t('scheduling.account')}</th>
                      <th>{t('scheduling.offset')}</th>
                      <th>{t('scheduling.mode')}</th>
                      <th>{t('scheduling.value')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slots.map((s, i) => (
                      <tr key={`${s.lottery_id}-${i}`}>
                        <td className="mono small-text">{(s.scheduled_at || '').replace('T', ' ').slice(0, 16)}</td>
                        <td>{s.platform}</td>
                        <td className="mono">L{s.lottery_id}</td>
                        <td className="mono">A{s.account_id}</td>
                        <td className="mono">+{s.scheduled_at_offset_min}</td>
                        <td><span className="badge badge-muted">{t('scheduling.modeGated')}</span></td>
                        <td className="mono">{s.value_score}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div className="empty-cell">{t('scheduling.noData')}</div>}
          </div>

          {unscheduled.length > 0 && (
            <div className="panel">
              <div className="panel-title">{t('scheduling.unscheduledTitle')}</div>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('scheduling.lottery')}</th>
                      <th>{t('scheduling.platform')}</th>
                      <th>{t('scheduling.reason')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {unscheduled.map((u, i) => (
                      <tr key={`${u.lottery_id}-${i}`}>
                        <td className="mono">L{u.lottery_id}</td>
                        <td>{u.platform}</td>
                        <td className="small-text">{tf(`scheduling.reasons.${u.reason}`, u.reason)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}

      <div className="panel">
        <div className="panel-title">{t('scheduling.limitsTitle')}</div>
        {Object.keys(limits).length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('scheduling.platform')}</th>
                  <th>{t('scheduling.maxDaily')}</th>
                  <th>{t('scheduling.spacing')}</th>
                  <th>{t('scheduling.perWindow')}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(limits).map(([platform, lim]) => (
                  <tr key={platform}>
                    <td>{platform}</td>
                    <td className="mono">{lim.max_daily_per_account}</td>
                    <td className="mono">{lim.min_spacing_min}</td>
                    <td className="mono">{lim.max_per_window}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-cell">{t('scheduling.noData')}</div>}
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
