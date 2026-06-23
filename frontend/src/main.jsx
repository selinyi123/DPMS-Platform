import React from 'react';
import ReactDOM from 'react-dom/client';

import App from './App';
import { UiProvider } from './uiContext';

import './index.css';
import './styles/base.css';
import './styles/workflows.css';
import './styles/operations.css';
import './styles/responsive.css';
import './styles/runtime-pages.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <UiProvider>
      <App />
    </UiProvider>
  </React.StrictMode>,
);
