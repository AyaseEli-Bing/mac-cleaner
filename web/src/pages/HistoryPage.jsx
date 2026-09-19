import React, { useCallback, useEffect, useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import Drawer from '@mui/material/Drawer';
import IconButton from '@mui/material/IconButton';
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
  PathText,
  RESULT_META,
  SectionTitle,
  SizeText,
  StatusBadge,
  formatBytes,
  formatDateTime,
  formatDuration,
  shortPath,
} from '../theme.js';

const CATEGORY_NAMES = {
  user_caches: '用户缓存',
  app_caches: '应用缓存',
  system_caches: '系统级缓存',
  user_logs: '用户日志',
  system_logs: '系统日志',
  temp_files: '临时文件',
  trash_residue: '回收站残留',
  downloads_large: '下载目录大文件',
  xcode_junk: 'Xcode 开发垃圾',
  pkg_manager_caches: '包管理器缓存',
  browser_caches: '浏览器缓存',
  ios_backup: 'iOS 设备备份',
  external_volumes: '外置卷残留',
};

/** 历史记录页：累计卡片 + 记录列表 + 明细抽屉 + 清空历史。 */
export default function HistoryPage() {
  const { notify, refreshKey, refresh } = useApp();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.historyList(100, 0));
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [notify]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const openDetail = async (record) => {
    if (!record.has_detail) {
      notify('本次明细已按策略归档，仅保留最近 5 次', 'warning');
      return;
    }
    try {
      const payload = await api.historyDetail(record.id);
      setDetail({ record: payload.record, items: payload.items });
      setDetailOpen(true);
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  const clearHistory = async () => {
    setBusy(true);
    try {
      const res = await api.historyClear();
      notify(`已清空 ${res.cleared} 条历史记录，累计释放量归零`, 'success');
      setClearOpen(false);
      setDetailOpen(false);
      refresh();
      await load();
    } catch (err) {
      notify(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  const records = data?.records || [];
  const cumulative = data?.cumulative_freed_bytes || 0;
  const cleanCount = data?.clean_count || 0;
  const firstUsedAt = data?.first_used_at;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      {/* T 区 汇总卡片 */}
      <Panel>
        <Box sx={{
          display: 'grid', gap: 2,
          gridTemplateColumns: { xs: '1fr', sm: 'repeat(3, 1fr)' },
        }}>
          <Box>
            <Typography variant="caption" color="text.secondary">累计释放</Typography>
            <Typography sx={{
              fontSize: 32, fontWeight: 700,
              color: cumulative > 0 ? COLORS.success : 'text.secondary',
            }}>
              {formatBytes(cumulative)}
            </Typography>
          </Box>
          <Box>
            <Typography variant="caption" color="text.secondary">共清理</Typography>
            <Typography sx={{ fontSize: 32, fontWeight: 700 }}>{cleanCount} 次</Typography>
          </Box>
          <Box>
            <Typography variant="caption" color="text.secondary">首次使用</Typography>
            <Typography sx={{ fontSize: 32, fontWeight: 700 }}>
              {firstUsedAt ? formatDateTime(firstUsedAt).slice(0, 10) : '—'}
            </Typography>
          </Box>
        </Box>
        {cumulative > 0 && firstUsedAt ? (
          <NoticeBar $tone="success" sx={{ mt: 2 }}>
            🎉 自 {formatDateTime(firstUsedAt).slice(0, 10)} 使用以来，已为你释放{' '}
            <strong>{formatBytes(cumulative)}</strong> 磁盘空间。
          </NoticeBar>
        ) : null}
      </Panel>

      {/* U 区 记录列表 */}
      <Panel>
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 1.5 }}>
          <SectionTitle sx={{ m: 0, flex: 1 }}>清理记录（{records.length} 条）</SectionTitle>
          <Button size="small" onClick={load} disabled={loading}>刷新</Button>
        </Box>
        {loading ? (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <Skeleton variant="rounded" height={72} />
            <Skeleton variant="rounded" height={72} />
            <Skeleton variant="rounded" height={72} />
          </Box>
        ) : records.length === 0 ? (
          <EmptyState>
            <EmptyStateIcon>📭</EmptyStateIcon>
            <EmptyStateTitle>还没有清理记录</EmptyStateTitle>
            <EmptyStateHint>
              完成第一次清理后，这里会记录每次清理释放了多少空间，以及清理了哪些路径。
            </EmptyStateHint>
          </EmptyState>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.25 }}>
            {records.map((record) => (
              <Box key={record.id} sx={{
                border: '1px solid', borderColor: 'divider', borderRadius: 2, p: 1.75,
              }}>
                <Box sx={{
                  display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap',
                }}>
                  <Typography sx={{ fontWeight: 600, minWidth: 132 }}>
                    {formatDateTime(record.started_at)}
                  </Typography>
                  <StatusBadge $status={record.status}>
                    {RESULT_META[record.status]?.label || record.status}
                  </StatusBadge>
                  <SizeText $strong sx={{ color: COLORS.success, minWidth: 84 }}>
                    {formatBytes(record.freed_bytes)}
                  </SizeText>
                  <Typography variant="caption" color="text.secondary">
                    {record.item_count} 项
                    {record.skipped_count ? ` · 跳过 ${record.skipped_count}` : ''}
                    {record.failed_count ? ` · 失败 ${record.failed_count}` : ''}
                    {' · 用时 '}{formatDuration(record.duration_ms)}
                  </Typography>
                  <Box sx={{ flex: 1 }} />
                  <Tooltip title={record.has_detail ? '查看本次清理的路径明细' :
                    '本次明细已按策略归档，仅保留最近 5 次'}>
                    <span>
                      <Button size="small" variant="outlined"
                        disabled={!record.has_detail}
                        onClick={() => openDetail(record)}>
                        查看详情
                      </Button>
                    </span>
                  </Tooltip>
                </Box>
                <Box sx={{ display: 'flex', gap: 0.75, mt: 1.25, flexWrap: 'wrap' }}>
                  <Chip size="small" variant="outlined"
                    label={record.mode === 'trash' ? '移到废纸篓' : '直接删除'} />
                  {(record.categories || []).map((cid) => (
                    <Chip key={cid} size="small" variant="outlined"
                      label={CATEGORY_NAMES[cid] || cid} />
                  ))}
                </Box>
              </Box>
            ))}
          </Box>
        )}
      </Panel>

      {/* W 区 清空历史 */}
      <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button size="small" color="error" variant="outlined"
          disabled={!records.length} onClick={() => setClearOpen(true)}>
          清空历史记录
        </Button>
      </Box>

      {/* V 区 明细抽屉 */}
      <Drawer anchor="right" open={detailOpen} onClose={() => setDetailOpen(false)}>
        <Box sx={{ width: 520, maxWidth: '92vw', display: 'flex', flexDirection: 'column', height: '100%' }}>
          <Box sx={{ p: 2.5, pb: 1.5, display: 'flex', alignItems: 'center', gap: 1 }}>
            <Typography variant="subtitle1" sx={{ fontWeight: 700, flex: 1 }}>
              清理明细
            </Typography>
            <IconButton size="small" onClick={() => setDetailOpen(false)}>✕</IconButton>
          </Box>
          <Divider />
          {detail ? (
            <>
              <Box sx={{ px: 2.5, py: 1.5 }}>
                <Typography variant="caption" color="text.secondary">
                  {formatDateTime(detail.record.started_at)} ·
                  {' '}释放 {formatBytes(detail.record.freed_bytes)} ·
                  {' '}耗时 {formatDuration(detail.record.duration_ms)}
                </Typography>
                <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                  <Chip size="small" color="success" variant="outlined"
                    label={`成功 ${detail.record.success_count}`} />
                  <Chip size="small" color="warning" variant="outlined"
                    label={`跳过 ${detail.record.skipped_count}`} />
                  <Chip size="small" color="error" variant="outlined"
                    label={`失败 ${detail.record.failed_count}`} />
                </Box>
              </Box>
              <Divider />
              <Box sx={{ flex: 1, overflowY: 'auto', px: 2.5, py: 1.5 }}>
                {detail.items.length === 0 ? (
                  <EmptyState>
                    <EmptyStateIcon>📦</EmptyStateIcon>
                    <EmptyStateTitle>暂无路径明细</EmptyStateTitle>
                    <EmptyStateHint>本次明细已按策略归档。</EmptyStateHint>
                  </EmptyState>
                ) : (
                  detail.items.map((row, idx) => (
                    <Box key={row.id || idx} sx={{
                      py: 0.85, borderBottom: '1px dashed', borderColor: 'divider',
                    }}>
                      <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center' }}>
                        <StatusBadge $status={row.result}>
                          {RESULT_META[row.result]?.label || row.result}
                        </StatusBadge>
                        <Box sx={{ flex: 1, minWidth: 0 }}>
                          <PathText>{shortPath(row.path)}</PathText>
                        </Box>
                        <SizeText>{formatBytes(row.size)}</SizeText>
                      </Box>
                      {row.reason_text ? (
                        <Typography variant="caption" color="text.secondary">
                          {row.reason_text}
                        </Typography>
                      ) : null}
                    </Box>
                  ))
                )}
              </Box>
            </>
          ) : null}
        </Box>
      </Drawer>

      {/* 清空历史二次确认 */}
      <Dialog open={clearOpen} onClose={() => setClearOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontWeight: 700 }}>清空历史记录</DialogTitle>
        <DialogContent dividers>
          <Typography variant="body2">
            确定要清空全部历史记录吗？累计释放空间将归零，该操作不可撤销。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 2.5, py: 1.75 }}>
          <Button size="small" onClick={() => setClearOpen(false)} disabled={busy}>取消</Button>
          <Button size="small" color="error" variant="contained" disableElevation
            onClick={clearHistory} disabled={busy}
            startIcon={busy ? <CircularProgress size={14} color="inherit" /> : null}>
            确认清空
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
