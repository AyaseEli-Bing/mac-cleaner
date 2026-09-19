/** 后端 REST API 封装（18 个接口）+ 统一错误拆包 + 轮询辅助。
 *
 * 约定（架构 10.5 第 1 条）：后端统一返回 `{code, message, data}`，
 * 此处在 `code !== 0` 时抛 `Error(message)`，交由上层 Snackbar 展示。
 * 前端**永不展示 traceback**。
 */
const BASE = '/api';

async function request(method, path, body, options = {}) {
  const init = {
    method,
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  };
  if (body !== undefined && body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, { ...init, ...options });
  let payload = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      throw new Error('服务返回了无法解析的内容，请稍后重试');
    }
  }
  if (!res.ok) {
    throw new Error((payload && payload.message) || `网络异常（HTTP ${res.status}）`);
  }
  if (!payload) throw new Error('服务未返回有效数据');
  if (payload.code !== 0) {
    const error = new Error(payload.message || '操作失败');
    error.code = payload.code;
    error.data = payload.data;
    throw error;
  }
  return payload.data;
}

const get = (path, query) => {
  const qs = query
    ? '?' +
      Object.entries(query)
        .filter(([, v]) => v !== undefined && v !== null && v !== '')
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
        .join('&')
    : '';
  return request('GET', path + qs);
};

const post = (path, body) => request('POST', path, body);
const put = (path, body) => request('PUT', path, body);
const del = (path, body) => request('DELETE', path, body);

/** 轮询直到 predicate 返回 true 或超时。
 * @param {() => Promise<any>} fn 每次轮询调用的函数
 * @param {(data:any) => boolean} predicate 结束条件
 * @param {{interval?:number, timeout?:number}} options
 */
export async function pollUntil(fn, predicate, options = {}) {
  const interval = options.interval || 1000;
  const timeout = options.timeout || 10 * 60 * 1000;
  const deadline = Date.now() + timeout;
  let last = null;
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    last = await fn();
    if (predicate(last)) return last;
    if (Date.now() > deadline) throw new Error('等待超时，请稍后重试');
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}

const api = {
  // 1 健康检查
  health: () => get('/health'),

  // 2 磁盘概览
  diskOverview: () => get('/disk/overview'),

  // 3 类目清单
  categories: () => get('/categories'),

  // 4 启动扫描
  scanStart: (categoryIds, force) =>
    post('/scan/start', { category_ids: categoryIds, force: !!force }),

  // 5 扫描进度
  scanProgress: () => get('/scan/progress'),

  // 6 扫描结果
  scanResult: () => get('/scan/result'),

  // 7 取消扫描
  scanCancel: () => post('/scan/cancel'),

  // 8 清理预览（dry-run）
  cleanPreview: (itemIds, mode) => post('/clean/preview', { items: itemIds, mode }),

  // 9 预览清单 CSV 下载
  previewCsvUrl: (token) => `${BASE}/clean/preview/${encodeURIComponent(token)}/csv`,

  // 10 执行清理
  cleanExecute: ({ previewToken, mode, confirmOutsideHome, confirmText }) =>
    post('/clean/execute', {
      preview_token: previewToken,
      mode,
      confirm_outside_home: !!confirmOutsideHome,
      confirm_text: confirmText || '',
    }),

  // 11 清理进度
  cleanProgress: () => get('/clean/progress'),

  // 12 取消清理
  cleanCancel: () => post('/clean/cancel'),

  // 13 历史列表
  historyList: (limit = 50, offset = 0) => get('/history', { limit, offset }),

  // 14 历史明细
  historyDetail: (id) => get(`/history/${id}`),

  // 15 清空历史
  historyClear: () => del('/history', { confirm: true }),

  // 16 读取设置
  getSettings: () => get('/settings'),

  // 17 更新设置
  putSettings: (patch) => put('/settings', { settings: patch }),

  // 18 日志
  logs: (type = 'audit', lines = 500) => get('/logs', { type, lines }),

  // 工具：触发 CSV 下载
  downloadPreviewCsv: (token) => {
    const link = document.createElement('a');
    link.href = api.previewCsvUrl(token);
    link.download = `clean-preview-${token}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  },
};

export default api;
