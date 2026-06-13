import { useEffect, useState } from 'react';

import { fetchJSON } from './api';
import NavButton from './components/NavButton';
import ToastStack from './components/ToastStack';
import Accounts from './pages/Accounts';
import Dashboard from './pages/Dashboard';
import Deploy from './pages/Deploy';
import EventTimeline from './pages/EventTimeline';
import Experiments from './pages/Experiments';
import Governance from './pages/Governance';
import Knowledge from './pages/Knowledge';
import Learning from './pages/Learning';
import Lotteries from './pages/Lotteries';
import RiskCenter from './pages/RiskCenter';
import RiskIntelligence from './pages/RiskIntelligence';
import SemanticTrace from './pages/SemanticTrace';
import Strategy from './pages/Strategy';
import TaskMonitor from './pages/TaskMonitor';
import TransitionGraph from './pages/TransitionGraph';
import { useUi } from './uiContext';

const pages = {
  dashboard: { Component: Dashboard },
  accounts: { Component: Accounts },
  lotteries: { Component: Lotteries },
  strategy: { Component: Strategy },
  knowledge: { Component: Knowledge },
  experiments: { Component: Experiments },
  riskIntel: { Component: RiskIntelligence },
  learning: { Component: Learning },
  governance: { Component: Governance },
  transitions: { Component: TransitionGraph },
  semantic: { Component: SemanticTrace },
  tasks: { Component: TaskMonitor },
  events: { Component: EventTimeline },
  risk: { Component: RiskCenter },
  deploy: { Component: Deploy },
};

export default function App() {
  const [page, setPage] = useState('dashboard');
  const [tokenDraft, setTokenDraft] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(
    () => !window.matchMedia('(max-width: 920px)').matches,
  );
  const [authState, setAuthState] = useState({ status: 'unknown', role: '', error: '' });
  const PageComponent = pages[page].Component;
  const { language, setLanguage, theme, setTheme, t } = useUi();
  const authText = language === 'en'
    ? {
      label: 'Admin token',
      placeholder: 'Paste ADMIN_TOKEN',
      save: 'Sign in',
      clear: 'Sign out',
      verified: 'Verified',
      missing: 'Not signed in',
      failed: 'Invalid token',
      settings: 'Access & display settings',
    }
    : {
      label: '管理令牌',
      placeholder: '粘贴 ADMIN_TOKEN',
      save: '登录',
      clear: '退出',
      verified: '已验证',
      missing: '未登录',
      failed: '令牌无效',
      settings: '访问与显示设置',
    };

  const verifyToken = async () => {
    if (!localStorage.getItem('dpms_admin_token')) {
      setAuthState({ status: 'missing', role: '', error: '' });
      return;
    }
    try {
      const actor = await fetchJSON('/auth/me', { auth: true });
      setAuthState({ status: 'verified', role: actor.role, error: '' });
    } catch (err) {
      setAuthState({ status: 'failed', role: '', error: err.message || '' });
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
    if (!value) {
      localStorage.removeItem('dpms_admin_token');
    } else {
      localStorage.setItem('dpms_admin_token', value);
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
            <NavButton key={key} active={page === key} onClick={() => setPage(key)}>
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
        <PageComponent />
      </main>
      <ToastStack />
    </div>
  );
}
