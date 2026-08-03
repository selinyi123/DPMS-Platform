import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import StatusBadge from '../components/StatusBadge';
import { formatText } from '../i18n/format';
import { knowledgeGapPresentation } from '../knowledgePresentation';
import { lotteryActionsForPlatform } from '../lotteryCompatibility';
import { useUi } from '../uiContext';

const FINAL_AUTHORIZATION_CHECKS = new Set([
  'real_run_global_switch',
  'global_circuit_breaker_closed',
  'autopilot_real_run_authorized',
]);

function consumerGroupAlertDetails(alerts) {
  return alerts.map((alert, index) => [
    alert.platform,
    alert.stream,
    ...(Array.isArray(alert.warning_codes) ? alert.warning_codes : []),
    ...(Array.isArray(alert.retention_blocked_groups)
      ? alert.retention_blocked_groups
      : []),
  ].filter(Boolean).join(':') || `alert-${index + 1}`).join(', ');
}

export default function Dashboard() {
  const { t, language } = useUi();
  const [metrics, setMetrics] = useState({});
  const [readiness, setReadiness] = useState(null);
  const [knowledge, setKnowledge] = useState(null);

  useEffect(() => {
    let disposed = false;
    const requests = [
      { path: '/metrics/overview', apply: setMetrics, inFlight: false, controller: null },
      { path: '/metrics/readiness', apply: setReadiness, inFlight: false, controller: null },
      { path: '/knowledge/summary', apply: setKnowledge, inFlight: false, controller: null },
    ];
    const loadOne = async (request) => {
      if (disposed || request.inFlight) return;
      request.inFlight = true;
      request.controller = new AbortController();
      try {
        const rows = await fetchJSON(request.path, {
          signal: request.controller.signal,
        });
        if (!disposed) request.apply(rows);
      } catch {
        // Keep only this endpoint's last known state. A knowledge or metrics
        // failure must not freeze the production gate and next actions.
      } finally {
        request.inFlight = false;
        request.controller = null;
      }
    };
    const load = () => requests.forEach(request => void loadOne(request));
    load();
    const timer = window.setInterval(load, 5000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      requests.forEach(request => request.controller?.abort());
    };
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
  const consumerGroupRetentionAlerts = Array.isArray(
    metrics.redis_consumer_group_retention_alerts,
  )
    ? metrics.redis_consumer_group_retention_alerts
    : [];
  const blockingConsumerGroupAlerts = consumerGroupRetentionAlerts.filter(
    alert => Boolean(alert?.retention_alert),
  );
  const staleConsumerMetadataAlerts = consumerGroupRetentionAlerts.filter(
    alert => !alert?.retention_alert && Boolean(alert?.consumer_inventory_alert),
  );
  const productionChecks = Array.isArray(readiness?.production_checks)
    ? readiness.production_checks
    : [];
  const technicalP0Checks = productionChecks.filter(check => (
    check?.priority === 'P0' && !FINAL_AUTHORIZATION_CHECKS.has(check?.code)
  ));
  const technicalP0Ready = technicalP0Checks.length > 0
    && technicalP0Checks.every(check => check?.passed === true);
  const failedProductionChecks = productionChecks.filter(check => check?.passed !== true);

  const kt = (key) => knowledgeCopy[language]?.[key] || knowledgeCopy.en[key] || key;
  const knowledgeSummary = knowledge?.summary || {};
  const platformProfiles = (knowledge?.platform_profiles || []).slice(0, 6);
  const accountProfiles = (knowledge?.account_profiles || []).slice(0, 5);
  const learningGaps = knowledge?.learning_gaps || [];

  const localizeAction = (action) => {
    const platform = platformLabel(readiness?.platforms, action.target);
    const byCode = {
      restore_worker_capacity: ['restoreWorkerTitle', 'restoreWorkerDetail', 'restoreWorkerExample'],
      restore_platform_task_transport: ['restoreTransportTitle', 'restoreTransportDetail', 'restoreTransportExample'],
      review_global_circuit_breaker: ['reviewBreakerTitle', 'reviewBreakerDetail', 'reviewBreakerExample'],
      restore_autopilot_heartbeat: ['restoreAutopilotTitle', 'restoreAutopilotDetail', 'restoreAutopilotExample'],
      configure_autopilot_dispatch: ['configureAutopilotTitle', 'configureAutopilotDetail', 'configureAutopilotExample'],
      authorize_autopilot_real_run: ['authorizeAutopilotTitle', 'authorizeAutopilotDetail', 'authorizeAutopilotExample'],
      approve_real_run_deployment: ['approveRealRunTitle', 'approveRealRunDetail', 'approveRealRunExample'],
      restore_target_readiness_observation: ['restoreTargetObservationTitle', 'restoreTargetObservationDetail', 'restoreTargetObservationExample'],
      add_autopilot_target: ['addAutopilotTargetTitle', 'addAutopilotTargetDetail', 'addAutopilotTargetExample'],
      complete_target_action_plan: ['completeTargetPlanTitle', 'completeTargetPlanDetail', 'completeTargetPlanExample'],
      complete_dispatch_platform_intersection: ['completePlatformIntersectionTitle', 'completePlatformIntersectionDetail', 'completePlatformIntersectionExample'],
      complete_exact_real_candidate: ['completeExactCandidateTitle', 'completeExactCandidateDetail', 'completeExactCandidateExample'],
      resolve_redis_consumer_group_retention: ['resolveRedisRetentionTitle', 'resolveRedisRetentionDetail', 'resolveRedisRetentionExample'],
      retire_stale_redis_consumer_metadata: ['retireRedisConsumersTitle', 'retireRedisConsumersDetail', 'retireRedisConsumersExample'],
      configure_notification: ['configureNotificationTitle', 'configureNotificationDetail', 'configureNotificationExample'],
      restore_notification_delivery_observation: ['restoreNotificationObservationTitle', 'restoreNotificationObservationDetail', 'restoreNotificationObservationExample'],
      verify_notification_delivery: ['verifyNotificationTitle', 'verifyNotificationDetail', 'verifyNotificationExample'],
      review_risk: ['reviewRiskTitle', 'reviewRiskDetail', 'reviewRiskExample'],
      add_proxy_exit: ['addProxyTitle', 'addProxyDetail', 'addProxyExample'],
      add_calibrated_account: ['addAccountTitle', 'addAccountDetail', 'addAccountExample'],
      configure_weibo_oauth: ['configureWeiboOAuthTitle', 'configureWeiboOAuthDetail', 'configureWeiboOAuthExample'],
      complete_adapter_probe: ['completeProbeTitle', 'completeProbeDetail', 'completeProbeExample'],
      enable_real_adapter: ['enableAdapterTitle', 'enableAdapterDetail', 'enableAdapterExample'],
      keep_dry_run: ['dryRunOnlyTitle', 'dryRunOnlyDetail', 'dryRunOnlyExample'],
    };
    const byTitle = {
      'Configure at least one notification channel': ['configureNotificationTitle', 'configureNotificationDetail', 'configureNotificationExample'],
      'Review recent account risk signals': ['reviewRiskTitle', 'reviewRiskDetail', 'reviewRiskExample'],
      'Add isolated proxy exits for production accounts': ['addProxyTitle', 'addProxyDetail', 'addProxyExample'],
      'Keep production dispatch in dry-run mode': ['dryRunOnlyTitle', 'dryRunOnlyDetail', 'dryRunOnlyExample'],
    };

    let keys = byCode[action.code] || byTitle[action.title];
    if (!keys && action.title?.startsWith('Add a calibrated safe account for ')) {
      keys = ['addAccountTitle', 'addAccountDetail', 'addAccountExample'];
    }
    if (!keys && action.title?.startsWith('Complete adapter probe for ')) {
      keys = ['completeProbeTitle', 'completeProbeDetail', 'completeProbeExample'];
    }
    if (!keys && action.title?.startsWith('Enable real action adapter for ')) {
      keys = ['enableAdapterTitle', 'enableAdapterDetail', 'enableAdapterExample'];
    }
    if (!keys) keys = ['unknownTitle', 'unknownDetail', 'unknownExample'];
    return {
      ...action,
      title: formatText(t(`dashboard.actionsMap.${keys[0]}`), { platform }),
      detail: formatText(t(`dashboard.actionsMap.${keys[1]}`), { platform }),
      example: formatText(t(`dashboard.actionsMap.${keys[2]}`), { platform }),
    };
  };

  const localizeActionTarget = (target) => {
    const mapped = t(`dashboard.actionTargets.${target}`);
    if (mapped !== `dashboard.actionTargets.${target}`) return mapped;
    return platformLabel(readiness?.platforms, target);
  };

  const localizeBlocker = (blocker, code) => {
    const key = code || blocker;
    const mapped = t(`dashboard.blockersMap.${key}`);
    if (mapped !== `dashboard.blockersMap.${key}`) return mapped;
    const fallback = t(`dashboard.blockersMap.${blocker}`);
    if (fallback !== `dashboard.blockersMap.${blocker}`) return fallback;
    const realGateBlocker = t(`lotteries.realGateBlockers.${key}`);
    if (realGateBlocker !== `lotteries.realGateBlockers.${key}`) return realGateBlocker;
    return t('dashboard.unknownBlocker');
  };

  const localizeCheck = (check) => {
    const title = t(`dashboard.checksMap.${check.code}Title`);
    const detail = t(`dashboard.checksMap.${check.code}Detail`);
    const example = t(`dashboard.checksMap.${check.code}Example`);
    return {
      title: title === `dashboard.checksMap.${check.code}Title` ? t('dashboard.checksMap.unknownTitle') : title,
      detail: detail === `dashboard.checksMap.${check.code}Detail` ? t('dashboard.checksMap.unknownDetail') : detail,
      example: example === `dashboard.checksMap.${check.code}Example` ? t('dashboard.checksMap.unknownExample') : example,
    };
  };

  const localizeStrategy = (item) => {
    const title = t(`dashboard.strategyMap.${item.code}Title`);
    const detail = t(`dashboard.strategyMap.${item.code}Detail`);
    return {
      title: title === `dashboard.strategyMap.${item.code}Title` ? item.title : title,
      detail: detail === `dashboard.strategyMap.${item.code}Detail` ? item.detail : detail,
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

      {blockingConsumerGroupAlerts.length > 0 && (
        <div className="alert-danger" role="alert">
          <strong>
            {t('dashboard.consumerGroupAlertTitle')} ({blockingConsumerGroupAlerts.length})
          </strong>
          <div className="small-text">
            {t('dashboard.consumerGroupAlertHint')}
          </div>
          <div className="mono small-text">
            {consumerGroupAlertDetails(blockingConsumerGroupAlerts)}
          </div>
        </div>
      )}

      {staleConsumerMetadataAlerts.length > 0 && (
        <div className="alert-warn" role="status">
          <strong>
            {t('dashboard.consumerGroupMetadataAlertTitle')} ({staleConsumerMetadataAlerts.length})
          </strong>
          <div className="small-text">
            {t('dashboard.consumerGroupMetadataAlertHint')}
          </div>
          <div className="mono small-text">
            {consumerGroupAlertDetails(staleConsumerMetadataAlerts)}
          </div>
        </div>
      )}

      <div className="metric-grid">
        {cards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      <div className="metric-grid readiness-metric-grid">
        {readinessCards.map(card => <MetricsCard key={card.label} {...card} />)}
      </div>

      {readiness?.summary?.production_ready === false && failedProductionChecks.length > 0 && (
        <div className="alert-warn" role="status">
          <strong>
            {formatText(t('dashboard.blockedSummaryTitle'), {
              count: failedProductionChecks.length,
            })}
          </strong>
          <div className="small-text">{t('dashboard.blockedSummaryHint')}</div>
          <div className="blocker-list compact-blockers">
            {failedProductionChecks.map(check => (
              <span className="badge badge-muted" key={check.code}>
                {check.priority} · {localizeCheck(check).title}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-title">{t('dashboard.productionGate')}</div>
        <p className="muted-text tight-text">{t('dashboard.productionGatePriorityHint')}</p>
        <div className="production-check-grid">
          {productionChecks.map(check => {
            const localized = localizeCheck(check);
            const authorizationLock = !check.passed && FINAL_AUTHORIZATION_CHECKS.has(check.code);
            return (
              <div className="production-check-row" key={check.code}>
                <div className="action-priority">
                  <span className={`badge ${check.passed ? 'badge-ready' : authorizationLock ? (technicalP0Ready ? 'badge-danger' : 'badge-muted') : check.priority === 'P0' ? 'badge-danger' : 'badge-warn'}`}>
                    {check.passed
                      ? t('dashboard.clear')
                      : authorizationLock
                        ? t('dashboard.authorizationLockPriority')
                      : check.priority === 'P1' && check.blocking === false
                        ? formatText(t('dashboard.nonBlockingPriority'), { priority: check.priority })
                        : check.priority}
                  </span>
                </div>
                <div className="action-copy">
                  <div className="action-title">{localized.title}</div>
                  <p className="muted-text">{localized.detail}</p>
                  {!check.passed && (
                    <p className="small-text muted-text">
                      <strong>{t('dashboard.exampleLabel')}</strong> {localized.example}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
          {!productionChecks.length && <div className="empty-cell">{t('dashboard.loading')}</div>}
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
                    {action.priority === 'P1'
                      ? formatText(t('dashboard.nonBlockingPriority'), { priority: action.priority })
                      : action.priority}
                  </span>
                  <span className="small-text muted-text">{localizeActionTarget(action.target)}</span>
                </div>
                <div className="action-copy">
                  <div className="action-title">{localized.title}</div>
                  <p className="muted-text">{localized.detail}</p>
                  <p className="small-text muted-text">
                    <strong>{t('dashboard.exampleLabel')}</strong> {localized.example}
                  </p>
                </div>
              </div>
            );
          })}
          {!readiness?.actions?.length && <div className="empty-cell">{t('dashboard.noActions')}</div>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('dashboard.strategyAdvice')}</div>
        <p className="muted-text tight-text">
          {formatText(t('dashboard.strategyWindow'), { days: readiness?.strategy_advice?.review_window_days ?? 7 })}
        </p>
        <div className="action-plan-list">
          {readiness?.strategy_advice?.advice?.map(item => {
            const localized = localizeStrategy(item);
            return (
              <div className="action-plan-row" key={item.code}>
                <div className="action-priority">
                  <span className={`badge ${item.priority === 'P0' ? 'badge-danger' : item.priority === 'P1' ? 'badge-warn' : 'badge-info'}`}>
                    {item.priority}
                  </span>
                  <span className="mono small-text muted-text">{item.target}</span>
                </div>
                <div className="action-copy">
                  <div className="action-title">{localized.title}</div>
                  <p className="muted-text">{localized.detail}</p>
                  <div className="mono small-text muted-text">{JSON.stringify(item.evidence || {})}</div>
                </div>
              </div>
            );
          })}
          {!readiness?.strategy_advice?.advice?.length && <div className="empty-cell">{t('dashboard.noStrategyAdvice')}</div>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{kt('title')}</div>
        <div className="knowledge-strip">
          <div className="knowledge-stat">
            <span>{kt('maturity')}</span>
            <strong>{knowledgeSummary.data_maturity_score ?? '-'}</strong>
            <em>{maturityText(knowledgeSummary.data_maturity_level, kt)}</em>
          </div>
          <div className="knowledge-stat">
            <span>{kt('events')}</span>
            <strong>{knowledgeSummary.total_events ?? '-'}</strong>
            <em>{kt('recentMemory')}</em>
          </div>
          <div className="knowledge-stat">
            <span>{kt('resultLabels')}</span>
            <strong>{knowledgeSummary.result_labels ?? '-'}</strong>
            <em>{kt('wonLost')}</em>
          </div>
          <div className="knowledge-stat">
            <span>{kt('shadowEvidence')}</span>
            <strong>{knowledgeSummary.shadow_success ?? '-'}</strong>
            <em>{kt('safeEvidence')}</em>
          </div>
        </div>

        <div className="ops-grid two-columns knowledge-grid">
          <div className="knowledge-section">
            <div className="compact-title">{kt('platformKnowledge')}</div>
            <div className="table-wrap">
              <table className="data-table compact-data-table">
                <thead>
                  <tr>
                    <th>{kt('platform')}</th>
                    <th>{kt('sample')}</th>
                    <th>{kt('winRate')}</th>
                    <th>{kt('risk')}</th>
                    <th>{kt('confidence')}</th>
                  </tr>
                </thead>
                <tbody>
                  {platformProfiles.map(item => (
                    <tr key={item.platform}>
                      <td>
                        <div className="mono">{item.platform}</div>
                        <div className="small-text muted-text">{item.label}</div>
                      </td>
                      <td>{item.total_lotteries}</td>
                      <td>{formatRate(item.win_rate)}</td>
                      <td>{item.risk_events}</td>
                      <td>
                        <div className="knowledge-score">
                          <span>{item.knowledge_confidence}</span>
                          <div><i style={{ width: `${item.knowledge_confidence || 0}%` }} /></div>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!platformProfiles.length && <tr><td className="empty-cell" colSpan="5">{kt('noKnowledge')}</td></tr>}
                </tbody>
              </table>
            </div>
          </div>

          <div className="knowledge-section">
            <div className="compact-title">{kt('accountKnowledge')}</div>
            <div className="table-wrap">
              <table className="data-table compact-data-table">
                <thead>
                  <tr>
                    <th>{kt('account')}</th>
                    <th>{kt('status')}</th>
                    <th>{kt('runs')}</th>
                    <th>{kt('risk')}</th>
                    <th>{kt('reputation')}</th>
                  </tr>
                </thead>
                <tbody>
                  {accountProfiles.map(item => (
                    <tr key={item.account_id}>
                      <td>
                        <div className="mono">A{item.account_id}</div>
                        <div className="small-text muted-text">{item.platform}</div>
                      </td>
                      <td><StatusBadge status={item.status} /></td>
                      <td>{item.total_runs}</td>
                      <td>{item.risk_events}</td>
                      <td>
                        <div className="knowledge-score">
                          <span>{item.reputation_score}</span>
                          <div><i style={{ width: `${item.reputation_score || 0}%` }} /></div>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!accountProfiles.length && <tr><td className="empty-cell" colSpan="5">{kt('noAccounts')}</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="knowledge-gap-list">
          <div className="compact-title">{kt('learningGaps')}</div>
          <div className="action-plan-list">
            {learningGaps.map(item => {
              const localized = knowledgeGapPresentation(item, t);
              return (
                <div className="action-plan-row" key={item.code}>
                  <div className="action-priority">
                    <span className={`badge ${item.priority === 'P0' ? 'badge-danger' : item.priority === 'P1' ? 'badge-warn' : 'badge-info'}`}>
                      {item.priority}
                    </span>
                    <span className="small-text muted-text">{localized.label}</span>
                  </div>
                  <div className="action-copy">
                    <div className="action-title">{localized.title}</div>
                    <p className="muted-text">{localized.detail}</p>
                  </div>
                </div>
              );
            })}
            {!learningGaps.length && <div className="empty-cell">{kt('noGaps')}</div>}
          </div>
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
                  <td>{platform.dry_run_supported === false
                    ? <span className="badge badge-info">{t('dashboard.shadowOnly')}</span>
                    : <StatusBadge status={platform.ready_for_dry_run ? 'ready' : 'pending'} />}</td>
                  <td><StatusBadge status={platform.ready_for_real_run ? 'ready' : 'pending'} /></td>
                  <td>{platform.safe_accounts}</td>
                  <td>
                    {platform.latest_probe ? (
                      <span className="mono small-text">
                        {platform.latest_probe.ready_phase_count ?? '-'}/{lotteryActionsForPlatform(platform.platform).length} / {statusText(platform.latest_probe.status)}
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

function platformLabel(platforms = [], id) {
  return platforms.find(item => item.platform === id)?.label || id;
}

function formatRate(value) {
  if (value === null || value === undefined) return '-';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function maturityText(level, kt) {
  return kt(`maturity_${level || 'unknown'}`);
}

const knowledgeCopy = {
  zh: {
    title: '知识运行时',
    maturity: '数据成熟度',
    events: '事件',
    recentMemory: '近期记忆',
    resultLabels: '结果标签',
    wonLost: '中奖/未中',
    shadowEvidence: 'Shadow 证据',
    safeEvidence: '安全证据',
    platformKnowledge: '平台经验',
    accountKnowledge: '账号信誉',
    platform: '平台',
    sample: '样本',
    winRate: '中奖率',
    risk: '风险',
    confidence: '置信度',
    account: '账号',
    status: '状态',
    runs: '运行',
    reputation: '信誉',
    learningGaps: '学习缺口',
    noKnowledge: '暂无平台经验',
    noAccounts: '暂无账号画像',
    noGaps: '暂无学习缺口',
    maturity_cold_start: '冷启动',
    maturity_warming: '积累中',
    maturity_usable: '可用于策略',
    maturity_learning_ready: '可进入学习',
    maturity_unknown: '未知',
    event_memory_emptyTitle: '事件记忆为空',
    event_memory_emptyDetail: '运行发现、账号校准、dry-run 或目标导入后，知识层才能形成历史。',
    result_labels_lowTitle: '结果标签不足',
    result_labels_lowDetail: '已知开奖结果后标记 won/lost，用于后续估算中奖率。',
    shadow_evidence_lowTitle: 'Shadow-run 证据不足',
    shadow_evidence_lowDetail: '在真实执行前先积累多次成功 shadow-run。',
    account_profiles_emptyTitle: '账号画像为空',
    account_profiles_emptyDetail: '通过扫码登录或 Cookie 导入账号，然后完成校准。',
    platform_samples_lowTitle: '部分平台样本偏少',
    platform_samples_lowDetail: '导入或发现更多目标后，再比较平台中奖率和风险率。',
    risk_observations_emptyTitle: '近期风险观察为空',
    risk_observations_emptyDetail: '这可能表示系统较安静或安全；继续记录验证码、限流、审核、封禁等风险信号。',
    lottery_value_profile_emptyTitle: '价值分层画像为空',
    lottery_value_profile_emptyDetail: '导入目标时设置 value_score，策略层才能比较不同价值段。',
  },
  en: {
    title: 'Knowledge Runtime',
    maturity: 'Data maturity',
    events: 'Events',
    recentMemory: 'recent memory',
    resultLabels: 'Result labels',
    wonLost: 'won/lost',
    shadowEvidence: 'Shadow evidence',
    safeEvidence: 'safe evidence',
    platformKnowledge: 'Platform intelligence',
    accountKnowledge: 'Account reputation',
    platform: 'Platform',
    sample: 'Sample',
    winRate: 'Win rate',
    risk: 'Risk',
    confidence: 'Confidence',
    account: 'Account',
    status: 'Status',
    runs: 'Runs',
    reputation: 'Reputation',
    learningGaps: 'Learning gaps',
    noKnowledge: 'No platform knowledge',
    noAccounts: 'No account profiles',
    noGaps: 'No learning gaps',
    maturity_cold_start: 'Cold start',
    maturity_warming: 'Warming',
    maturity_usable: 'Strategy usable',
    maturity_learning_ready: 'Learning ready',
    maturity_unknown: 'Unknown',
    event_memory_emptyTitle: 'Event memory is empty',
    event_memory_emptyDetail: 'Run discovery, account calibration, dry-run, or target import so Knowledge Runtime can build history.',
    result_labels_lowTitle: 'Lottery result labels are sparse',
    result_labels_lowDetail: 'Mark won/lost results after known outcomes to estimate future win rates.',
    shadow_evidence_lowTitle: 'Shadow-run evidence is thin',
    shadow_evidence_lowDetail: 'Collect several successful shadow-runs before relying on real-run recommendations.',
    account_profiles_emptyTitle: 'No account profiles',
    account_profiles_emptyDetail: 'Add accounts through QR login or Cookie import, then calibrate them.',
    platform_samples_lowTitle: 'Some platforms have low sample counts',
    platform_samples_lowDetail: 'Import or discover more targets before comparing platform win or risk rates.',
    risk_observations_emptyTitle: 'No recent risk observations',
    risk_observations_emptyDetail: 'This can mean the system is quiet or safe; keep recording captcha, rate-limit, review, and ban signals.',
    lottery_value_profile_emptyTitle: 'No value-band profile',
    lottery_value_profile_emptyDetail: 'Import targets with value_score so the strategy layer can compare value bands.',
  },
};
