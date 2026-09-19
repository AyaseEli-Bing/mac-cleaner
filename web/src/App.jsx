import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import AppBar from '@mui/material/AppBar';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import Drawer from '@mui/material/Drawer';
import FormControlLabel from '@mui/material/FormControlLabel';
import IconButton from '@mui/material/IconButton';
import MenuItem from '@mui/material/MenuItem';
import Radio from '@mui/material/Radio';
import RadioGroup from '@mui/material/RadioGroup';
import Slider from '@mui/material/Slider';
import Snackbar from '@mui/material/Snackbar';
import Switch from '@mui/material/Switch';
import Tab from '@mui/material/Tab';
import Tabs from '@mui/material/Tabs';
import TextField from '@mui/material/TextField';
import Toolbar from '@mui/material/Toolbar';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';
import Alert from '@mui/material/Alert';
import { ThemeProvider, createTheme, styled } from '@mui/material/styles';
import DarkModeIcon from '@mui/icons-material/DarkModeRounded';
import LightModeIcon from '@mui/icons-material/LightModeRounded';
import SettingsIcon from '@mui/icons-material/SettingsRounded';
import CleaningServicesIcon from '@mui/icons-material/CleaningServicesRounded';

import api from './api.js';
import Dashboard from './pages/Dashboard.jsx';
import ScanPage from './pages/ScanPage.jsx';
import HistoryPage from './pages/HistoryPage.jsx';
import {
  COLORS,
  NoticeBar,
  createAppTheme,
  formatBytes,
  SectionTitle,
} from './theme.js';

export const AppContext = createContext(null);

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useApp 必须在 AppProvider 内使用');
  return ctx;
}

const APPEARANCE_KEY = 'mac-cleaner-appearance';

const Row = styled('div')(() => ({
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 12,
  padding: '10px 0',
}));

const DrawerInner = styled('div')(() => ({
  width: 420,
  maxWidth: '92vw',
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
}));

function readStoredAppearance() {
  try {
    return localStorage.getItem(APPEARANCE_KEY) || 'dark';
  } catch (err) {
    return 'dark';
  }
}

function writeStoredAppearance(value) {
  try {
    localStorage.setItem(APPEARANCE_KEY, value);
  } catch (err) {
    /* 隐私模式下忽略 */
  }
}

function resolveAppearance(appearance) {
  if (appearance === 'system') {
    if (typeof window !== 'undefined' && window.matchMedia) {
      return window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark';
    }
    return 'dark';
  }
  return appearance === 'light' ? 'light' : 'dark';
}

/** 设置抽屉：类目开关、模式、阈值、外观、数据维护。 */
function SettingsDrawer({ open, onClose, settings, onPatch, notify, onCleared }) {
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  if (!settings) return null;

  const catEnabled = settings.category_enabled || {};
  const catSelected = settings.category_default_selected || {};
  const limits = settings.limits || {};
  const downloads = settings.downloads || {};
  const scanConf = settings.scan || {};

  const toggleCategory = async (id, value) => {
    setBusy(true);
    try {
      await onPatch({ category_enabled: { [id]: value } });
      notify(value ? '类目已开启，下次扫描生效' : '类目已关闭');
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const patch = async (value, okText) => {
    setBusy(true);
    try {
      await onPatch(value);
      if (okText) notify(okText);
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const clearHistory = async () => {
    setBusy(true);
    try {
      await api.historyClear();
      notify('历史记录已清空，累计释放量归零');
      setConfirmClear(false);
      if (onCleared) onCleared();
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const exportLogs = async () => {
    try {
      const data = await api.logs('audit', 5000);
      const text = (data.lines || []).join('\n');
      const blob = new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `mac-cleaner-audit-${Date.now()}.log`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      notify('审计日志已导出');
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  return (
    <Drawer anchor="right" open={open} onClose={onClose}>
      <DrawerInner>
        <Box sx={{ p: 2.5, pb: 1, display: 'flex', alignItems: 'center', gap: 1 }}>
          <SettingsIcon fontSize="small" />
          <Typography variant="subtitle1" sx={{ fontWeight: 700, flex: 1 }}>
            设置
          </Typography>
          {busy ? <CircularProgress size={16} /> : null}
        </Box>
        <Divider />
        <Box sx={{ flex: 1, overflowY: 'auto', px: 2.5, py: 1.5 }}>
          <SectionTitle>清理方式</SectionTitle>
          <RadioGroup
            row
            value={settings.mode}
            onChange={(e) => patch({ mode: e.target.value },
              e.target.value === 'trash'
                ? '已切换为「移到废纸篓」，清理后可恢复'
                : '已切换为「直接删除」，删除后不可恢复')}
          >
            <FormControlLabel value="trash" control={<Radio size="small" />}
              label={<span style={{ fontSize: 13 }}>移到废纸篓（推荐）</span>} />
            <FormControlLabel value="delete" control={<Radio size="small" />}
              label={<span style={{ fontSize: 13, color: COLORS.danger }}>
                直接删除（不可恢复）
              </span>} />
          </RadioGroup>
          <NoticeBar $tone={settings.mode === 'trash' ? 'info' : 'danger'} sx={{ mt: 1 }}>
            {settings.mode === 'trash'
              ? '♻️ 清理后文件进入废纸篓，需要清空废纸篓才会真正释放磁盘空间。'
              : '⚠️ 直接删除模式下文件不可恢复，请谨慎操作。'}
          </NoticeBar>

          <Divider sx={{ my: 2 }} />
          <SectionTitle>类目开关</SectionTitle>
          <Row>
            <span style={{ fontSize: 13 }}>外置卷残留（扫描外接磁盘）</span>
            <Switch size="small" checked={!!settings.scan_external_volumes}
              onChange={(e) => patch({ scan_external_volumes: e.target.checked })} />
          </Row>
          <Row>
            <span style={{ fontSize: 13 }}>Xcode 开发垃圾（开发者项）</span>
            <Switch size="small" checked={!!catEnabled.xcode_junk}
              onChange={(e) => toggleCategory('xcode_junk', e.target.checked)} />
          </Row>
          <Row>
            <span style={{ fontSize: 13 }}>包管理器缓存（开发者项）</span>
            <Switch size="small" checked={!!catEnabled.pkg_manager_caches}
              onChange={(e) => toggleCategory('pkg_manager_caches', e.target.checked)} />
          </Row>
          <Row>
            <span style={{ fontSize: 13 }}>系统日志（需磁盘访问权限）</span>
            <Switch size="small" checked={!!catEnabled.system_logs}
              onChange={(e) => toggleCategory('system_logs', e.target.checked)} />
          </Row>
          <Row>
            <span style={{ fontSize: 13 }}>浏览器缓存（隐私敏感项）</span>
            <Switch size="small" checked={!!catEnabled.browser_caches}
              onChange={(e) => toggleCategory('browser_caches', e.target.checked)} />
          </Row>
          <Row>
            <span style={{ fontSize: 13, color: COLORS.danger }}>
              iOS 设备备份（高危）
            </span>
            <Switch size="small" checked={!!catEnabled.ios_backup}
              onChange={(e) => toggleCategory('ios_backup', e.target.checked)} />
          </Row>
          <Row>
            <span style={{ fontSize: 13 }}>下载目录大文件</span>
            <Switch size="small" checked={!!catEnabled.downloads_large}
              onChange={(e) => toggleCategory('downloads_large', e.target.checked)} />
          </Row>
          <NoticeBar $tone="info">
            💡 下载目录大文件默认不会自动勾选，需要你在扫描页逐条确认。
          </NoticeBar>

          <Divider sx={{ my: 2 }} />
          <SectionTitle>保护阈值</SectionTitle>
          <Typography variant="caption" color="text.secondary">
            单次清理体积上限：{formatBytes(limits.max_bytes_per_clean || 0)}
          </Typography>
          <Slider
            size="small"
            min={1 * 1024 ** 3}
            max={200 * 1024 ** 3}
            step={1024 ** 3}
            value={Math.min(limits.max_bytes_per_clean || 0, 200 * 1024 ** 3)}
            onChangeCommitted={(e, value) =>
              patch({ limits: { max_bytes_per_clean: Number(value) } },
                '清理上限已更新')}
          />
          <Typography variant="caption" color="text.secondary">
            单次清理条目上限：{limits.max_items_per_clean || 0} 条
          </Typography>
          <Slider
            size="small"
            min={1000}
            max={200000}
            step={1000}
            value={Math.min(limits.max_items_per_clean || 1000, 200000)}
            onChangeCommitted={(e, value) =>
              patch({ limits: { max_items_per_clean: Number(value) } },
                '条目上限已更新')}
          />
          <Box sx={{ display: 'flex', gap: 1.5, mt: 2 }}>
            <TextField
              size="small" type="number" label="大文件判定(MB)"
              value={Math.round((downloads.min_size_bytes || 0) / 1024 / 1024)}
              onChange={(e) => patch({
                downloads: {
                  min_size_bytes: Number(e.target.value || 0) * 1024 * 1024,
                },
              })}
              inputProps={{ min: 1 }}
            />
            <TextField
              size="small" type="number" label="未使用天数"
              value={downloads.min_days_unused || 30}
              onChange={(e) => patch({
                downloads: { min_days_unused: Number(e.target.value || 1) },
              })}
              inputProps={{ min: 1 }}
            />
          </Box>
          <Box sx={{ mt: 2 }}>
            <TextField
              size="small" select fullWidth label="并发扫描线程"
              value={scanConf.concurrency || 4}
              onChange={(e) => patch({ scan: { concurrency: Number(e.target.value) } },
                '并发数已更新')}
            >
              {[1, 2, 4, 6, 8, 12].map((n) => (
                <MenuItem key={n} value={n}>{n} 个线程</MenuItem>
              ))}
            </TextField>
          </Box>

          <Divider sx={{ my: 2 }} />
          <SectionTitle>外观</SectionTitle>
          <RadioGroup
            row
            value={settings.appearance || 'dark'}
            onChange={(e) => patch({ appearance: e.target.value })}
          >
            <FormControlLabel value="dark" control={<Radio size="small" />}
              label={<span style={{ fontSize: 13 }}>深色</span>} />
            <FormControlLabel value="light" control={<Radio size="small" />}
              label={<span style={{ fontSize: 13 }}>浅色</span>} />
            <FormControlLabel value="system" control={<Radio size="small" />}
              label={<span style={{ fontSize: 13 }}>跟随系统</span>} />
          </RadioGroup>

          <Divider sx={{ my: 2 }} />
          <SectionTitle>数据</SectionTitle>
          <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
            数据文件位于项目 data/ 目录，删除项目目录即无残留。
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, mt: 1.5, flexWrap: 'wrap' }}>
            <Button size="small" variant="outlined" onClick={exportLogs}>
              导出审计日志
            </Button>
            <Button size="small" color="error" variant="outlined"
              onClick={() => setConfirmClear(true)}>
              清空历史记录
            </Button>
          </Box>
          {confirmClear ? (
            <NoticeBar $tone="danger" sx={{ mt: 1.5 }}>
              <Box>
                <div>确定要清空全部历史记录吗？累计释放量将归零。</div>
                <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                  <Button size="small" color="error" variant="contained"
                    onClick={clearHistory}>确认清空</Button>
                  <Button size="small" onClick={() => setConfirmClear(false)}>取消</Button>
                </Box>
              </Box>
            </NoticeBar>
          ) : null}
        </Box>
      </DrawerInner>
    </Drawer>
  );
}

/** 应用外壳：顶栏 + Tab 导航 + 设置抽屉 + 全局 Snackbar。 */
export default function App() {
  const [appearance, setAppearance] = useState(readStoredAppearance());
  const [themeMode, setThemeMode] = useState(() =>
    resolveAppearance(readStoredAppearance()));
  const [page, setPage] = useState('dashboard');
  const [settings, setSettings] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [toast, setToast] = useState({ open: false, message: '', severity: 'info' });
  const [refreshKey, setRefreshKey] = useState(0);
  const [focusCategory, setFocusCategory] = useState(null);
  const initRef = useRef(false);

  const notify = useCallback((message, severity = 'info') => {
    setToast({ open: true, message, severity });
  }, []);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  // 首次加载：读取设置并决定主题
  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;
    api
      .getSettings()
      .then((data) => {
        const next = data.settings;
        setSettings(next);
        const stored = readStoredAppearance();
        const value = stored === 'system' ? next.appearance || 'dark' : stored;
        setAppearance(value);
        setThemeMode(resolveAppearance(value));
      })
      .catch(() => notify('未能读取设置，已使用默认值', 'warning'));
  }, [notify]);

  // 跟随系统外观变化时实时切换
  useEffect(() => {
    if (appearance !== 'system' || typeof window === 'undefined') return undefined;
    const media = window.matchMedia('(prefers-color-scheme: light)');
    const handler = () => setThemeMode(media.matches ? 'light' : 'dark');
    media.addEventListener('change', handler);
    return () => media.removeEventListener('change', handler);
  }, [appearance]);

  // Tailwind 的 darkMode: 'class' 需要同步 html 上的 class
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', themeMode === 'dark');
    root.classList.toggle('light', themeMode === 'light');
  }, [themeMode]);

  const theme = useMemo(() => createAppTheme(themeMode), [themeMode]);

  const toggleTheme = () => {
    const next = themeMode === 'dark' ? 'light' : 'dark';
    setThemeMode(next);
    setAppearance(next);
    writeStoredAppearance(next);
    if (settings && settings.appearance === 'system') {
      api.putSettings({ appearance: next }).catch(() => {});
    }
  };

  const patchSettings = useCallback(async (value) => {
    const data = await api.putSettings(value);
    setSettings(data.settings);
    if (value.appearance !== undefined) {
      setAppearance(value.appearance);
      setThemeMode(resolveAppearance(value.appearance));
      writeStoredAppearance(value.appearance);
    }
    return data.settings;
  }, []);

  const goto = (next, categoryId) => {
    setPage(next);
    if (categoryId) setFocusCategory(categoryId);
  };

  const ctx = useMemo(
    () => ({
      settings,
      patchSettings,
      notify,
      refresh,
      refreshKey,
      goto,
      focusCategory,
      clearFocusCategory: () => setFocusCategory(null),
    }),
    [settings, patchSettings, notify, refresh, refreshKey, focusCategory],
  );

  return (
    <ThemeProvider theme={theme}>
      <AppContext.Provider value={ctx}>
        <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
          <AppBar position="sticky" elevation={0}
            sx={{ bgcolor: 'background.paper', borderBottom: '1px solid',
              borderColor: 'divider' }}>
            <Toolbar sx={{ gap: 2, minHeight: 60 }}>
              <CleaningServicesIcon sx={{ color: 'primary.main' }} />
              <Typography variant="subtitle1" sx={{ fontWeight: 700, mr: 1 }}>
                Mac 清理助手
              </Typography>
              <Tabs
                value={page}
                onChange={(e, value) => setPage(value)}
                sx={{ flex: 1, minHeight: 60 }}
              >
                <Tab value="dashboard" label="首页" sx={{ minHeight: 60 }} />
                <Tab value="scan" label="扫描清理" sx={{ minHeight: 60 }} />
                <Tab value="history" label="历史记录" sx={{ minHeight: 60 }} />
              </Tabs>
              <Tooltip title={themeMode === 'dark' ? '切换到浅色' : '切换到深色'}>
                <IconButton size="small" onClick={toggleTheme}>
                  {themeMode === 'dark'
                    ? <LightModeIcon fontSize="small" />
                    : <DarkModeIcon fontSize="small" />}
                </IconButton>
              </Tooltip>
              <Tooltip title="设置">
                <IconButton size="small" onClick={() => setDrawerOpen(true)}>
                  <SettingsIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            </Toolbar>
          </AppBar>

          <Box sx={{ maxWidth: 1280, mx: 'auto', px: { xs: 2, md: 3 }, py: 3 }}>
            {page === 'dashboard' ? <Dashboard /> : null}
            {page === 'scan' ? <ScanPage /> : null}
            {page === 'history' ? <HistoryPage /> : null}
          </Box>

          <SettingsDrawer
            open={drawerOpen}
            onClose={() => setDrawerOpen(false)}
            settings={settings}
            onPatch={patchSettings}
            notify={notify}
            onCleared={refresh}
          />

          <Snackbar
            open={toast.open}
            autoHideDuration={3600}
            onClose={() => setToast((t) => ({ ...t, open: false }))}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
          >
            <Alert
              severity={toast.severity}
              variant="filled"
              onClose={() => setToast((t) => ({ ...t, open: false }))}
              sx={{ maxWidth: 560 }}
            >
              {toast.message}
            </Alert>
          </Snackbar>
        </Box>
      </AppContext.Provider>
    </ThemeProvider>
  );
}
