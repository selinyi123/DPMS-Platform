import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { dictionaries } from './i18n/dictionaries';

const UiContext = createContext(null);

export function UiProvider({ children }) {
  const [language, setLanguageState] = useState(() => localStorage.getItem('dpms_language') || 'zh');
  const [theme, setThemeState] = useState(() => localStorage.getItem('dpms_theme') || 'system');
  const [toasts, setToasts] = useState([]);
  const [page, setPage] = useState('dashboard');
  const [pageParams, setPageParams] = useState({});

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const applyTheme = () => {
      const resolved = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme;
      document.documentElement.dataset.themeMode = theme;
      document.documentElement.dataset.theme = resolved;
    };
    applyTheme();
    media.addEventListener('change', applyTheme);
    return () => media.removeEventListener('change', applyTheme);
  }, [theme]);

  const setLanguage = useCallback((next) => {
    setLanguageState(next);
    localStorage.setItem('dpms_language', next);
  }, []);

  const setTheme = useCallback((next) => {
    setThemeState(next);
    localStorage.setItem('dpms_theme', next);
  }, []);

  const notify = useCallback((message, type = 'info') => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts(prev => [...prev, { id, message, type }]);
    window.setTimeout(() => {
      setToasts(prev => prev.filter(item => item.id !== id));
    }, 3600);
  }, []);

  const t = useCallback((path) => {
    const parts = path.split('.');
    let current = dictionaries[language] || dictionaries.zh;
    for (const part of parts) current = current?.[part];
    return current || path;
  }, [language]);

  // Cross-page navigation with an optional params payload (e.g. a deep link
  // from Governance into the Semantic Trace for a specific lottery). Manual
  // nav-bar clicks pass no params, which clears any stale deep-link payload.
  const navigate = useCallback((nextPage, params = {}) => {
    setPageParams(params);
    setPage(nextPage);
  }, []);

  const value = useMemo(
    () => ({ language, setLanguage, theme, setTheme, toasts, notify, t, page, setPage, pageParams, navigate }),
    [language, notify, setLanguage, setTheme, t, theme, toasts, page, pageParams, navigate],
  );

  return <UiContext.Provider value={value}>{children}</UiContext.Provider>;
}

export function useUi() {
  const context = useContext(UiContext);
  if (!context) throw new Error('useUi must be used inside UiProvider');
  return context;
}
