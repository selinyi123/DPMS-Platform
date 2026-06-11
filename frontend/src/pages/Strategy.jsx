import { useEffect, useMemo, useState } from 'react';

import { fetchJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import { useUi } from '../uiContext';

const TIER_ORDER = ['S', 'A', 'B', 'hold'];
const TIER_COLOR = { S: '#dc2626', A: '#d97706', B: '#2563eb', hold: '#64748b' };

export default function Strategy() {
  const { t, language } = useUi();
  const [items, setItems] = useState([]);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(null);
  const [explain, setExplain] = useState(null);
  const [explaining, setExplaining] = useState(false);

  const load = () => fetchJSON('/lotteries/strategy/queue?limit=50')
    .then(data => {
      setItems(data.items || []);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 8000);
    return () => clearInterval(timer);
  }, []);

  const openExplain = async (item) => {
    setSelected(item);
    setExplain(null);
    setExplaining(true);
    try {
      const detail = await fetchJSON(`/lotteries/${item.lottery_id}/strategy/explain`);
      setExplain(detail);
    } catch (err) {
      setError(err.message);
    } finally {
      setExplaining(false);
    }
  };

  const closeExplain = () => {
    setSelected(null);
    setExplain(null);
  };

  const grouped = useMemo(() => {
    const buckets = { S: [], A: [], B: [], hold: [] };
    for (const item of items) {
      const tier = buckets[item.priority_tier] ? item.priority_tier : 'hold';
      buckets[tier].push(item);
    }
    return buckets;
  }, [items]);

  const reasonLabel = (code) => {
    const value = t(`lotteries.strategyReasons.${code}`);
    return value === `lotteries.strategyReasons.${code}` ? code : value;
  };
  const modeLabel = (mode) => {
    const value = t(`lotteries.${mode}`);
    return value === `lotteries.${mode}` ? mode : value;
  };

  const cards = [
    { label: t('strategy.totalTargets'), value: items.length, unit: t('strategy.units.targets'), color: '#2563eb' },
    { label: t('strategy.actNow'), value: grouped.S.length, unit: t('strategy.units.tierS'), color: '#dc2626' },
    { label: t('strategy.strong'), value: grouped.A.length, unit: t('strategy.units.tierA'), color: '#d97706' },
    { label: t('strategy.holding'), value: grouped.hold.length, unit: t('strategy.units.hold'), color: '#64748b' },
  ];

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('strategy.eyebrow')}</p>
          <h1>{t('strategy.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('strategy.subtitle')}</p>
      {error && <div className="alert-danger">{error}</div>}

      <div className="metric-grid readiness-metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      {TIER_ORDER.map(tier => (
        <div className="panel" key={tier}>
          <div className="panel-title">
            <span className="badge" style={{ background: TIER_COLOR[tier], color: '#fff' }}>{tier}</span>
            {' '}{t(`strategy.tier.${tier}`)} · {grouped[tier].length}
          </div>
          {grouped[tier].length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('strategy.rank')}</th>
                    <th>{t('strategy.target')}</th>
                    <th>{t('strategy.score')}</th>
                    <th>{t('strategy.expectedValue')}</th>
                    <th>{t('strategy.mode')}</th>
                    <th>{t('strategy.reasons')}</th>
                    <th>{t('strategy.action')}</th>
                  </tr>
                </thead>
                <tbody>
                  {grouped[tier].map(item => (
                    <tr key={item.lottery_id}>
                      <td className="mono">#{item.rank}</td>
                      <td>
                        <div className="mono small-text">L{item.lottery_id} / {item.platform}</div>
                        <div className="small-text muted-text ellipsis-cell">{item.raw_url}</div>
                      </td>
                      <td className="mono">{item.strategy_score}</td>
                      <td className="mono">{item.expected_value}</td>
                      <td><span className="badge badge-muted">{modeLabel(item.recommended_mode)}</span></td>
                      <td className="small-text">
                        {(item.reason_codes || []).slice(0, 3).map(code => (
                          <span className="badge badge-muted reason-chip" key={code}>{reasonLabel(code)}</span>
                        ))}
                      </td>
                      <td><button className="btn-primary" onClick={() => openExplain(item)}>{t('strategy.explain')}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-cell">{t('strategy.emptyTier')}</div>
          )}
        </div>
      ))}

      {selected && (
        <div className="drawer-backdrop" onClick={closeExplain}>
          <aside className="drawer-panel" onClick={e => e.stopPropagation()}>
            <header className="drawer-header">
              <div>
                <p className="eyebrow">{t('strategy.explainTitle')}</p>
                <h2>L{selected.lottery_id} · {selected.platform}</h2>
              </div>
              <button className="btn-ghost" onClick={closeExplain}>{t('common.cancel')}</button>
            </header>
            {explaining && <div className="muted-text">{t('strategy.explaining')}</div>}
            {explain && <ExplainBody explain={explain} t={t} reasonLabel={reasonLabel} modeLabel={modeLabel} language={language} />}
          </aside>
        </div>
      )}
    </section>
  );
}

function ExplainBody({ explain, t, reasonLabel, modeLabel, language }) {
  const block = explain.explain || {};
  const score = block.score || {};
  const components = score.components || {};
  const win = block.win_probability || {};
  const trust = block.trust_score || {};
  const knowledge = block.knowledge_confidence || {};
  const mode = block.mode || {};

  const componentRows = Object.entries(components);
  const knowledgeRows = Object.entries(knowledge.components || {});

  return (
    <div className="drawer-body">
      <div className="panel-subtle">
        <div className="panel-title">{t('strategy.modeDecision')}</div>
        <div className="kv-row"><span>{t('strategy.mode')}</span><span className="badge badge-muted">{modeLabel(mode.recommended_mode)}</span></div>
        <div className="kv-row"><span>{t('strategy.score')}</span><span className="mono">{score.total}</span></div>
        <div className="reason-list">
          {(mode.reason_codes || []).map(code => (
            <span className="badge badge-muted reason-chip" key={code}>{reasonLabel(code)}</span>
          ))}
        </div>
        {(mode.blockers || []).length > 0 && (
          <div className="alert-danger small-text">{(mode.blockers || []).join('; ')}</div>
        )}
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('strategy.scoreBreakdown')}</div>
        <div className="kv-row"><span>{t('strategy.modeMultiplier')}</span><span className="mono">{score.mode_multiplier}</span></div>
        {componentRows.map(([name, value]) => (
          <div className="kv-row" key={name}>
            <span>{t(`strategy.components.${name}`) === `strategy.components.${name}` ? name : t(`strategy.components.${name}`)}</span>
            <span className={`mono ${value < 0 ? 'negative-value' : ''}`}>{value}</span>
          </div>
        ))}
        <div className="kv-row total-row"><span>{t('strategy.total')}</span><span className="mono">{score.total}</span></div>
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('strategy.winProbability')}</div>
        <div className="kv-row"><span>{t('strategy.baseline')}</span><span className="mono">{win.baseline}</span></div>
        <div className="kv-row"><span>{t('strategy.observedRate')}</span><span className="mono">{win.observed_win_rate ?? '-'}</span></div>
        <div className="kv-row"><span>{t('strategy.confidenceWeight')}</span><span className="mono">{win.confidence_weight}</span></div>
        <div className="kv-row total-row"><span>{t('strategy.blended')}</span><span className="mono">{win.blended}</span></div>
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('strategy.trustScore')}</div>
        <div className="kv-row"><span>{t('strategy.accountTrust')}</span><span className="mono">{trust.account_trust}</span></div>
        <div className="kv-row"><span>{t('strategy.confidenceBonus')}</span><span className="mono">{trust.confidence_bonus}</span></div>
        <div className="kv-row"><span>{t('strategy.riskPenalty')}</span><span className="mono negative-value">{trust.risk_penalty}</span></div>
        <div className="kv-row total-row"><span>{t('strategy.trustTotal')}</span><span className="mono">{trust.trust_score}</span></div>
      </div>

      <div className="panel-subtle">
        <div className="panel-title">{t('strategy.knowledgeConfidence')} · {knowledge.total}</div>
        {knowledgeRows.map(([name, value]) => (
          <div className="kv-row" key={name}>
            <span>{t(`strategy.knowledgeParts.${name}`) === `strategy.knowledgeParts.${name}` ? name : t(`strategy.knowledgeParts.${name}`)}</span>
            <span className="mono">{value}</span>
          </div>
        ))}
      </div>

      <p className="muted-text small-text">{t('strategy.safetyNote')}</p>
    </div>
  );
}
