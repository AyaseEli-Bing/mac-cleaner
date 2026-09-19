/** MUI 主题、语义色 token 与通用展示原子组件。
 *
 * 颜色语义严格对齐《架构设计》10.4 节，保证前后端一致。
 * 本文件为纯 JS，**不包含 JSX**（原子组件一律用 MUI `styled` 构造），
 * 避免额外的 loader 配置。
 */
import { createTheme, styled } from '@mui/material/styles';
import Box from '@mui/material/Box';

/** 全局语义色（与 tailwind.config.js 保持一致）。 */
export const COLORS = {
  brand: '#4C8DF6',
  success: '#3FB950',
  warning: '#F5A623',
  danger: '#F2545B',
  bgDark: '#121212',
  cardDark: '#1E1E1E',
  bgLight: '#F5F5F5',
  cardLight: '#FFFFFF',
  textDark: '#E6E6E6',
  textLight: '#1A1A1A',
};

/** 风险等级色与文案（4.3 节 + UI-04）。 */
export const RISK_META = {
  low: { color: COLORS.success, label: '低风险', emoji: '🟢', desc: '可放心清理' },
  medium: { color: COLORS.warning, label: '中风险', emoji: '🟡', desc: '请留意提示' },
  high: { color: COLORS.danger, label: '高风险', emoji: '🔴', desc: '需额外确认' },
};

/** 清理结果状态色与文案（CLEAN-06）。 */
export const RESULT_META = {
  success: { color: COLORS.success, label: '成功' },
  skipped: { color: COLORS.warning, label: '跳过' },
  failed: { color: COLORS.danger, label: '失败' },
  completed: { color: COLORS.success, label: '完成' },
  aborted: { color: COLORS.warning, label: '已中止' },
  error: { color: COLORS.danger, label: '出错' },
  idle: { color: '#8A8A8A', label: '空闲' },
  running: { color: COLORS.brand, label: '进行中' },
};

const dark = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: COLORS.brand },
    secondary: { main: COLORS.success },
    warning: { main: COLORS.warning },
    error: { main: COLORS.danger },
    success: { main: COLORS.success },
    background: { default: COLORS.bgDark, paper: COLORS.cardDark },
    text: { primary: COLORS.textDark, secondary: '#9E9E9E' },
    divider: 'rgba(255,255,255,0.10)',
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily: [
      '-apple-system',
      'BlinkMacSystemFont',
      '"PingFang SC"',
      '"Microsoft YaHei"',
      'Roboto',
      'sans-serif',
    ].join(','),
    h6: { fontWeight: 600 },
    subtitle2: { fontWeight: 600 },
  },
  components: {
    MuiPaper: { styleOverrides: { root: { backgroundImage: 'none' } } },
    MuiButton: { styleOverrides: { root: { textTransform: 'none', borderRadius: 10 } } },
    MuiTooltip: { defaultProps: { arrow: true } },
  },
});

const light = createTheme({
  palette: {
    mode: 'light',
    primary: { main: COLORS.brand },
    secondary: { main: COLORS.success },
    warning: { main: COLORS.warning },
    error: { main: COLORS.danger },
    success: { main: COLORS.success },
    background: { default: COLORS.bgLight, paper: COLORS.cardLight },
    text: { primary: COLORS.textLight, secondary: '#616161' },
    divider: 'rgba(0,0,0,0.10)',
  },
  shape: { borderRadius: 12 },
  typography: dark.typography,
  components: dark.components,
});

/** 生成主题对象。
 * @param {'dark'|'light'} mode 主题模式
 */
export function createAppTheme(mode = 'dark') {
  return mode === 'light' ? light : dark;
}

// ------------------------------------------------------------------ 格式化
/** 字节格式化：保留 1 位小数，< 1 MB 显示 KB（STAT-05）。 */
export function formatBytes(num) {
  const value = Number(num) || 0;
  if (value <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  if (i === 0) return `${Math.round(v)} B`;
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

/** GB 数量格式化（用于「20 GB」这类阈值展示）。 */
export function bytesToGB(num) {
  return Math.round((Number(num) || 0) / (1024 * 1024 * 1024));
}

/** ISO 时间串 → `YYYY-MM-DD HH:mm`。 */
export function formatDateTime(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** 毫秒 → 人类可读耗时。 */
export function formatDuration(ms) {
  const value = Number(ms) || 0;
  if (value < 1000) return `${Math.round(value)} 毫秒`;
  const sec = value / 1000;
  if (sec < 60) return `${sec.toFixed(1)} 秒`;
  const min = Math.floor(sec / 60);
  return `${min} 分 ${Math.round(sec % 60)} 秒`;
}

/** 路径缩写显示：把主目录前缀折叠为 ~（UI-03）。 */
export function shortPath(path, home = '') {
  if (!path) return '';
  if (home && path.startsWith(home)) return `~${path.slice(home.length)}`;
  const marker = '/Users/';
  const idx = path.indexOf(marker);
  if (idx === 0) {
    const rest = path.slice(marker.length).split('/');
    if (rest.length > 1) return `~/${rest.slice(1).join('/')}`;
  }
  return path;
}

// ------------------------------------------------------------------ 原子组件
/** 卡片容器。 */
export const Panel = styled(Box)(({ theme }) => ({
  background: theme.palette.background.paper,
  border: `1px solid ${theme.palette.divider}`,
  borderRadius: theme.shape.borderRadius * 1.2,
  padding: theme.spacing(2.5),
}));

/** 分区小标题。 */
export const SectionTitle = styled('div')(({ theme }) => ({
  fontSize: 13,
  fontWeight: 600,
  letterSpacing: 0.4,
  color: theme.palette.text.secondary,
  marginBottom: theme.spacing(1.25),
}));

/** 大数字（用于统计卡片）。 */
export const StatValue = styled('div')(({ theme }) => ({
  fontSize: 30,
  lineHeight: 1.2,
  fontWeight: 700,
  color: theme.palette.text.primary,
  fontVariantNumeric: 'tabular-nums',
}));

/** 大数字的副标题。 */
export const StatLabel = styled('div')(({ theme }) => ({
  fontSize: 13,
  color: theme.palette.text.secondary,
  marginTop: 4,
}));

/** 风险徽章：$risk = low | medium | high。 */
export const RiskBadge = styled('span')(({ theme, $risk = 'low' }) => {
  const meta = RISK_META[$risk] || RISK_META.low;
  return {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    fontSize: 12,
    fontWeight: 600,
    padding: '2px 8px',
    borderRadius: 999,
    color: meta.color,
    border: `1px solid ${meta.color}55`,
    background: `${meta.color}18`,
    whiteSpace: 'nowrap',
  };
});

/** 结果状态徽章：$status = success | skipped | failed | ... */
export const StatusBadge = styled('span')(({ theme, $status = 'idle' }) => {
  const meta = RESULT_META[$status] || RESULT_META.idle;
  return {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    fontSize: 12,
    fontWeight: 600,
    padding: '2px 8px',
    borderRadius: 999,
    color: meta.color,
    border: `1px solid ${meta.color}55`,
    background: `${meta.color}18`,
    whiteSpace: 'nowrap',
  };
});

/** 体积文本（等宽 + 主色强调）。 */
export const SizeText = styled('span')(({ theme, $strong = false }) => ({
  fontVariantNumeric: 'tabular-nums',
  fontWeight: $strong ? 700 : 500,
  color: $strong ? theme.palette.primary.main : theme.palette.text.primary,
}));

/** 空状态容器。 */
export const EmptyState = styled('div')(({ theme }) => ({
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  gap: theme.spacing(1),
  padding: theme.spacing(6, 2),
  textAlign: 'center',
  color: theme.palette.text.secondary,
}));

/** 空状态图标区。 */
export const EmptyStateIcon = styled('div')(() => ({
  fontSize: 40,
  lineHeight: 1,
  opacity: 0.85,
}));

/** 空状态主文案。 */
export const EmptyStateTitle = styled('div')(({ theme }) => ({
  fontSize: 15,
  fontWeight: 600,
  color: theme.palette.text.primary,
}));

/** 空状态辅助说明。 */
export const EmptyStateHint = styled('div')(() => ({
  fontSize: 13,
  maxWidth: 420,
  lineHeight: 1.7,
}));

/** 等宽小号路径文本。 */
export const PathText = styled('span')(({ theme }) => ({
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: 11.5,
  color: theme.palette.text.secondary,
  wordBreak: 'break-all',
}));

/** 提示条（安全说明 / 无权限引导）。 */
export const NoticeBar = styled('div')(({ theme, $tone = 'info' }) => {
  const map = {
    info: theme.palette.primary.main,
    success: theme.palette.success.main,
    warning: theme.palette.warning.main,
    danger: theme.palette.error.main,
  };
  const color = map[$tone] || map.info;
  return {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 8,
    fontSize: 13,
    lineHeight: 1.7,
    padding: theme.spacing(1.25, 1.5),
    borderRadius: 10,
    color: theme.palette.text.primary,
    background: `${color}14`,
    border: `1px solid ${color}33`,
  };
});
