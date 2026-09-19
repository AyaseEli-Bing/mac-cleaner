import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import IconButton from '@mui/material/IconButton';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';

import { Accordion, AccordionDetails, AccordionSummary } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMoreRounded';
import DeleteSweepIcon from '@mui/icons-material/DeleteSweepRounded';
import StopIcon from '@mui/icons-material/StopRounded';

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
  RISK_META,
  RESULT_META,
  RiskBadge,
  SectionTitle,
  SizeText,
  StatusBadge,
  formatBytes,
  formatDateTime,
  shortPath,
} from '../theme.js';

const CONFIRM_TEXT = '确认删除';
const MAX_ROWS = 200;

const GROUP_FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'cache', label: '缓存' },
  { key: 'log', label: '日志' },
  { key: 'temp', label: '临时文件' },
  { key: 'trash', label: '回收站' },
  { key: 'dev', label: '开发者' },
  { key: 'privacy', label: '隐私敏感' },
  { key: 'personal', label: '个人文件' },
  { key: 'external', label: '外置卷' },
];

/** 扫描清理页：扫描状态条 + 汇总操作栏 + 类目手风琴 + 确认弹窗 + 进度/结果弹层。 */
export default function ScanPage() {
  const { settings, notify, refresh, focusCategory, clearFocusCategory, refreshKey } =
    useApp();
  const [status, setStatus] = useState('idle');
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [expanded, setExpanded] = useState(() => new Set());
  const [group, setGroup] = useState('all');
  const [sort, setSort] = useState('size');
  const [search, setSearch] = useState('');
  const [showPaths, setShowPaths] = useState(() => new Set());
  const [preview, setPreview] = useState(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [confirmOutside, setConfirmOutside] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [job, setJob] = useState(null);
  const [jobOpen, setJobOpen] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const bootedRef = useRef(false);
  const timerRef = useRef(null);

  const mode = settings?.mode || 'trash';

  // ---------------------------------------------------------------- 数据
  const loadResult = useCallback(async () => {
    try {
      const data = await api.scanResult();
      setResult(data);
      setStatus(data.status);
      return data;
    } catch (err) {
      if (err.code !== 1004) notify(err.message, 'error');
      return null;
    }
  }, [notify]);

  const pollOnce = useCallback(async () => {
    try {
      const p = await api.scanProgress();
      setProgress(p);
      if (p.status === 'running') {
        setStatus('running');
        return true;
      }
      await loadResult();
      setStatus(p.status || 'done');
      return false;
    } catch (err) {
      setStatus('idle');
      return false;
    }
  }, [loadResult]);

  const schedulePoll = useCallback((delay) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      const running = await pollOnce();
      schedulePoll(running ? 1000 : 4000);
    }, delay);
  }, [pollOnce]);

  useEffect(() => {
    // 首次进入：有结果就直接展示，否则自动启动一次扫描
    if (bootedRef.current) return undefined;
    bootedRef.current = true;
    (async () => {
      const existing = await loadResult();
      const running = await pollOnce();
      if (!existing && !running) startScan();
    })();
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  // 结果变化时重建勾选集合（以服务端 default_selected 为准）
  useEffect(() => {
    if (!result) return;
    const next = new Set();
    result.categories.forEach((cat) => {
      cat.items.forEach((item) => {
        if (item.selected) next.add(item.id);
      });
    });
    setSelected(next);
  }, [result]);

  // 从首页跳转过来时自动展开目标类目
  useEffect(() => {
    if (!focusCategory || !result) return;
    setExpanded((prev) => new Set(prev).add(focusCategory));
    const el = document.getElementById(`cat-${focusCategory}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    clearFocusCategory();
  }, [focusCategory, result, clearFocusCategory]);

  // ---------------------------------------------------------------- 动作
  const startScan = async () => {
    try {
      setStatus('running');
      setSelected(new Set());
      await api.scanStart(null, true);
      schedulePoll(600);
    } catch (err) {
      if (err.code === 1003) {
        setStatus('running');
        schedulePoll(600);
        notify('扫描正在进行中，已为你切换到进度视图');
        return;
      }
      setStatus('idle');
      notify(err.message, 'error');
    }
  };

  const cancelScan = async () => {
    try {
      await api.scanCancel();
      notify('已请求停止扫描');
      setTimeout(() => pollOnce(), 800);
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  // ---------------------------------------------------------------- 派生数据
  const categories = useMemo(() => {
    const list = (result?.categories || []).slice();
    const keyword = search.trim().toLowerCase();
    let filtered = list;
    if (group !== 'all') filtered = filtered.filter((c) => c.group === group);
    if (keyword) {
      filtered = filtered.filter((c) => {
        if (c.name.toLowerCase().includes(keyword)) return true;
        if (c.desc.toLowerCase().includes(keyword)) return true;
        return c.items.some((i) =>
          (i.display || '').toLowerCase().includes(keyword) ||
          (i.path || '').toLowerCase().includes(keyword));
      });
    }
    const compare = {
      size: (a, b) => b.size - a.size,
      name: (a, b) => a.name.localeCompare(b.name, 'zh-CN'),
      risk: (a, b) => {
        const weight = { high: 3, medium: 2, low: 1 };
        return (weight[b.risk] || 0) - (weight[a.risk] || 0);
      },
    }[sort] || ((a, b) => b.size - a.size);
    return filtered.slice().sort(compare);
  }, [result, group, search, sort]);

  const selectedItems = useMemo(() => {
    const items = [];
    (result?.categories || []).forEach((cat) => {
      cat.items.forEach((item) => {
        if (selected.has(item.id)) items.push(item);
      });
    });
    return items;
  }, [result, selected]);

  const selectedBytes = selectedItems.reduce((sum, i) => sum + (i.size || 0), 0);
  const running = status === 'running';

  // ---------------------------------------------------------------- 勾选控制
  const toggleItem = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleCategory = (cat, value) => {
    setSelected((prev) => {
      const next = new Set(prev);
      cat.items.forEach((item) => {
        if (value) next.add(item.id); else next.delete(item.id);
      });
      return next;
    });
  };

  const selectSafe = () => {
    setSelected(() => {
      const next = new Set();
      (result?.categories || []).forEach((cat) => {
        if (cat.risk === 'high' || cat.needs_confirm_text) return;
        cat.items.forEach((item) => next.add(item.id));
      });
      return next;
    });
    notify('已选中全部安全项（高风险类目未自动勾选）');
  };

  const clearAll = () => setSelected(new Set());

  // ---------------------------------------------------------------- 清理
  const openPreview = async () => {
    if (!selectedItems.length) return;
    try {
      const data = await api.cleanPreview(selectedItems.map((i) => i.id), mode);
      setPreview(data);
      setPreviewOpen(true);
      setConfirmOutside(false);
      setConfirmText('');
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  const runClean = async () => {
    if (!preview) return;
    setCleaning(true);
    setPreviewOpen(false);
    setJobOpen(true);
    setJob({ status: 'running', processed: 0, total: preview.summary.planned_items,
      freed_bytes: 0, success: 0, skipped: 0, failed: 0, current_path: null,
      results: [] });
    try {
      await api.cleanExecute({
        previewToken: preview.preview_token,
        mode: preview.mode,
        confirmOutsideHome: confirmOutside,
        confirmText: confirmText,
      });
      const final = await api.pollUntil(
        () => api.cleanProgress(),
        (data) => data && data.status !== 'running',
        { interval: 900, timeout: 30 * 60 * 1000 },
      );
      setJob(final);
      notify(
        `清理完成：释放 ${formatBytes(final.freed_bytes || 0)}`,
        'success',
      );
      refresh();
      // 清理后原结果已失效，自动重新扫描一次
      setTimeout(() => startScan(), 400);
    } catch (err) {
      notify(err.message, 'error');
      setJob((prev) => prev && { ...prev, status: 'error' });
    } finally {
      setCleaning(false);
    }
  };

  const cancelClean = async () => {
    try {
      await api.cleanCancel();
      notify('已请求停止清理，当前项处理完即刻停止');
    } catch (err) {
      notify(err.message, 'error');
    }
  };

  const requires = preview?.requires || {};
  const summary = preview?.summary;
  const previewReady = !!preview && (
    !requires.confirm_outside_home || confirmOutside) && (
    !requires.confirm_text || confirmText.trim() === requires.confirm_text);

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      {/* G 区 扫描状态条 */}
      <Panel sx={{ py: 2 }}>
        {running ? (
          <Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              <Box sx={{ flex: 1 }}>
                <Typography variant="body2" sx={{ mb: 0.5, fontWeight: 600 }}>
                  正在扫描… 已发现 {formatBytes(progress?.found_bytes || 0)}
                </Typography>
                <LinearProgress sx={{ height: 6, borderRadius: 4 }} />
                <Typography variant="caption" color="text.secondary" sx={{ mt: 0.75, display: 'block' }}>
                  已扫描 {(progress?.scanned_dirs || 0).toLocaleString()} 个目录 ·
                  {' '}{(progress?.scanned_entries || 0).toLocaleString()} 个文件
                  {progress?.current_category_name ? ` · 当前：${progress.current_category_name}` : ''}
                </Typography>
              </Box>
              <Button size="small" color="error" variant="outlined"
                startIcon={<StopIcon fontSize="small" />} onClick={cancelScan}>
                停止扫描
              </Button>
            </Box>
          </Box>
        ) : (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
            <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>
              {result
                ? `扫描${status === 'aborted' ? '已中止' : '完成'} · 用时 ${Math.round(
                  (result.elapsed_ms || 0) / 1000)} 秒 · ${result.categories.length} 个类目 · 可回收 ${formatBytes(
                  result.totals?.reclaimable_bytes || 0)}`
                : '还没有开始扫描'}
            </Typography>
            <Button size="small" variant="contained" disableElevation
              onClick={() => startScan()} disabled={running}>
              {result ? '重新扫描' : '开始扫描'}
            </Button>
          </Box>
        )}
      </Panel>

      {/* H 区 汇总操作栏 */}
      <Panel sx={{
        py: 2, position: 'sticky', top: 72, zIndex: 5,
        backdropFilter: 'blur(6px)',
      }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          <Typography variant="body2" sx={{ flex: 1, fontWeight: 600 }}>
            已选 <span style={{ color: COLORS.brand }}>{selectedItems.length}</span> 项 ·
            {' '}预计释放 <span style={{ color: COLORS.success }}>{formatBytes(selectedBytes)}</span>
          </Typography>
          <Button size="small" onClick={selectSafe} disabled={running}>全选安全项</Button>
          <Button size="small" onClick={clearAll} disabled={running}>取消全选</Button>
          <Button
            size="small" color="error" variant="contained" disableElevation
            disabled={running || !selectedItems.length}
            startIcon={<DeleteSweepIcon fontSize="small" />}
            onClick={openPreview}
          >
            清理选中项
          </Button>
        </Box>
      </Panel>

      {/* I 区 筛选器 */}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        {GROUP_FILTERS.map((f) => (
          <Chip key={f.key} label={f.label} size="small"
            color={group === f.key ? 'primary' : 'default'}
            variant={group === f.key ? 'filled' : 'outlined'}
            onClick={() => setGroup(f.key)} />
        ))}
        <Box sx={{ flex: 1 }} />
        <TextField size="small" select value={sort} onChange={(e) => setSort(e.target.value)}
          sx={{ width: 128 }} label="排序">
          <MenuItem value="size">按体积</MenuItem>
          <MenuItem value="name">按名称</MenuItem>
          <MenuItem value="risk">按风险</MenuItem>
        </TextField>
        <TextField size="small" label="搜索类目或路径" value={search}
          onChange={(e) => setSearch(e.target.value)} sx={{ width: 220 }} />
      </Box>

      {/* J 区 类目手风琴 */}
      {!result && running ? (
        <Panel><Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
          <CircularProgress size={18} />
          <Typography variant="body2" color="text.secondary">正在扫描…</Typography>
        </Box></Panel>
      ) : null}

      {categories.length === 0 && result ? (
        <Panel>
          <EmptyState>
            <EmptyStateIcon>🔍</EmptyStateIcon>
            <EmptyStateTitle>没有符合条件的类目</EmptyStateTitle>
            <EmptyStateHint>换个筛选条件，或点「重新扫描」刷新结果。</EmptyStateHint>
          </EmptyState>
        </Panel>
      ) : null}

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.25 }}>
        {categories.map((cat) => {
          const catSelected = cat.items.length > 0 &&
            cat.items.every((i) => selected.has(i.id));
          const partial = cat.items.some((i) => selected.has(i.id)) && !catSelected;
          return (
            <Accordion
              key={cat.category_id}
              id={`cat-${cat.category_id}`}
              expanded={expanded.has(cat.category_id)}
              onChange={() => setExpanded((prev) => {
                const next = new Set(prev);
                if (next.has(cat.category_id)) next.delete(cat.category_id);
                else next.add(cat.category_id);
                return next;
              })}
              disableGutters
              sx={{
                borderRadius: '14px !important', overflow: 'hidden',
                border: '1px solid', borderColor: 'divider',
                '&:before': { display: 'none' },
              }}
            >
              <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ px: 2 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, width: '100%', flexWrap: 'wrap' }}>
                  <Checkbox
                    size="small"
                    checked={catSelected}
                    indeterminate={partial}
                    disabled={running || cat.items.length === 0}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => toggleCategory(cat, e.target.checked)}
                  />
                  <Typography sx={{ fontWeight: 600, minWidth: 120 }}>{cat.name}</Typography>
                  <RiskBadge $risk={cat.risk}>
                    {RISK_META[cat.risk]?.emoji} {RISK_META[cat.risk]?.label}
                  </RiskBadge>
                  {cat.privacy_sensitive ? (
                    <Chip size="small" label="隐私敏感" variant="outlined" />
                  ) : null}
                  <SizeText sx={{ minWidth: 76 }}>{formatBytes(cat.size)}</SizeText>
                  <Box sx={{ width: 120, display: { xs: 'none', sm: 'block' } }}>
                    <LinearProgress variant="determinate"
                      value={Math.min(Math.max(cat.percent || 0, 0), 100)}
                      sx={{ height: 6, borderRadius: 4 }} />
                  </Box>
                  <Typography variant="caption" color="text.secondary">
                    {cat.percent?.toFixed ? cat.percent.toFixed(1) : cat.percent}%
                  </Typography>
                  <Box sx={{ flex: 1 }} />
                  <Typography variant="caption" color="text.secondary">
                    {cat.item_count} 项
                  </Typography>
                  {cat.status !== 'ok' ? (
                    <Chip size="small" color="warning" variant="outlined"
                      label={cat.status === 'not_found' ? '未检测到' : '部分无权限'} />
                  ) : null}
                </Box>
              </AccordionSummary>
              <AccordionDetails sx={{ px: 2, pt: 0 }}>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
                  {cat.desc}
                  {cat.note ? `（${cat.note}）` : ''}
                </Typography>

                {(cat.warnings || []).map((w, idx) => (
                  <NoticeBar key={`${w.code}-${idx}`} $tone="warning" sx={{ mb: 1.5 }}>
                    ⚠️ {w.message}
                    {w.guide ? <span style={{ opacity: 0.8 }}> · {w.guide}</span> : null}
                  </NoticeBar>
                ))}

                {cat.items.length === 0 ? (
                  <Typography variant="body2" color="text.secondary" sx={{ py: 1.5 }}>
                    没有可清理的条目
                    {cat.status === 'not_found' ? '（未检测到该类目，可能尚未安装）' : ''}。
                  </Typography>
                ) : (
                  <>
                    {cat.items.slice(0, MAX_ROWS).map((item) => {
                      const checked = selected.has(item.id);
                      const pathVisible = showPaths.has(item.id);
                      return (
                        <Box key={item.id} sx={{
                          display: 'flex', alignItems: 'center', gap: 1.5,
                          py: 0.85, borderTop: '1px dashed', borderColor: 'divider',
                        }}>
                          <Checkbox size="small" checked={checked} disabled={running}
                            onChange={() => toggleItem(item.id)} />
                          <Box sx={{ flex: 1, minWidth: 0 }}>
                            <Typography variant="body2" noWrap sx={{ fontWeight: 500 }}>
                              {item.display}
                              {item.flags?.includes('privacy') ? ' 🔒' : ''}
                              {item.flags?.includes('outside_home') ? ' · 主目录外' : ''}
                            </Typography>
                            {pathVisible ? (
                              <PathText>{shortPath(item.path)}</PathText>
                            ) : null}
                          </Box>
                          <Typography variant="caption" color="text.secondary">
                            {formatDateTime(new Date(item.mtime * 1000).toISOString()).slice(0, 10)}
                          </Typography>
                          <SizeText sx={{ minWidth: 72, textAlign: 'right' }}>
                            {formatBytes(item.size)}
                          </SizeText>
                          <Button size="small" sx={{ minWidth: 0, textTransform: 'none' }}
                            onClick={() => setShowPaths((prev) => {
                              const next = new Set(prev);
                              if (next.has(item.id)) next.delete(item.id); else next.add(item.id);
                              return next;
                            })}>
                            {pathVisible ? '收起路径' : '查看路径'}
                          </Button>
                        </Box>
                      );
                    })}
                    {cat.items.length > MAX_ROWS ? (
                      <Typography variant="caption" color="text.secondary">
                        还有 {cat.items.length - MAX_ROWS} 项未显示（已按体积排序）
                      </Typography>
                    ) : null}
                  </>
                )}
              </AccordionDetails>
            </Accordion>
          );
        })}
      </Box>

      {/* L 区 底部安全说明 */}
      <NoticeBar $tone="success">
        🛡️ 删除前会再次向你确认；
        {mode === 'trash'
          ? '开启「移到废纸篓」后可随时从废纸篓恢复（需清空废纸篓才真正释放空间）。'
          : '当前为「直接删除」模式，删除后不可恢复，请谨慎操作。'}
      </NoticeBar>

      {/* 弹窗一：清理确认 */}
      <Dialog open={previewOpen} onClose={() => setPreviewOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 700, flex: 1 }}>
            ⚠️ 确认清理这 {summary?.planned_items || 0} 项吗？
          </Typography>
          <IconButton size="small" onClick={() => setPreviewOpen(false)}>✕</IconButton>
        </DialogTitle>
        <DialogContent dividers>
          {/* N 摘要区 */}
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 1.5, mb: 2 }}>
            <StatBlock label="清理项数" value={summary?.planned_items || 0} />
            <StatBlock label="预计释放" value={formatBytes(summary?.planned_bytes || 0)}
              color={COLORS.success} />
            <StatBlock label="涉及类目" value={summary?.category_count || 0} />
          </Box>

          {/* O 分区 */}
          <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5 }}>
            <ZoneCard tone="success" title="主目录内（安全）"
              count={summary?.in_home?.items || 0} bytes={summary?.in_home?.bytes || 0} />
            <ZoneCard tone="warning" title="主目录外（需谨慎）"
              count={summary?.outside_home?.items || 0} bytes={summary?.outside_home?.bytes || 0} />
          </Box>

          {/* P Top5 路径 */}
          <SectionTitle sx={{ mt: 2.5 }}>体积最大的几项</SectionTitle>
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {(summary?.top_paths || preview?.top_paths || []).slice(0, 5).map((p, idx) => (
              <Box key={idx} sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
                <PathText>{p.path}</PathText>
                <SizeText>{formatBytes(p.size)}</SizeText>
              </Box>
            ))}
          </Box>

          {summary?.blocked?.length ? (
            <NoticeBar $tone="warning" sx={{ mt: 2 }}>
              ℹ️ 有 {summary.blocked.length} 项被安全策略自动拦截，不会被执行。
            </NoticeBar>
          ) : null}

          {/* Q 可恢复性 */}
          <NoticeBar $tone={preview?.mode === 'trash' ? 'info' : 'danger'} sx={{ mt: 2 }}>
            {preview?.mode === 'trash'
              ? '♻️ 当前为「移到废纸篓」模式，清理后可从废纸篓恢复（需清空废纸篓才真正释放磁盘空间）。'
              : '⚠️ 当前为「直接删除」模式，删除后不可恢复。'}
          </NoticeBar>

          {/* R 额外确认 */}
          {requires.confirm_outside_home || requires.confirm_text ? (
            <Box sx={{ mt: 2 }}>
              {requires.confirm_outside_home ? (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.5 }}>
                  <Checkbox size="small" checked={confirmOutside}
                    onChange={(e) => setConfirmOutside(e.target.checked)} />
                  <Typography variant="body2">
                    我确认清理位于主目录之外 / 高风险的项目
                  </Typography>
                </Box>
              ) : null}
              {requires.confirm_text ? (
                <Box sx={{ mt: 1.5 }}>
                  <Typography variant="caption" color="text.secondary">
                    含高危类目，请输入「{requires.confirm_text}」以继续：
                  </Typography>
                  <TextField size="small" fullWidth value={confirmText}
                    placeholder={requires.confirm_text}
                    onChange={(e) => setConfirmText(e.target.value)} sx={{ mt: 0.5 }} />
                </Box>
              ) : null}
            </Box>
          ) : null}
        </DialogContent>
        <DialogActions sx={{ px: 2.5, py: 1.75, gap: 1 }}>
          <Button size="small" variant="outlined"
            onClick={() => api.downloadPreviewCsv(preview.preview_token)}>
            导出清单 CSV
          </Button>
          <Box sx={{ flex: 1 }} />
          <Button size="small" onClick={() => setPreviewOpen(false)}>取消</Button>
          <Tooltip title={previewReady ? '' : '请先完成上面的确认'}>
            <span>
              <Button size="small" color="error" variant="contained" disableElevation
                disabled={!previewReady} onClick={runClean}>
                确认清理
              </Button>
            </span>
          </Tooltip>
        </DialogActions>
      </Dialog>

      {/* 弹窗二：清理进度 / 结果汇总 */}
      <Dialog open={jobOpen} onClose={() => !cleaning && setJobOpen(false)}
        maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontWeight: 700 }}>
          {job?.status === 'running' ? '正在清理…' : '清理完成'}
        </DialogTitle>
        <DialogContent dividers>
          {job?.status === 'running' ? (
            <Box>
              <LinearProgress
                variant="determinate"
                value={job.total ? (job.processed / job.total) * 100 : 0}
                sx={{ height: 8, borderRadius: 5, mb: 1.5 }} />
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                正在清理（{job.processed}/{job.total}）
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                当前：{job.current_path ? shortPath(job.current_path) : '准备中…'}
              </Typography>
              <Typography variant="body2" sx={{ mt: 1, color: COLORS.success, fontWeight: 700 }}>
                已释放：{formatBytes(job.freed_bytes || 0)}
              </Typography>
            </Box>
          ) : (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
              <Typography variant="body1" sx={{ fontWeight: 700 }}>
                ✅ 成功 {job?.success || 0} 项 · 释放 {formatBytes(job?.freed_bytes || 0)}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                ⏭️ 跳过 {job?.skipped || 0} 项 · ❌ 失败 {job?.failed || 0} 项
                {' · '}状态：<StatusBadge $status={job?.status}>{
                  RESULT_META[job?.status]?.label || '完成'}</StatusBadge>
              </Typography>
              {(job?.results || []).filter((r) => r.status !== 'success').length ? (
                <Box>
                  <SectionTitle>未成功的项目</SectionTitle>
                  <Box sx={{ maxHeight: 220, overflowY: 'auto' }}>
                    {(job.results || []).filter((r) => r.status !== 'success')
                      .slice(0, 50).map((r, idx) => (
                        <Box key={idx} sx={{
                          display: 'flex', gap: 1.5, py: 0.6,
                          borderBottom: '1px dashed', borderColor: 'divider',
                        }}>
                          <StatusBadge $status={r.status}>
                            {RESULT_META[r.status]?.label || r.status}
                          </StatusBadge>
                          <Box sx={{ flex: 1, minWidth: 0 }}>
                            <Typography variant="body2" noWrap>{r.reason_text || '—'}</Typography>
                            <PathText>{shortPath(r.path)}</PathText>
                          </Box>
                          <SizeText>{formatBytes(r.size)}</SizeText>
                        </Box>
                      ))}
                  </Box>
                </Box>
              ) : null}
            </Box>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 2.5, py: 1.75, gap: 1 }}>
          {job?.status === 'running' ? (
            <Button size="small" color="error" variant="outlined"
              startIcon={<StopIcon fontSize="small" />} onClick={cancelClean}>
              停止清理
            </Button>
          ) : (
            <>
              <Button size="small" onClick={() => api.health().then(() => window.scrollTo(0, 0))}>
                查看历史记录
              </Button>
              <Button size="small" variant="contained" disableElevation
                onClick={() => setJobOpen(false)}>完成</Button>
            </>
          )}
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function StatBlock({ label, value, color }) {
  return (
    <Box sx={{
      border: '1px solid', borderColor: 'divider', borderRadius: 2, p: 1.5,
      textAlign: 'center',
    }}>
      <Typography sx={{ fontSize: 22, fontWeight: 700, color: color || 'text.primary' }}>
        {value}
      </Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  );
}

function ZoneCard({ tone, title, count, bytes }) {
  const color = tone === 'success' ? COLORS.success : COLORS.warning;
  return (
    <Box sx={{
      border: '1px solid', borderColor: `${color}55`, background: `${color}12`,
      borderRadius: 2, p: 1.5,
    }}>
      <Typography variant="caption" sx={{ color, fontWeight: 700 }}>{title}</Typography>
      <Typography sx={{ fontWeight: 700, mt: 0.5 }}>
        {count} 项 · {formatBytes(bytes)}
      </Typography>
    </Box>
  );
}
