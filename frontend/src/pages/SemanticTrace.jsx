import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import { useUi } from '../uiContext';

export default function SemanticTrace() {
  const { t, language, pageParams } = useUi();
  const [subjectId, setSubjectId] = useState('');
  const [trace, setTrace] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const lookup = async (rawId) => {
    const id = String(rawId ?? subjectId).trim();
    if (!id) return;
    setSubjectId(id);
    setLoading(true);
    setError('');
    try {
      const data = await fetchJSON(`/semantic/trace/lottery/${encodeURIComponent(id)}`);
      setTrace(data);
    } catch (err) {
      setError(err.message);
      setTrace(null);
    } finally {
      setLoading(false);
    }
  };

  // Deep link from another page (e.g. Governance) pre-fills and auto-traces.
  useEffect(() => {
    const deepId = pageParams?.subjectId;
    if (deepId != null && String(deepId).trim() !== '') {
      lookup(deepId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageParams]);

  const onKeyDown = (e) => {
    if (e.key === 'Enter') lookup();
  };

  const narrative = trace?.narrative?.[language] || trace?.narrative?.en || [];
  const hints = trace?.consistency_checks || [];
  const intent = trace?.intent || {};
  const institution = trace?.institution || {};
  const policy = trace?.policy || {};
  const transition = trace?.transition || {};
  const execution = trace?.execution || {};
  const lineage = transition.lineage || [];
  const latestHop = lineage.length ? lineage[lineage.length - 1] : null;
  const hasIntent = intent.strategy_score != null || intent.priority_tier != null;
  const failedGates = (policy.reasons || [])
    .map(r => (typeof r === 'string' ? r : r?.code))
    .filter(Boolean);
  const topDrivers = (intent.top_drivers || [])
    .map(d => (typeof d === 'string' ? d : d?.feature))
    .filter(Boolean);

  const kv = (label, value) => (
    <div className="kv-row"><span>{label}</span><span className="mono">{value ?? '-'}</span></div>
  );

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('semantic.eyebrow')}</p>
          <h1>{t('semantic.title')}</h1>
        </div>
      </header>
      <p className="muted-text small-text">{t('semantic.subtitle')}</p>
      <div className="alert-danger">{t('semantic.safetyBanner')}</div>

      <div className="panel">
        <div className="stack-form">
          <label>
            <span>{t('semantic.lookupLabel')}</span>
            <input
              className="input"
              value={subjectId}
              onChange={e => setSubjectId(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={t('semantic.lookupPlaceholder')}
              inputMode="numeric"
            />
          </label>
          <div className="toolbar">
            <button className="btn-primary" onClick={lookup} disabled={loading}>
              {loading ? t('semantic.loading') : t('semantic.lookup')}
            </button>
          </div>
        </div>
        {error && <div className="alert-danger">{error}</div>}
      </div>

      {!trace && !error && <div className="empty-cell">{t('semantic.noTrace')}</div>}

      {trace && (
        <>
          <div className="panel">
            <div className="panel-title">{t('semantic.narrativeTitle')}</div>
            <ol className="reason-list-block">
              {narrative.map((line, idx) => <li key={idx} className="small-text">{line}</li>)}
            </ol>
          </div>

          <div className="panel">
            <div className="panel-title">{t('semantic.consistencyTitle')}</div>
            {hints.length ? (
              hints.map((hint, idx) => <div key={idx} className="alert-warn small-text">{hint}</div>)
            ) : (
              <div className="empty-cell">{t('semantic.noConsistency')}</div>
            )}
          </div>

          <div className="semantic-grid">
            <div className="panel semantic-card">
              <div className="panel-title">{t('semantic.layers.intent')}</div>
              {hasIntent ? (
                <>
                  {kv(t('semantic.intent.strategyScore'), intent.strategy_score)}
                  {kv(t('semantic.intent.priorityTier'), intent.priority_tier)}
                  {kv(t('semantic.intent.recommendedMode'), intent.recommended_mode)}
                  {intent.probability != null && kv(t('semantic.intent.probability'), intent.probability)}
                  {topDrivers.length > 0 && (
                    <div className="kv-row">
                      <span>{t('semantic.intent.topDrivers')}</span>
                      <span className="reason-list">
                        {topDrivers.map(d => <span className="badge reason-chip badge-info" key={d}>{d}</span>)}
                      </span>
                    </div>
                  )}
                </>
              ) : <div className="empty-cell">{t('semantic.intent.none')}</div>}
            </div>

            <div className="panel semantic-card">
              <div className="panel-title">{t('semantic.layers.institution')}</div>
              {institution.policy_key ? (
                <>
                  {kv(t('semantic.institution.policyKey'), institution.policy_key)}
                  {kv(t('semantic.institution.activeVersion'), institution.active_version != null ? `v${institution.active_version}` : '-')}
                </>
              ) : <div className="empty-cell">{t('semantic.institution.none')}</div>}
            </div>

            <div className="panel semantic-card">
              <div className="panel-title">{t('semantic.layers.policy')}</div>
              {policy.outcome ? (
                <>
                  <div className="kv-row">
                    <span>{t('semantic.policy.outcome')}</span>
                    <span className={`badge ${policy.outcome === 'allow' ? 'badge-ready' : 'badge-danger'}`}>{policy.outcome}</span>
                  </div>
                  {kv(t('semantic.policy.decisionVersion'), policy.policy_version != null ? `v${policy.policy_version}` : '-')}
                  {kv(t('semantic.policy.decisionId'), policy.decision_id)}
                  {failedGates.length > 0 && (
                    <div className="kv-row">
                      <span>{t('semantic.policy.failedGates')}</span>
                      <span className="reason-list">
                        {failedGates.map(g => <span className="badge reason-chip badge-danger" key={g}>{g}</span>)}
                      </span>
                    </div>
                  )}
                </>
              ) : <div className="empty-cell">{t('semantic.policy.none')}</div>}
            </div>

            <div className="panel semantic-card">
              <div className="panel-title">{t('semantic.layers.transition')}</div>
              {lineage.length ? (
                <>
                  {kv(t('semantic.transition.activeVersion'), transition.active_version != null ? `v${transition.active_version}` : '-')}
                  {kv(t('semantic.transition.hops'), lineage.length)}
                  {latestHop && (
                    <div className="kv-row">
                      <span>{t('semantic.transition.latest')}</span>
                      <span className="reason-list">
                        <span className="mono">v{latestHop.from_version}→v{latestHop.to_version}</span>
                        <span className={`badge ${latestHop.classification === 'tightening' ? 'badge-ready' : latestHop.classification === 'loosening' ? 'badge-danger' : 'badge-muted'}`}>
                          {t(`transitions.classifications.${latestHop.classification}`)}
                        </span>
                      </span>
                    </div>
                  )}
                </>
              ) : <div className="empty-cell">{t('semantic.transition.none')}</div>}
            </div>

            <div className="panel semantic-card">
              <div className="panel-title">{t('semantic.layers.execution')}</div>
              {(execution.status || (execution.task_runs || []).length) ? (
                <>
                  {kv(t('semantic.execution.status'), execution.status)}
                  {kv(t('semantic.execution.runs'), execution.count ?? (execution.task_runs || []).length)}
                  {execution.latest_run_status && kv(t('semantic.execution.latestRun'), execution.latest_run_status)}
                </>
              ) : <div className="empty-cell">{t('semantic.execution.none')}</div>}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
