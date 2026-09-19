import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import LinearProgress from '@mui/material/LinearProgress';
import Skeleton from '@mui/material/Skeleton';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';

import api from '../api.js';
import { useApp } from '../App.jsx';
import {
  COLORS,
  EmptyState,
  EmptyStateHint,
  EmptyStateIcon,
  EmptyStateTitle,
  NoticeBar,
  Panel,
  SectionTitle,
  StatLabel,
  StatValue,
  formatBytes,
  formatDateTime,
} from '../theme.js';

/** 环形磁盘容量图（自绘 SVG，不引入图表库）。 */
function RingChart({ used, total, reclaimable, free }) {
  const size = 168;
  const stroke = 18;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const usedRatio = total > 0 ? Math.min(used / total, 1) : 0;
  const reclaimRatio = total > 0 ? Math.min(reclaimable / total, 1) : 0;
  const usedDash = circumference * usedRatio;
  const reclaimDash = circumference * reclaimRatio;

  return (
    <Box sx={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none"
          stroke="rgba(128,128,128,0.18)" strokeWidth={stroke} />
        <circle
          cx={size / 2} cy={size / 2} r={radius} fill="none"
          stroke={COLORS.brand} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={`${usedDash} ${circumference}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
        <circle
          cx={size / 2} cy={size / 2} r={radius} fill="none"
          stroke={COLORS.success} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={`${reclaimDash} ${circumference}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          opacity={0.85}
        />
      </svg>
      <Box sx={{
        position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
      }}>
        <Typography variant="h6" sx={{ fontWeight: 700 }}>
          {total > 0 ? Math.round((used / total) * 100) : 0}%
        </Typography>
        <Typography variant="caption" color="text.secondary">已用</Typography>
      </Box>
    </Box>
  );
}

/** Top5 类目横向条形图，点击可跳转扫描页。 */
function CategoryBars({ categories, onJump }) {
  if (!categories.length) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
        还没有扫描结果，先去「扫描清理」页跑一次吧。
      </Typography>
    );
  }
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {categories.map((cat) => (
        <Box
          key={cat.category_id}
          onClick={() => onJump(cat.category_id)}
          sx={{ cursor: 'pointer', '&:hover .bar-name': { color: 'primary.main' } }}
        >
          <Box sx={{
            display: 'flex', justifyContent: 'space-between',
            alignItems: 'baseline', mb: 0.5,
          }}>
            <Typography className="bar-name" variant="body2" sx={{ fontWeight: 600 }}>
              {cat.name}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              <span style={{ color: 'text.primary' }}>{formatBytes(cat.size)}</span>
              {' · '}{cat.percent?.toFixed ? cat.percent.toFixed(1) : cat.percent}%
            </Typography>
          </Box>
          <LinearProgress
            variant="determinate"
            value={Math.min(Math.max(cat.percent || 0, 0), 100)}
            sx={{
              height: 8, borderRadius: 6, bgcolor: 'rgba(128,128,128,0.16)',
              '& .MuiLinearProgress-bar': {
                borderRadius: 6,
                backgroundColor: cat.risk === 'high'
                  ? COLORS.danger
                  : cat.risk === 'medium' ? COLORS.warning : COLORS.brand,
              },
            }}
          />
        </Box>
      ))}
    </Box>
  );
}

/** 首页仪表盘：磁盘总览 + 累计释放 + Top5 排行 + 安全提示 + 快速设置。 */
export default function Dashboard() {
  const { settings, notify, goto, refreshKey, patchSettings } = useApp();
  const [disk, setDisk] = useState(null);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyScan, setBusyScan] = useState(false);

  const load = useCallback(async () => {
    try {
      const [diskData, resultData] = await Promise.all([
        api.diskOverview(),
        api.scanResult().catch(() => null),
      ]);
      setDisk(diskData);
      setResult(resultData);
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [notify]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const top5 = useMemo(() => {
    const list = (result?.categories || []).filter(
      (c) => c.selected && c.size > 0 && c.status === 'ok');
    return list.slice().sort((a, b) => b.size - a.size).slice(0, 5);
  }, [result]);

  const startScan = async (rescan) => {
    setBusyScan(true);
    try {
      await api.scanStart(null, !!rescan);
      goto('scan');
    } catch (err) {
      // 扫描已在進行中时也直接跳过去，让用户看到进度
      if (err.code === 1003) {
        goto('scan');
        return;
      }
      notify(err.message, 'error');
    } finally {
      setBusyScan(false);
    }
  };

  const toggleChip = async (what, value, okText) => {
    try {
      await patchSettings(what);
      notify(okText || '设置已更新');
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  if (loading) {
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Skeleton variant="rounded" height={220} />
        <Skeleton variant="rounded" height={260} />
      </Box>
    );
  }

  const total = disk?.total_bytes || 0;
  const used = disk?.used_bytes || 0;
  const free = disk?.free_bytes || 0;
  const reclaimable = disk?.reclaimable_bytes || 0;
  const cumulative = disk?.cumulative_freed_bytes || 0;
  const cleanCount = disk?.clean_count || 0;
  const firstUsedAt = disk?.first_used_at;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      <Box sx={{
        display: 'grid', gap: 2, gridTemplateColumns: {
          xs: '1fr', lg: 'minmax(0, 1.1fr) minmax(0, 0.9fr)',
        },
      }}>
        {/* A 区 磁盘总览 */}
        <Panel>
          <SectionTitle>磁盘空间</SectionTitle>
          <Box sx={{ display: 'flex', gap: 3, alignItems: 'center', flexWrap: 'wrap' }}>
            <RingChart used={used} total={total} reclaimable={reclaimable} free={free} />
            <Box sx={{ flex: 1, minWidth: 200 }}>
              <RowData label="总容量" value={formatBytes(total)} />
              <RowData label="已用" value={formatBytes(used)} color={COLORS.brand} />
              <RowData label="可用" value={formatBytes(free)} color={COLORS.success} />
              <RowData
                label="预计可清理"
                value={formatBytes(reclaimable)}
                color={reclaimable > 0 ? COLORS.success : 'text.secondary'}
                strong
              />
            </Box>
          </Box>
          <Box sx={{ display: 'flex', gap: 1.25, mt: 2, flexWrap: 'wrap' }}>
            <Button
              size="small" variant="contained" disableElevation
              disabled={busyScan}
              startIcon={busyScan ? <CircularProgress size={14} color="inherit" /> : null}
              onClick={() => startScan(false)}
            >
              {result ? '查看扫描结果' : '开始扫描'}
            </Button>
            {result ? (
              <Button size="small" variant="outlined" disabled={busyScan}
                onClick={() => startScan(true)}>
                重新扫描
              </Button>
            ) : null}
          </Box>
        </Panel>

        {/* B 区 累计清理成果 */}
        <Panel>
          <SectionTitle>累计清理成果</SectionTitle>
          <StatValue sx={{ fontSize: 38, color: cumulative > 0 ? COLORS.success : 'text.secondary' }}>
            {formatBytes(cumulative)}
          </StatValue>
          <StatLabel>累计已释放</StatLabel>
          <Typography variant="caption" color="text.secondary" sx={{ mt: 2, display: 'block' }}>
            {firstUsedAt
              ? `自 ${formatDateTime(firstUsedAt).slice(0, 10)} 使用以来 · 共清理 ${cleanCount} 次`
              : '还没有清理记录，完成第一次清理后这里会有数字'}
          </Typography>
          <Box sx={{ mt: 2.5 }}>
            <Button size="small" variant="text" onClick={() => goto('history')}>
              查看历史记录 →
            </Button>
          </Box>
        </Panel>
      </Box>

      {/* C 区 类目排行 */}
      <Panel>
        <SectionTitle>空间占用排行（点击查看详情）</SectionTitle>
        <CategoryBars
          categories={top5}
          onJump={(cid) => goto('scan', cid)}
        />
      </Panel>

      {/* E 区 安全提示 */}
      <NoticeBar $tone="success">
        🛡️ 本工具默认只清理缓存 / 日志 / 临时文件，不会触碰你的文档、照片、邮件与系统文件。
      </NoticeBar>

      {/* F 区 快速设置 */}
      <Panel>
        <SectionTitle>快速设置</SectionTitle>
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
          <Tooltip title="点击切换为「直接删除」">
            <Chip
              label={`移到废纸篓：${settings?.mode === 'trash' ? '开' : '关'}`}
              onClick={() => toggleChip(
                { mode: settings?.mode === 'trash' ? 'delete' : 'trash' },
                null,
                settings?.mode === 'trash' ? '已切换为直接删除（不可恢复）' : '已切换为移到废纸篓',
              )}
              color={settings?.mode === 'trash' ? 'primary' : 'default'}
              variant={settings?.mode === 'trash' ? 'filled' : 'outlined'}
            />
          </Tooltip>
          <Chip
            label={`开发者类目：${
              settings?.category_enabled?.xcode_junk ||
              settings?.category_enabled?.pkg_manager_caches ? '开' : '关'}`}
            onClick={() => {
              const next = !(
                settings?.category_enabled?.xcode_junk ||
                settings?.category_enabled?.pkg_manager_caches);
              toggleChip(
                {
                  category_enabled: {
                    xcode_junk: next,
                    pkg_manager_caches: next,
                  },
                },
                null,
                next ? '开发者类目已开启' : '开发者类目已关闭',
              );
            }}
            variant={
              settings?.category_enabled?.xcode_junk ||
              settings?.category_enabled?.pkg_manager_caches ? 'filled' : 'outlined'}
            color={
              settings?.category_enabled?.xcode_junk ||
              settings?.category_enabled?.pkg_manager_caches ? 'primary' : 'default'}
          />
          <Chip
            label={`浏览器缓存：${settings?.category_enabled?.browser_caches ? '开' : '关'}`}
            onClick={() => {
              const next = !settings?.category_enabled?.browser_caches;
              toggleChip(
                { category_enabled: { browser_caches: next } },
                null,
                next ? '浏览器缓存类目已开启（清理后部分网站需重新登录）' : '浏览器缓存类目已关闭',
              );
            }}
            variant={settings?.category_enabled?.browser_caches ? 'filled' : 'outlined'}
            color={settings?.category_enabled?.browser_caches ? 'primary' : 'default'}
          />
        </Box>
        <Divider sx={{ my: 2 }} />
        {top5.length ? null : (
          <EmptyState>
            <EmptyStateIcon>🧹</EmptyStateIcon>
            <EmptyStateTitle>还没有扫描结果</EmptyStateTitle>
            <EmptyStateHint>
              点上面的「开始扫描」，几秒钟后就能看到哪些缓存可以安全清理。
            </EmptyStateHint>
          </EmptyState>
        )}
      </Panel>
    </Box>
  );
}

function RowData({ label, value, color, strong }) {
  return (
    <Box sx={{
      display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
      py: 0.5, borderBottom: '1px dashed', borderColor: 'divider',
    }}>
      <Typography variant="body2" color="text.secondary">{label}</Typography>
      <Typography
        variant="body2"
        sx={{
          fontWeight: strong ? 700 : 600,
          color: color || 'text.primary',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </Typography>
    </Box>
  );
}
