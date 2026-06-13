import { useEffect, useState } from 'react';

import { fetchJSON, postJSON } from '../api';
import { useUi } from '../uiContext';

const POLICY_KEY = 'real_run_gate';
const REASON_CODES = ['experiment_result', 'risk_tightening', 'manual_review', 'incident_response', 'scheduled_review'];

export default function TransitionGraph() {
  const { t, notify } = useUi();
  const [lineage, setLineage] = useState(null);
  const [activePolicy, setActivePolicy] = useState(null);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState({});
  const [definitionJson, setDefinitionJson] = useState('');
  const [reasonCode, setReasonCode] = useState('manual_review');
  const [rollbackCondition, setRollbackCondition] = useState('');
  const [loosening, setLoosening] = useState('');
  const [note, setNote] = useState('');
  const [publishing, setPublishing] = useState(false);
  const [publishResult, setPublishResult] = useState(null);
  const [activatingVersion, setActivatingVersion] = useState(null);

  const load = () => Promise.all([
    fetchJSON(`/transitions/${POLICY_KEY}/lineage`),
    fetchJSON('/governance/policy'),
  ])
    .then(([lineageData, policyData]) => {
      setLineage(lineageData);
      setActivePolicy(policyData);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 20000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (activePolicy && !definitionJson) {
      seedFromActive();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePolicy]);

  const seedFromActive = () => {
    if (!activePolicy) return;
    const draft = { ...activePolicy, version: (activePolicy.version || 1) + 1 };
    delete draft.description;
    setDefinitionJson(JSON.stringify({ ...activePolicy, ...draft }, null, 2));
  };

  const toggleExpand = (toVersion) => {
    setExpanded(prev => ({ ...prev, [toVersion]: !prev[toVersion] }));
  };

  const classificationBadge = (classification) => {
    const cls = classification === 'tightening' ? 'badge-ready' : classification === 'loosening' ? 'badge-danger' : 'badge-muted';
    return <span className={`badge ${cls}`}>{t(`transitions.classifications.${classification}`)}</span>;
  };

  const reasonLabel = (code) => {
    const value = t(`transitions.reasons.${code}`);
    return value === `transitions.reasons.${code}` ? code : value;
  };

  const publish = async () => {
    let definition;
    try {
      definition = JSON.parse(definitionJson);
    } catch {
      notify(t('transitions.invalidJson'), 'error');
      return;
    }
    if (!rollbackCondition.trim()) {
      notify(t('transitions.rollbackRequired'), 'error');
      return;
    }
    setPublishing(true);
    setPublishResult(null);
    try {
      const result = await postJSON('/governance/policies', {
        policy_key: POLICY_KEY,
        definition,
        reason_code: reasonCode,
        rollback_condition: rollbackCondition,
        loosening_justification: loosening.trim() || null,
        note: note.trim() || null,
      }, { confirm: true });
      setPublishResult(result);
      notify(t('transitions.publishSuccess'), 'success');
      await load();
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setPublishing(false);
    }
  };

  const activate = async (version) => {
    setActivatingVersion(version);
    try {
      await postJSON(`/governance/policies/${version}/activate?policy_key=${POLICY_KEY}`, { note: null }, { confirm: true });
      notify(t('transitions.activateSuccess'), 'success');
      await load();
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setActivatingVersion(null);
    }
  };

  const chain = lineage?.chain || [];
  const activeVersion = lineage?.active_version;
  const rootVersion = chain.length ? chain[0].from_version : activeVersion;

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('transitions.eyebrow')}</p>
          <h1>{t('transitions.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('transitions.subtitle')}</p>
      <div className="alert-danger">{t('transitions.safetyBanner')}</div>
      {error && <div className="alert-danger">{error}</div>}

      <div className="panel">
        <div className="panel-title">{t('transitions.chainTitle')}</div>
        <div className="kv-row"><span>{t('transitions.activeVersion')}</span><span className="mono">{activeVersion ?? '-'}</span></div>
        {!chain.length && <div className="empty-cell">{t('transitions.noChain')}</div>}
        <div className="transition-chain">
          {rootVersion != null && (
            <div className="transition-node">
              <span className="mono">v{rootVersion}</span>
              {activeVersion === rootVersion && <span className="badge badge-ready">{t('transitions.active')}</span>}
            </div>
          )}
          {chain.map(tr => {
            const isActive = activeVersion === tr.to_version;
            const isExpanded = !!expanded[tr.to_version];
            return (
              <div key={tr.to_version} className="transition-edge-group">
                <button type="button" className="btn-ghost transition-edge" onClick={() => toggleExpand(tr.to_version)}>
                  {classificationBadge(tr.classification)}
                  <span className="small-text">{reasonLabel(tr.reason_code)}</span>
                  {tr.requires_extra_review ? <span className="badge badge-danger">{t('transitions.extraReview')}</span> : null}
                  <span className="small-text muted-text">{tr.created_at || '-'}</span>
                  <span className="small-text">{isExpanded ? t('transitions.hideDiff') : t('transitions.viewDiff')}</span>
                </button>
                {isExpanded && (
                  <div className="panel-subtle">
                    <div className="kv-row"><span>{t('transitions.rollbackCondition')}</span><span className="small-text">{tr.rollback_condition || '-'}</span></div>
                    {tr.loosening_justification && (
                      <div className="kv-row"><span>{t('transitions.loosJustification')}</span><span className="small-text">{tr.loosening_justification}</span></div>
                    )}
                    <div className="panel-title">{t('transitions.addedGates')}</div>
                    {tr.diff?.added_gates?.length ? (
                      <div className="reason-list">
                        {tr.diff.added_gates.map(code => (
                          <span className="badge reason-chip badge-info" key={code}>
                            {code} ({tr.diff.added_gates_required?.[code] ? t('transitions.required') : t('transitions.optional')})
                          </span>
                        ))}
                      </div>
                    ) : <div className="empty-cell">{t('transitions.noGateChanges')}</div>}
                    <div className="panel-title">{t('transitions.removedGates')}</div>
                    {tr.diff?.removed_gates?.length ? (
                      <div className="reason-list">
                        {tr.diff.removed_gates.map(code => (
                          <span className="badge reason-chip badge-danger" key={code}>{code}</span>
                        ))}
                      </div>
                    ) : <div className="empty-cell">{t('transitions.noGateChanges')}</div>}
                    <div className="panel-title">{t('transitions.changedGates')}</div>
                    {tr.diff?.changed_required?.length ? (
                      <div className="reason-list">
                        {tr.diff.changed_required.map(code => (
                          <span className="badge reason-chip badge-warn" key={code}>
                            {code} → {tr.diff.changed_required_to?.[code] ? t('transitions.required') : t('transitions.optional')}
                          </span>
                        ))}
                      </div>
                    ) : <div className="empty-cell">{t('transitions.noGateChanges')}</div>}
                    <div className="panel-title">{t('transitions.impactScope')}</div>
                    <p className="muted-text small-text">{tr.impact_scope?.description || t('transitions.noGateChanges')}</p>
                  </div>
                )}
                <div className="transition-node">
                  <span className="mono">v{tr.to_version}</span>
                  {isActive && <span className="badge badge-ready">{t('transitions.active')}</span>}
                  {!isActive && tr.activated_at && <span className="badge badge-muted">{t('transitions.activated')}</span>}
                  {!isActive && !tr.activated_at && (
                    <>
                      <span className="badge badge-warn">{t('transitions.pendingActivation')}</span>
                      <button
                        className="btn-primary"
                        onClick={() => activate(tr.to_version)}
                        disabled={activatingVersion === tr.to_version}
                      >
                        {activatingVersion === tr.to_version ? t('transitions.activating') : t('transitions.activate')}
                      </button>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('transitions.publishTitle')}</div>
        <p className="muted-text small-text">{t('transitions.publishHint')}</p>
        <div className="stack-form">
          <label>
            <span>{t('transitions.definitionLabel')}</span>
            <textarea
              className="input textarea code-textarea"
              value={definitionJson}
              onChange={e => setDefinitionJson(e.target.value)}
              rows={12}
            />
          </label>
          <div className="toolbar">
            <button className="btn-ghost" type="button" onClick={seedFromActive}>{t('transitions.seedFromActive')}</button>
          </div>
          <label>
            <span>{t('transitions.reasonCodeLabel')}</span>
            <select className="input" value={reasonCode} onChange={e => setReasonCode(e.target.value)}>
              {REASON_CODES.map(code => <option key={code} value={code}>{reasonLabel(code)}</option>)}
            </select>
          </label>
          <label>
            <span>{t('transitions.rollbackLabel')}</span>
            <input
              className="input"
              value={rollbackCondition}
              onChange={e => setRollbackCondition(e.target.value)}
              placeholder={t('transitions.rollbackPlaceholder')}
            />
          </label>
          <label>
            <span>{t('transitions.loosJustificationLabel')}</span>
            <textarea
              className="input textarea"
              value={loosening}
              onChange={e => setLoosening(e.target.value)}
              rows={3}
            />
          </label>
          <label>
            <span>{t('transitions.noteLabel')}</span>
            <input className="input" value={note} onChange={e => setNote(e.target.value)} />
          </label>
          <div className="toolbar">
            <button className="btn-primary" onClick={publish} disabled={publishing}>
              {publishing ? t('transitions.publishing') : t('transitions.publish')}
            </button>
          </div>
        </div>
        {publishResult && (
          <div className="panel-subtle">
            <div className="panel-title">{t('transitions.publishResult')}</div>
            <div className="kv-row"><span>{t('governance.policyVersion')}</span><span className="mono">{publishResult.from_version} → {publishResult.to_version}</span></div>
            <div className="kv-row"><span>{t('transitions.classification')}</span>{classificationBadge(publishResult.classification)}</div>
            {publishResult.requires_extra_review ? <span className="badge badge-danger">{t('transitions.extraReview')}</span> : null}
            <p className="muted-text small-text">{publishResult.impact_scope?.description}</p>
          </div>
        )}
      </div>
    </section>
  );
}
