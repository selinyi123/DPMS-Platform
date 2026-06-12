import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import { useUi } from '../uiContext';

const BAND_COLOR = { high: '#059669', moderate: '#2563eb', low: '#d97706', very_low: '#64748b' };

export default function Learning() {
  const { t } = useUi();
  const [model, setModel] = useState(null);
  const [predictions, setPredictions] = useState([]);
  const [error, setError] = useState('');

  const load = () => Promise.all([
    fetchJSON('/learning/model'),
    fetchJSON('/learning/predictions?limit=25'),
  ])
    .then(([modelData, predictionData]) => {
      setModel(modelData);
      setPredictions(predictionData.items || []);
      setError('');
    })
    .catch(err => setError(err.message));

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, []);

  const featureLabel = (name) => {
    const value = t(`learning.features.${name}`);
    return value === `learning.features.${name}` ? name : value;
  };
  const bandLabel = (band) => {
    const value = t(`learning.bands.${band}`);
    return value === `learning.bands.${band}` ? band : value;
  };

  const weights = model?.weights || {};
  const descriptions = model?.feature_descriptions || {};
  const weightRows = Object.entries(weights);

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('learning.eyebrow')}</p>
          <h1>{t('learning.title')}</h1>
        </div>
        <button className="btn-ghost" onClick={load}>{t('common.refresh')}</button>
      </header>
      <p className="muted-text small-text">{t('learning.subtitle')}</p>
      {error && <div className="alert-danger">{error}</div>}

      <div className="panel">
        <div className="panel-title">{t('learning.modelCard')}</div>
        {model ? (
          <>
            <div className="kv-row"><span>{t('learning.modelVersion')}</span><span className="mono">{model.model_version}</span></div>
            <div className="kv-row"><span>{t('learning.featureSetVersion')}</span><span className="mono">{model.feature_set_version}</span></div>
            <div className="kv-row"><span>{t('learning.modelType')}</span><span className="mono small-text">{model.type}</span></div>
            <div className="kv-row"><span>{t('learning.bias')}</span><span className="mono">{model.bias}</span></div>
            {model.notes && <p className="muted-text small-text">{model.notes}</p>}
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('learning.feature')}</th>
                    <th>{t('learning.weight')}</th>
                    <th>{t('learning.description')}</th>
                  </tr>
                </thead>
                <tbody>
                  {weightRows.map(([name, weight]) => (
                    <tr key={name}>
                      <td>{featureLabel(name)}</td>
                      <td className={`mono ${weight < 0 ? 'negative-value' : ''}`}>{weight}</td>
                      <td className="small-text muted-text">{descriptions[name] || '-'}</td>
                    </tr>
                  ))}
                  {!weightRows.length && <tr><td className="empty-cell" colSpan="3">{t('learning.noModel')}</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <div className="empty-cell">{t('learning.loadingModel')}</div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">{t('learning.predictions')}</div>
        {predictions.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('learning.lottery')}</th>
                  <th>{t('learning.platform')}</th>
                  <th>{t('learning.valueScore')}</th>
                  <th>{t('learning.probability')}</th>
                  <th>{t('learning.confidenceBand')}</th>
                  <th>{t('learning.topDrivers')}</th>
                </tr>
              </thead>
              <tbody>
                {predictions.map(item => (
                  <tr key={item.lottery_id}>
                    <td className="mono">L{item.lottery_id}</td>
                    <td>{item.platform}</td>
                    <td className="mono">{item.value_score}</td>
                    <td className="mono">{Math.round((item.probability || 0) * 100)}%</td>
                    <td>
                      <span className="badge" style={{ background: BAND_COLOR[item.confidence_band] || '#64748b', color: '#fff' }}>
                        {bandLabel(item.confidence_band)}
                      </span>
                    </td>
                    <td className="small-text">
                      {(item.top_drivers || []).slice(0, 2).map(driver => (
                        <span className="badge badge-muted reason-chip mono" key={driver.feature}>
                          {featureLabel(driver.feature)}: <span className={driver.contribution < 0 ? 'negative-value' : ''}>{driver.contribution}</span>
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-cell">{t('learning.noPredictions')}</div>
        )}
      </div>

      <p className="muted-text small-text">{t('learning.safetyNote')}</p>
    </section>
  );
}
