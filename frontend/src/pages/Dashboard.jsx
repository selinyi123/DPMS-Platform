import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

export default function Dashboard() {
  const { t } = useUi();
  const [metrics, setMetrics] = useState({});
  const [readiness, setReadiness] = useState(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [metricRows, readinessRows] = await Promise.all([
          fetchJSON('/metrics/overview'),
          fetchJSON('/metrics/readiness'),
        ]);
        setMetrics(metricRows);
        setReadiness(readinessRows);
      } catch {
        // Dashboard keeps its last known state if one polling cycle fails.
      }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const cards = [
    { label: t('dashboard.workersOnline'), value: metrics.workers_online, unit: t('dashboard.units.nodes'), color: '#2563eb' },
    { label: t('dashboard.pendingTasks'), value: metrics.pending, unit: t('dashboard.units.items'), color: '#d97706' },
    { label: t('dashboard.readyAccounts'), value: metrics.accounts_ready, unit: t('dashboard.units.accounts'), color: '#059669' },
    { label: t('dashboard.coolingAccounts'), value: metrics.accounts_cooling, unit: t('dashboard.units.accounts'), color: '#dc2626' },
    { label: t('dashboard.todayExecutions'), value: metrics.today_tasks, unit: t('dashboard.units.runs'), color: '#7c3aed' },
    { label: t('dashboard.memory'), value: metrics.memory_mb, unit: 'MB', color: '#475569' },
  ];

  const readinessCards = [
    { label: t('dashboard.productionReady'), value: readiness?.summary?.production_ready ? t('dashboard.clear') : t('dashboard.blocked'), unit: t('dashboard.productionGate'), color: readiness?.summary?.production_ready ? '#059669' : '#dc2626' },
    { label: t('dashboard.dryRunPlatforms'), value: readiness?.summary?.dry_run_ready, unit: `${t('dashboard.units.of')} ${readiness?.summary?.platforms_total ?? '-'}`, color: '#059669' },
    { label: t('dashboard.realRunPlatforms'), value: readiness?.summary?.real_run_ready, unit: `${t('dashboard.units.of')} ${readiness?.summary?.platforms_total ?? '-'}`, color: '#d97706' },
    { label: t('dashboard.safeAccounts'), value: readiness?.summary?.safe_accounts_total, unit: t('dashboard.units.calibrated'), color: '#2563eb' },
    { label: t('dashboard.notifyChannels'), value: readiness?.summary?.notification_channels_configured, unit: t('dashboard.units.configured'), color: '#7c3aed' },
    { label: t('dashboard.proxyExits'), value: readiness?.summary?.proxy_exits_total, unit: t('dashboard.units.configured'), color: '#0891b2' },
    { label: t('dashboard.risk24h'), value: readiness?.summary?.recent_risk_events_24h, unit: t('dashboard.units.events'), color: '#dc2626' },
  ];

  const localizeAction = (action) => {
    const platform = platformLabel(readiness?.platforms, action.target);
    const byCode = {
      configure_notification: ['configureNotificationTitle', 'configureNotificationDetail'],
      review_risk: ['reviewRiskTitle', 'reviewRiskDetail'],
      add_proxy_exit: ['addProxyTitle', 'addProxyDetail'],
      add_calibrated_account: ['addAccountTitle', 'addAccountDetail'],
      complete_adapter_probe: ['completeProbeTitle', 'completeProbeDetail'],
      enable_real_adapter: ['enableAdapterTitle', 'enableAdapterDetail'],
      keep_dry_run: ['dryRunOnlyTitle', 'dryRunOnlyDetail'],
    };
    const byTitle = {
      'Configure at least one notification channel': ['configureNotificationTitle', 'configureNotificationDetail'],
      'Review recent account risk signals': ['reviewRiskTitle', 'reviewRiskDetail'],
      'Add isolated proxy exits for production accounts': ['addProxyTitle', 'addProxyDetail'],
      'Keep production dispatch in dry-run mode': ['dryRunOnlyTitle', 'dryRunOnlyDetail'],
    };

    let keys = byCode[action.code] || byTitle[action.title];
    if (!keys && action.title?.startsWith('Add a calibrated safe account for ')) {
      keys = ['addAccountTitle', 'addAccountDetail'];
    }
    if (!keys && action.title?.startsWith('Complete adapter probe for ')) {
      keys = ['completeProbeTitle', 'completeProbeDetail'];
    }
    if (!keys && action.title?.startsWith('Enable real action adapter for ')) {
      keys = ['enableAdapterTitle', 'enableAdapterDetail'];
    }
    if (!keys) return action;
    return {
      ...action,
      title: formatText(t(`dashboard.actionsMap.${keys[0]}`), { platform }),
      detail: formatText(t(`dashboard.actionsMap.${keys[1]}`), { platform }),
    };
  };

  const localizeBlocker = (blocker, code) => {
    const key = code || blocker;
    const mapped = t(`dashboard.blockersMap.${key}`);
    if (mapped !== `dashboard.blockersMap.${key}`) return mapped;
    const fallback = t(`dashboard.blockersMap.${blocker}`);
    return fallback === `dashboard.blockersMap.${blocker}` ? blocker : fallback;
  };

  const localizeCheck = (check) => {
    const title = t(`dashboard.checksMap.${check.code}Title`);
    const detail = t(`dashboard.checksMap.${check.code}Detail`);
    return {
      title: title === `dashboard.checksMap.${check.code}Title` ? check.title : title,
      detail: detail === `dashboard.checksMap.${check.code}Detail` ? check.detail : detail,
    };
  };

  const statusText = (status) => {
    const label = t(`status.${status}`);
    return label === `status.${status}` ? status : label;
  };

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('dashboard.eyebrow')}</p>
          <h1>{t('dashboard.title')}</h1>
        </div>
      </header>

      <div className="metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="metric-grid readiness-metric-grid">
        {readinessCards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="panel">
        <div className="panel-title">{t('dashboard.productionGate')}</div>
        <div className="production-check-grid">
          {readiness?.production_checks?.map(check => {
            const localized = localizeCheck(check);
            return (
              <div className="production-check-row" key={check.code}>
                <div className="action-priority">
                  <span className={`badge ${check.passed ? 'badge-ready' : check.priority === 'P0' ? 'badge-danger' : 'badge-warn'}`}>
                    {check.passed ? t('dashboard.clear') : check.priority}
                  </span>
                  <span className="mono small-text muted-text">{check.code}</span>
                </div>
                <div className="action-copy">
                  <div className="action-title">{localized.title}</div>
                  <p className="muted-text">{localized.detail}</p>
                </div>
              </div>
            );
          })}
          {!readiness?.production_checks?.length && <div className="empty-cell">{t('dashboard.loading')}</div>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('dashboard.nextActions')}</div>
        <div className="action-plan-list">
          {readiness?.actions?.map(action => {
            const localized = localizeAction(action);
            return (
              <div className="action-plan-row" key={`${action.target}-${action.title}`}>
                <div className="action-priority">
                  <span className={`badge ${action.priority === 'P0' ? 'badge-danger' : action.priority === 'P1' ? 'badge-warn' : 'badge-info'}`}>
                    {action.priority}
                  </span>
                  <span className="mono small-text muted-text">{action.target}</span>
                </div>
                <div className="action-copy">
                  <div className="action-title">{localized.title}</div>
                  <p className="muted-text">{localized.detail}</p>
                </div>
              </div>
            );
          })}
          {!readiness?.actions?.length && <div className="empty-cell">{t('dashboard.noActions')}</div>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('dashboard.platformReadiness')}</div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>{t('dashboard.platform')}</th><th>{t('dashboard.dryRun')}</th><th>{t('dashboard.realRun')}</th><th>{t('dashboard.safeAccounts')}</th><th>{t('dashboard.probe')}</th><th>{t('dashboard.blockers')}</th></tr>
            </thead>
            <tbody>
              {readiness?.platforms?.map(platform => (
                <tr key={platform.platform}>
                  <td>
                    <div className="mono">{platform.platform}</div>
                    <div className="small-text muted-text">{platform.label}</div>
                  </td>
                  <td><StatusBadge status={platform.ready_for_dry_run ? 'ready' : 'pending'} /></td>
                  <td><StatusBadge status={platform.ready_for_real_run ? 'ready' : 'pending'} /></td>
                  <td>{platform.safe_accounts}</td>
                  <td>
                    {platform.latest_probe ? (
                      <span className="mono small-text">
                        {platform.latest_probe.ready_phase_count ?? '-'}/4 / {statusText(platform.latest_probe.status)}
                      </span>
                    ) : (
                      <span className="badge badge-muted">{t('dashboard.noProbe')}</span>
                    )}
                  </td>
                  <td>
                    {platform.blockers.length ? (
                      <div className="blocker-list">
                        {platform.blockers.map((item, index) => <span className="badge badge-warn" key={item}>{localizeBlocker(item, platform.blocker_codes?.[index])}</span>)}
                      </div>
                    ) : (
                      <span className="badge badge-ready">{t('dashboard.clear')}</span>
                    )}
                  </td>
                </tr>
              ))}
              {!readiness?.platforms?.length && <tr><td className="empty-cell" colSpan="6">{t('dashboard.loading')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="ops-grid three-columns">
        <div className="panel strategy-panel">
          <div className="panel-kicker">{t('dashboard.kickerLogin')}</div>
          <div className="panel-title">{t('dashboard.loginTitle')}</div>
          <p className="muted-text">{t('dashboard.loginText')}</p>
        </div>
        <div className="panel strategy-panel">
          <div className="panel-kicker">{t('dashboard.kickerSafety')}</div>
          <div className="panel-title">{t('dashboard.safetyTitle')}</div>
          <p className="muted-text">{t('dashboard.safetyText')}</p>
        </div>
        <div className="panel strategy-panel">
          <div className="panel-kicker">{t('dashboard.kickerNotify')}</div>
          <div className="panel-title">{t('dashboard.notifyTitle')}</div>
          <p className="muted-text">{t('dashboard.notifyText')}</p>
        </div>
      </div>
    </section>
  );
}

function formatText(template, values) {
  return Object.entries(values).reduce((text, [key, value]) => text.replaceAll(`{${key}}`, value), template);
}

function platformLabel(platforms = [], id) {
  return platforms.find(item => item.platform === id)?.label || id;
}
