import { useEffect, useState } from 'react';

import { fetchJSON, postJSON } from '../api';
import { useUi } from '../uiContext';

export default function Governance() {
  const { t, notify } = useUi();
  const [policy, setPolicy] = useState(null);
  const [versions, setVersions] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [error, setError] = useState('');
  const [lotteryId, setLotteryId] = useState('');
  const [evalResult, setEvalResult] = useState(null);
  const [evaluating, setEvaluating] = useState(false);
  const [replayResults, setReplayResults] = useState({});

  const load = () => Promise.all([
    fetchJSON('/governance/policy'),
    fetchJSON('/governance/policies'),
    fetchJSON('/governance/decisions?limit=50'),
  ])
    .then(([policyData, versionData, decisionData]) => {
      setPolicy(policyData);
      setVersions(versionData.items || []);
      setDecisions(decisionData.items || []);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, []);

  const gateLabel = (code) => {
    const value = t(`governance.gates.${code}.label`);
    return value === `governance.gates.${code}.label` ? code : value;
  };
  const gateRemediation = (code) => {
    const value = t(`governance.gates.${code}.remediation`);
    return value === `governance.gates.${code}.remediation` ? code : value;
  };
  const remediationLabel = (code) => {
    const value = t(`governance.remediations.${code}`);
    return value === `governance.remediations.${code}` ? code : value;
  };
  const outcomeLabel = (outcome) => {
    const value = t(`governance.outcomes.${outcome}`);
    return value === `governance.outcomes.${outcome}` ? outcome : value;
  };

  const evaluateGate = async () => {
    const id = parseInt(lotteryId, 10);
    if (!Number.isFinite(id) || id <= 0) {
      notify(t('governance.invalidLotteryId'), 'error');
      return;
    }
    setEvaluating(true);
    setEvalResult(null);
    try {
      const result = await fetchJSON(`/governance/real-run/${id}`);
      setEvalResult(result);
      await load();
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setEvaluating(false);
    }
  };

  const replayDecision = async (decisionId) => {
    try {
      const result = await postJSON(`/governance/decisions/${decisionId}/replay`, {}, { confirm: true });
      setReplayResults(prev => ({ ...prev, [decisionId]: result }));
      const summary = result.matches
        ? t('governance.replayMatch')
        : t('governance.replayMismatch');
      notify(`${summary} (${result.recomputed_outcome} / ${result.recorded_outcome})`, result.matches ? 'success' : 'error');
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('governance.eyebrow')}</p>
          <h1>{t('governance.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('governance.subtitle')}</p>
      {error && <div className="alert-danger">{error}</div>}

      <div className="panel">
        <div className="panel-title">{t('governance.activePolicy')}</div>
        {policy ? (
          <>
            <div className="kv-row"><span>{t('governance.policyKey')}</span><span className="mono">{policy.policy_key}</span></div>
            <div className="kv-row"><span>{t('governance.policyVersion')}</span><span className="mono">{policy.version}</span></div>
            {policy.description && <p className="muted-text small-text">{policy.description}</p>}
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('governance.gateCode')}</th>
                    <th>{t('governance.required')}</th>
                    <th>{t('governance.remediation')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(policy.gates || []).map(gate => (
                    <tr key={gate.code}>
                      <td>{gateLabel(gate.code)}</td>
                      <td>{gate.required ? t('governance.yes') : t('governance.no')}</td>
                      <td className="small-text">{remediationLabel(gate.remediation)}</td>
                    </tr>
                  ))}
                  {!(policy.gates || []).length && <tr><td className="empty-cell" colSpan="3">{t('governance.noGates')}</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <div className="empty-cell">{t('governance.loadingPolicy')}</div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">{t('governance.policyVersions')}</div>
        {versions.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('governance.policyVersion')}</th>
                  <th>{t('governance.note')}</th>
                  <th>{t('governance.active')}</th>
                  <th>{t('governance.createdAt')}</th>
                </tr>
              </thead>
              <tbody>
                {versions.map(v => (
                  <tr key={`${v.policy_key}-${v.version}`}>
                    <td className="mono">{v.version}</td>
                    <td className="small-text">{v.note || '-'}</td>
                    <td>{v.active ? <span className="badge badge-ready">{t('governance.yes')}</span> : <span className="badge badge-muted">{t('governance.no')}</span>}</td>
                    <td className="small-text muted-text">{v.created_at || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-cell">{t('governance.noVersions')}</div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">{t('governance.evaluateGate')}</div>
        <p className="muted-text small-text">{t('governance.evaluateHint')}</p>
        <div className="inline-edit">
          <input
            type="number"
            min="1"
            className="input compact-input"
            placeholder={t('governance.lotteryIdPlaceholder')}
            value={lotteryId}
            onChange={e => setLotteryId(e.target.value)}
          />
          <button className="btn-primary" onClick={evaluateGate} disabled={evaluating}>
            {evaluating ? t('governance.evaluating') : t('governance.evaluate')}
          </button>
        </div>
        {evalResult && (
          <div className="panel-subtle">
            <div className="kv-row">
              <span>{t('governance.outcome')}</span>
              <span className={`badge ${evalResult.outcome === 'allow' ? 'badge-ready' : 'badge-danger'}`}>{outcomeLabel(evalResult.outcome)}</span>
            </div>
            <div className="kv-row"><span>{t('governance.policyVersion')}</span><span className="mono">{evalResult.policy_version}</span></div>
            <div className="kv-row"><span>{t('governance.decisionId')}</span><span className="mono small-text">{evalResult.decision_id}</span></div>
            <div className="panel-title">{t('governance.gateChecklist')}</div>
            <div className="reason-list">
              {Object.entries(evalResult.inputs || {}).map(([code, pass]) => (
                <span className={`badge reason-chip ${pass ? 'badge-ready' : 'badge-danger'}`} key={code}>
                  {pass ? '✓' : '✗'} {gateLabel(code)}
                </span>
              ))}
            </div>
            {(evalResult.reasons || []).length > 0 && (
              <>
                <div className="panel-title">{t('governance.failedReasons')}</div>
                <div className="reason-list">
                  {(evalResult.reasons || []).map(reason => (
                    <span className="badge badge-danger reason-chip" key={reason.code}>
                      {gateLabel(reason.code)} → {remediationLabel(reason.remediation)}
                    </span>
                  ))}
                </div>
              </>
            )}
            <div className="kv-row"><span>{t('governance.nextAction')}</span><span className="small-text">{remediationLabel(evalResult.next_action)}</span></div>
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">{t('governance.recentDecisions')}</div>
        {decisions.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('governance.decisionId')}</th>
                  <th>{t('governance.subject')}</th>
                  <th>{t('governance.outcome')}</th>
                  <th>{t('governance.createdAt')}</th>
                  <th>{t('governance.action')}</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map(d => {
                  const replay = replayResults[d.decision_id];
                  return (
                    <tr key={d.decision_id}>
                      <td className="mono small-text">{d.decision_id.slice(0, 8)}</td>
                      <td className="mono">{d.subject_type} #{d.subject_id}</td>
                      <td><span className={`badge ${d.outcome === 'allow' ? 'badge-ready' : 'badge-danger'}`}>{outcomeLabel(d.outcome)}</span></td>
                      <td className="small-text muted-text">{d.created_at || '-'}</td>
                      <td>
                        <button className="btn-ghost" onClick={() => replayDecision(d.decision_id)}>{t('governance.replay')}</button>
                        {replay && (
                          <div className="small-text muted-text">
                            {replay.matches ? t('governance.replayMatch') : t('governance.replayMismatch')}
                            {' '}({replay.recomputed_outcome} / {replay.recorded_outcome})
                            {' · '}
                            {replay.version_matches ? t('governance.versionMatch') : t('governance.versionMismatch')}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-cell">{t('governance.noDecisions')}</div>
        )}
      </div>
    </section>
  );
}
