import React from 'react';
import ReactDOM from 'react-dom/client';

import App from './App';
import { installPreloadErrorRecovery } from './preloadRecovery';
import { UiProvider } from './uiContext';

import './index.css';
import './styles/base.css';
import './styles/workflows.css';
import './styles/operations.css';
import './styles/responsive.css';
import './styles/runtime-pages.css';

installPreloadErrorRecovery(window);

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <UiProvider>
      <App />
    </UiProvider>
  </React.StrictMode>,
);
