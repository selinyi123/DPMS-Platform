import { useEffect, useState } from 'react';

import { fetchJSON } from '../api';
import MetricsCard from '../components/MetricsCard';
import StatusBadge from '../components/StatusBadge';
import { useUi } from '../uiContext';

export default function Dashboard() {
  const { t, language } = useUi();
  const [metrics, setMetrics] = useState({});
  const [readiness, setReadiness] = useState(null);
  const [knowledge, setKnowledge] = useState(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [metricRows, readinessRows, knowledgeRows] = await Promise.all([
          fetchJSON('/metrics/overview'),
          fetchJSON('/metrics/readiness'),
          fetchJSON('/knowledge/summary'),
        ]);
        setMetrics(metricRows);
        setReadiness(readinessRows);
        setKnowledge(knowledgeRows);
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

  const kt = (key) => knowledgeCopy[language]?.[key] || knowledgeCopy.en[key] || key;
  const knowledgeSummary = knowledge?.summary || {};
  const platformProfiles = (knowledge?.platform_profiles || []).slice(0, 6);
  const accountProfiles = (knowledge?.account_profiles || []).slice(0, 5);
  const learningGaps = knowledge?.learning_gaps || [];

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
            {learningGaps.map(item => (
              <div className="action-plan-row" key={item.code}>
                <div className="action-priority">
                  <span className={`badge ${item.priority === 'P0' ? 'badge-danger' : item.priority === 'P1' ? 'badge-warn' : 'badge-info'}`}>
                    {item.priority}
                  </span>
                  <span className="mono small-text muted-text">{item.code}</span>
                </div>
                <div className="action-copy">
                  <div className="action-title">{localKnowledgeGap(item, kt).title}</div>
                  <p className="muted-text">{localKnowledgeGap(item, kt).detail}</p>
                </div>
              </div>
            ))}
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

function formatRate(value) {
  if (value === null || value === undefined) return '-';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function maturityText(level, kt) {
  return kt(`maturity_${level || 'unknown'}`);
}

function localKnowledgeGap(item, kt) {
  const titleKey = `${item.code}Title`;
  const detailKey = `${item.code}Detail`;
  const title = kt(titleKey);
  const detail = kt(detailKey);
  return {
    title: title === titleKey ? item.title : title,
    detail: detail === detailKey ? item.detail : detail,
  };
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
