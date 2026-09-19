import React from 'react';
import { createRoot } from 'react-dom/client';
import CssBaseline from '@mui/material/CssBaseline';
import { ThemeProvider } from '@mui/material/styles';
import App from './App.jsx';
import { createAppTheme } from './theme.js';
import './index.css';

const container = document.getElementById('root');
const root = createRoot(container);

// 出厂默认深色主题（UI-02 / 团队决策）
root.render(
  <ThemeProvider theme={createAppTheme('dark')}>
    <CssBaseline />
    <App />
  </ThemeProvider>,
);
