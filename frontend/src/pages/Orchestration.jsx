import { useEffect, useState } from 'react';

import { fetchJSON, postJSON } from '../api';
import { useUi } from '../uiContext';

const MODE_COLOR = { dry_run: '#64748b', shadow_run: '#2563eb', real_run: '#dc2626' };

export default function Orchestration() {
  const { t } = useUi();
  const [data, setData] = useState(null);
  const [drafts, setDrafts] = useState([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [campaignResult, draftsResult] = await Promise.allSettled([
        fetchJSON('/orchestration/campaign'),
        fetchJSON('/orchestration/drafts'),
      ]);
      if (campaignResult.status === 'fulfilled') {
        setData(campaignResult.value);
      } else {
        setData(null);
      }
      if (draftsResult.status === 'fulfilled') {
        setDrafts(draftsResult.value.items || []);
      } else {
        setDrafts([]);
      }
      const failures = [campaignResult, draftsResult]
        .filter(result => result.status === 'rejected')
        .map(result => result.reason?.message || String(result.reason));
      if (failures.length) setError(failures.join(' · '));
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  };

  const saveDraft = async () => {
    setError('');
    setNotice('');
    try {
      await postJSON('/orchestration/campaign/draft', { campaign_key: 'console' });
      setNotice(t('orchestration.draftSaved'));
      load();
    } catch (err) {
      setError(err.message);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const plan = data?.plan;
  const waves = plan?.waves || [];
  const summary = plan?.summary || {};
  const validation = data?.validation || { valid: true, errors: [] };
  const modeName = (mode) => t(`orchestration.modeNames.${mode}`);

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('orchestration.eyebrow')}</p>
          <h1>{t('orchestration.title')}</h1>
        </div>
        <div className="toolbar">
          <button className="btn-ghost" onClick={load} disabled={loading}>{t('orchestration.refresh')}</button>
          <button className="btn-primary" onClick={saveDraft}>{t('orchestration.saveDraft')}</button>
        </div>
      </header>
      <p className="muted-text small-text">{t('orchestration.subtitle')}</p>
      <div className="alert-warn small-text">{t('orchestration.safetyBanner')}</div>
      {error && <div className="alert-danger">{error}</div>}
      {notice && <div className="alert-ok small-text">{notice}</div>}

      {data && (
        <>
          <div className="metric-grid readiness-metric-grid">
            <Metric label={t('orchestration.targets')} value={summary.total ?? 0} />
            <Metric label={t('orchestration.waveCount')} value={summary.wave_count ?? 0} />
            <Metric label={modeName('real_run')} value={summary.by_mode?.real_run ?? 0} />
            <Metric label={modeName('shadow_run')} value={summary.by_mode?.shadow_run ?? 0} />
          </div>

          <div className={validation.valid ? 'alert-ok small-text' : 'alert-danger small-text'}>
            {validation.valid ? t('orchestration.validTrue') : t('orchestration.validFalse')}
            {summary.requires_review ? ` · ${t('orchestration.requiresReview')}` : ''}
          </div>
          {!validation.valid && (
            <div className="panel">
              <div className="panel-title">{t('orchestration.validationErrors')}</div>
              {validation.errors.map((e, i) => <div key={i} className="alert-danger small-text">{e}</div>)}
            </div>
          )}

          <div className="panel">
            <div className="panel-title">{t('orchestration.wavesTitle')}</div>
            {waves.length ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('orchestration.wave')}</th>
                      <th>{t('orchestration.mode')}</th>
                      <th>{t('orchestration.size')}</th>
                      <th>{t('orchestration.load')}</th>
                      <th>{t('orchestration.items')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {waves.map(w => (
                      <tr key={w.index}>
                        <td className="mono">#{w.index}</td>
                        <td>
                          <span className="badge" style={{ background: MODE_COLOR[w.mode] || '#64748b', color: '#fff' }}>
                            {modeName(w.mode)}
                          </span>
                        </td>
                        <td className="mono">{w.size}</td>
                        <td className="small-text mono">
                          {Object.entries(w.platform_loads || {}).map(([p, c]) => `${p}:${c}`).join('  ')}
                        </td>
                        <td className="small-text mono">
                          {(w.items || []).map(it => `L${it.lottery_id}`).join(', ')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div className="empty-cell">{t('orchestration.noData')}</div>}
          </div>
        </>
      )}

      <div className="panel">
        <div className="panel-title">{t('orchestration.draftsTitle')}</div>
        {drafts.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('orchestration.draftId')}</th>
                  <th>{t('orchestration.key')}</th>
                  <th>{t('orchestration.waveCount')}</th>
                  <th>{t('orchestration.status')}</th>
                  <th>{t('orchestration.created')}</th>
                </tr>
              </thead>
              <tbody>
                {drafts.map(d => (
                  <tr key={d.id}>
                    <td className="mono">#{d.id}</td>
                    <td>{d.campaign_key}</td>
                    <td className="mono">{d.waves}</td>
                    <td><span className="badge badge-muted">{d.status}</span></td>
                    <td className="small-text mono">{String(d.created_at || '').replace('T', ' ').slice(0, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-cell">{t('orchestration.noData')}</div>}
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
