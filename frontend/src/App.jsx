import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useMemo,
  useState,
} from 'react';

import {
  fetchJSON,
  isAuthenticationApiError,
} from './api';
import { reloadApplicationModuleGraph } from './asyncSlices';
import NavButton from './components/NavButton';
import PlatformModuleBoundary from './components/PlatformModuleBoundary';
import ToastStack from './components/ToastStack';
import { useUi } from './uiContext';

const pages = {
  dashboard: { load: () => import('./pages/Dashboard.jsx') },
  accounts: { load: () => import('./pages/Accounts.jsx') },
  lotteries: { load: () => import('./pages/Lotteries.jsx'), platformAware: true },
  xhsTargets: { load: () => import('./pages/XiaohongshuTargets.jsx') },
  strategy: { load: () => import('./pages/Strategy.jsx') },
  knowledge: { load: () => import('./pages/Knowledge.jsx') },
  experiments: { load: () => import('./pages/Experiments.jsx') },
  riskIntel: { load: () => import('./pages/RiskIntelligence.jsx') },
  learning: { load: () => import('./pages/Learning.jsx') },
  governance: { load: () => import('./pages/Governance.jsx') },
  transitions: { load: () => import('./pages/TransitionGraph.jsx') },
  semantic: { load: () => import('./pages/SemanticTrace.jsx') },
  scheduling: { load: () => import('./pages/Scheduling.jsx') },
  capacity: { load: () => import('./pages/Capacity.jsx') },
  orchestration: { load: () => import('./pages/Orchestration.jsx') },
  throughput: { load: () => import('./pages/Throughput.jsx') },
  tasks: { load: () => import('./pages/TaskMonitor.jsx') },
  events: { load: () => import('./pages/EventTimeline.jsx') },
  risk: { load: () => import('./pages/RiskCenter.jsx') },
  deploy: { load: () => import('./pages/Deploy.jsx') },
};

class PageErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error) {
    console.error('page_module_load_failed', error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="auth-gate">
          <div className="auth-gate-card" role="alert">
            <h2>{this.props.title}</h2>
            <p>{this.props.message}</p>
            <button type="button" onClick={this.props.onRetry}>
              {this.props.retryLabel}
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const [tokenDraft, setTokenDraft] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(
    () => !window.matchMedia('(max-width: 920px)').matches,
  );
  const [authState, setAuthState] = useState({ status: 'unknown', role: '', error: '' });
  const { language, setLanguage, theme, setTheme, t, page, navigate } = useUi();
  const pageDefinition = pages[page] || pages.dashboard;
  const PageComponent = useMemo(
    () => lazy(pageDefinition.load),
    [pageDefinition],
  );
  const pageLoadingText = language === 'en' ? 'Loading page…' : '正在加载页面…';
  const pageLoadFailureTitle = language === 'en' ? 'Page unavailable' : '页面暂不可用';
  const pageLoadFailureMessage = language === 'en'
    ? 'The page module could not be loaded.'
    : '页面模块加载失败。';
  const pageLoadRetryText = language === 'en' ? 'Reload' : '重新加载';
  const authText = language === 'en'
    ? {
      label: 'Admin token',
      placeholder: 'Paste ADMIN_TOKEN',
      save: 'Sign in',
      clear: 'Sign out',
      verified: 'Verified',
      missing: 'Not signed in',
      failed: 'Invalid token',
      unavailable: 'Service temporarily unavailable. Retry when Core is healthy.',
      unavailableTitle: 'Service temporarily unavailable',
      unavailableBody: 'The saved token was kept. Use Sign in to retry the connection.',
      settings: 'Access & display settings',
      gateTitle: 'Sign in required',
      gateBody: 'Enter a valid ADMIN_TOKEN to access the console — every data endpoint requires authentication.',
      checking: 'Checking session…',
    }
    : {
      label: '管理令牌',
      placeholder: '粘贴 ADMIN_TOKEN',
      save: '登录',
      clear: '退出',
      verified: '已验证',
      missing: '未登录',
      failed: '令牌无效',
      unavailable: '服务暂不可用，请在 Core 恢复健康后重试。',
      unavailableTitle: '服务暂不可用',
      unavailableBody: '已保留本机令牌；请点击登录重试连接。',
      settings: '访问与显示设置',
      gateTitle: '需要登录',
      gateBody: '请输入有效的 ADMIN_TOKEN 以访问控制台——所有数据接口均需鉴权。',
      checking: '正在校验会话…',
    };

  const verifyToken = async () => {
    if (!localStorage.getItem('dpms_admin_token')) {
      setAuthState({ status: 'missing', role: '', error: '' });
      return;
    }
    setAuthState({ status: 'unknown', role: '', error: '' });
    try {
      const actor = await fetchJSON('/auth/me', { auth: true });
      setAuthState({ status: 'verified', role: actor.role, error: '' });
    } catch (err) {
      const tokenRejected = isAuthenticationApiError(err);
      setAuthState({
        status: tokenRejected ? 'failed' : 'unavailable',
        role: '',
        error: tokenRejected ? authText.failed : authText.unavailable,
      });
    }
  };

  useEffect(() => {
    verifyToken();
  }, []);

  useEffect(() => {
    const media = window.matchMedia('(max-width: 920px)');
    const handleChange = event => setSettingsOpen(!event.matches);
    media.addEventListener('change', handleChange);
    return () => media.removeEventListener('change', handleChange);
  }, []);

  const saveToken = async () => {
    const value = tokenDraft.trim();
    if (value) {
      localStorage.setItem('dpms_admin_token', value);
    } else if (!localStorage.getItem('dpms_admin_token')) {
      setAuthState({ status: 'missing', role: '', error: '' });
      return;
    }
    await verifyToken();
    setTokenDraft('');
  };

  const clearToken = async () => {
    localStorage.removeItem('dpms_admin_token');
    setTokenDraft('');
    await verifyToken();
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">D</div>
          <div>
            <div className="brand-title">DPMS</div>
            <div className="brand-subtitle">Multi-platform Lottery Ops</div>
          </div>
        </div>

        <nav className="nav-list">
          {Object.entries(pages).map(([key]) => (
            <NavButton key={key} active={page === key} onClick={() => navigate(key)}>
              {t(`nav.${key}`)}
            </NavButton>
          ))}
        </nav>

        <details
          className="settings-panel"
          open={settingsOpen}
          onToggle={event => setSettingsOpen(event.currentTarget.open)}
        >
          <summary className="settings-summary">
            <span>{authText.settings}</span>
            <span className={`settings-auth-dot auth-${authState.status}`} />
          </summary>
          <label>
            <span>{authText.label}</span>
            <input
              className="sidebar-input"
              type="password"
              value={tokenDraft}
              onChange={e => setTokenDraft(e.target.value)}
              placeholder={authText.placeholder}
              autoComplete="off"
            />
          </label>
          <div className="auth-actions">
            <button type="button" onClick={saveToken}>{authText.save}</button>
            <button type="button" onClick={clearToken}>{authText.clear}</button>
          </div>
          <div className={`auth-status auth-${authState.status}`}>
            {authState.status === 'verified'
              ? authText.verified
              : authState.status === 'failed'
                ? (authState.error || authText.failed)
                : authState.status === 'unavailable'
                  ? (authState.error || authText.unavailable)
                  : authState.status === 'unknown'
                    ? authText.checking
                    : authText.missing}
            {authState.role ? ` / ${authState.role}` : ''}
          </div>
          <label>
            <span>{t('common.language')}</span>
            <select value={language} onChange={e => setLanguage(e.target.value)}>
              <option value="zh">中文</option>
              <option value="en">English</option>
            </select>
          </label>
          <label>
            <span>{t('common.theme')}</span>
            <select value={theme} onChange={e => setTheme(e.target.value)}>
              <option value="system">{t('common.system')}</option>
              <option value="light">{t('common.light')}</option>
              <option value="dark">{t('common.dark')}</option>
            </select>
          </label>
        </details>

        <div className="sidebar-footer">
          <span className="status-dot" />
          {t('common.local')}
        </div>
      </aside>

      <main className="main-pane">
        {authState.status === 'verified' ? (
          <PageErrorBoundary
            key={page}
            title={pageLoadFailureTitle}
            message={pageLoadFailureMessage}
            onRetry={() => reloadApplicationModuleGraph()}
            retryLabel={pageLoadRetryText}
          >
            <Suspense fallback={(
              <div className="auth-gate">
                <div className="auth-gate-card">{pageLoadingText}</div>
              </div>
            )}
            >
              {pageDefinition.platformAware ? (
                <PlatformModuleBoundary Component={PageComponent} language={language} />
              ) : (
                <PageComponent />
              )}
            </Suspense>
          </PageErrorBoundary>
        ) : authState.status === 'unknown' ? (
          <div className="auth-gate">
            <div className="auth-gate-card">{authText.checking}</div>
          </div>
        ) : (
          <div className="auth-gate">
            <div className="auth-gate-card">
              <h2>{authState.status === 'unavailable'
                ? authText.unavailableTitle
                : authText.gateTitle}</h2>
              <p>{authState.status === 'unavailable'
                ? authText.unavailableBody
                : authText.gateBody}</p>
              <input
                className="auth-gate-input"
                type="password"
                value={tokenDraft}
                onChange={e => setTokenDraft(e.target.value)}
                placeholder={authText.placeholder}
                autoComplete="off"
                onKeyDown={e => { if (e.key === 'Enter') saveToken(); }}
              />
              <button type="button" onClick={saveToken}>{authText.save}</button>
              {['failed', 'unavailable'].includes(authState.status) && (
                <div className="auth-gate-error">
                  {authState.error || (authState.status === 'failed'
                    ? authText.failed
                    : authText.unavailable)}
                </div>
              )}
            </div>
          </div>
        )}
      </main>
      <ToastStack />
    </div>
  );
}
