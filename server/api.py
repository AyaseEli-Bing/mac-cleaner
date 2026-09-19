# -*- coding: utf-8 -*-
"""REST 路由层（Presentation 层）。

职责：路由分发、JSON 编解码、统一响应包装、静态资源托管、全局异常兜底。
**所有 handler 都不得直接操作文件系统或数据库**，一律委托给领域层
（scanner / cleaner / store / registry）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Match, Optional, Tuple

from . import config
from .categories import CategoryRegistry, REGISTRY
from .cleaner import Cleaner
from .scanner import Scanner
from .store import Store

MAX_BODY_BYTES = 8 * 1024 * 1024


class AppContext:
    """全局单例容器：领域层对象在这里被装配并共享给所有请求线程。

    Args:
        sandbox_root: 沙箱根路径（QA 注入用），生产环境为 None。
        home_dir: 真实主目录。
        db_path: SQLite 路径，默认 ``data/mac_cleaner.db``。
    """

    def __init__(self, sandbox_root: Optional[str] = None,
                 home_dir: Optional[str] = None,
                 db_path: Optional[str] = None) -> None:
        self.sandbox_root: Optional[str] = (
            os.path.normpath(sandbox_root) if sandbox_root else None)
        self.home: str = os.path.normpath(home_dir or config.HOME)
        self.store: Store = Store(db_path=db_path)
        self.registry: CategoryRegistry = REGISTRY
        settings = self.store.get_settings()
        self.scanner: Scanner = Scanner(
            sandbox_root=self.sandbox_root, home_dir=self.home,
            concurrency=int((settings.get("scan") or {}).get(
                "concurrency", config.SCAN_DEFAULT_CONCURRENCY)),
            timeout_sec=int((settings.get("scan") or {}).get(
                "timeout_sec", config.SCAN_TIMEOUT_SEC)),
            registry=self.registry)
        self.scanner.set_settings(settings)
        self.cleaner: Cleaner = Cleaner(
            store=self.store, settings=settings, registry=self.registry,
            sandbox_root=self.sandbox_root, home_dir=self.home)
        self.port: int = config.DEFAULT_PORT
        self.store.log("info", "服务上下文初始化完成（端口 %d）" % self.port)

    # ---------------------------------------------------------------- 便捷
    def settings(self) -> Dict[str, Any]:
        """读取最新设置（每次都从库里读，保证跨请求一致）。"""
        return self.store.get_settings()

    def refresh(self) -> None:
        """设置变更后同步到扫描器与清理器。"""
        settings = self.store.get_settings()
        self.scanner.set_settings(settings)
        self.cleaner.set_settings(settings)

    def shutdown(self) -> None:
        """释放资源（进程退出前调用）。"""
        try:
            self.store.close()
        except Exception:                                   # pragma: no cover
            pass


# ------------------------------------------------------------------ 路由表
# (HTTP 方法, 正则, handler 方法名)
ROUTES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("GET", re.compile(r"^/api/disk/overview$"), "disk_overview"),
    ("GET", re.compile(r"^/api/categories$"), "categories"),
    ("POST", re.compile(r"^/api/scan/start$"), "scan_start"),
    ("GET", re.compile(r"^/api/scan/progress$"), "scan_progress"),
    ("GET", re.compile(r"^/api/scan/result$"), "scan_result"),
    ("POST", re.compile(r"^/api/scan/cancel$"), "scan_cancel"),
    ("POST", re.compile(r"^/api/clean/preview$"), "clean_preview"),
    ("GET", re.compile(r"^/api/clean/preview/([^/]+)/csv$"), "preview_csv"),
    ("POST", re.compile(r"^/api/clean/execute$"), "clean_execute"),
    ("GET", re.compile(r"^/api/clean/progress$"), "clean_progress"),
    ("POST", re.compile(r"^/api/clean/cancel$"), "clean_cancel"),
    ("GET", re.compile(r"^/api/history$"), "history_list"),
    ("GET", re.compile(r"^/api/history/(\d+)$"), "history_detail"),
    ("DELETE", re.compile(r"^/api/history$"), "history_clear"),
    ("GET", re.compile(r"^/api/settings$"), "settings_get"),
    ("PUT", re.compile(r"^/api/settings$"), "settings_put"),
    ("GET", re.compile(r"^/api/logs$"), "logs"),
]


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器（由 :func:`build_handler` 绑定上下文后使用）。"""

    server_version = "mac-cleaner/1.0"
    protocol_version = "HTTP/1.1"
    ctx: AppContext = None                                  # 由工厂注入

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # True 表示该请求已自行写出响应（如 CSV 附件），外层不再追加 JSON
        self._response_sent: bool = False
        super().__init__(*args, **kwargs)

    # ---------------------------------------------------------------- 入口
    def log_message(self, fmt: str, *args: Any) -> None:
        """静音默认请求日志（全部收敛到 app.log）。"""
        return

    def do_GET(self) -> None:                               # noqa: N802
        self._process("GET")

    def do_HEAD(self) -> None:                              # noqa: N802
        self._process("HEAD")

    def do_POST(self) -> None:                              # noqa: N802
        self._process("POST")

    def do_PUT(self) -> None:                               # noqa: N802
        self._process("PUT")

    def do_DELETE(self) -> None:                            # noqa: N802
        self._process("DELETE")

    def _process(self, method: str) -> None:
        """统一入口：解析请求 → 分发 → 写响应，全程兜底异常。"""
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query or "")
        body: Dict[str, Any] = {}
        if method in ("POST", "PUT", "DELETE"):
            body = self._read_body()
        try:
            if path.startswith(config.API_PREFIX):
                data, message, code = self.handle_dispatch(method, path, body,
                                                           query)
                self.json_response(code, message, data)
            elif method in ("GET", "HEAD"):
                self.serve_static(path)
            else:
                self.json_response(config.ErrorCode.BAD_REQUEST,
                                   "不支持的请求方法", None)
        except config.ApiError as exc:
            self.json_response(exc.code, exc.message, exc.data)
        except Exception:                                   # ERR-05：绝不外抛堆栈
            detail = traceback.format_exc()
            if self.ctx is not None:
                self.ctx.store.log("error", "处理 %s %s 时发生内部错误：\n%s"
                                   % (method, path, detail))
            self.json_response(config.ErrorCode.INTERNAL_ERROR,
                               config.ERROR_MESSAGE[
                                   config.ErrorCode.INTERNAL_ERROR], None)

    # ---------------------------------------------------------------- 请求
    def _read_body(self) -> Dict[str, Any]:
        """读取并解析 JSON 请求体；非法 JSON 返回空字典。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise config.ApiError(config.ErrorCode.BAD_REQUEST, "请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise config.ApiError(config.ErrorCode.BAD_REQUEST,
                                  "请求体不是合法的 JSON")
        if not isinstance(parsed, dict):
            raise config.ApiError(config.ErrorCode.BAD_REQUEST,
                                  "请求体必须是 JSON 对象")
        return parsed

    # ---------------------------------------------------------------- 分发
    def handle_dispatch(self, method: str, path: str,
                        body: Dict[str, Any],
                        query: Dict[str, List[str]]) -> Tuple[Any, str, int]:
        """路由分发到具体 handler。

        Returns:
            ``(data, message, code)`` 三元组。

        Raises:
            config.ApiError: 404 资源不存在。
        """
        for route_method, pattern, handler_name in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match is None:
                continue
            handler: Optional[Callable[..., Any]] = getattr(
                self, "api_" + handler_name, None)
            if handler is None:                             # pragma: no cover
                raise config.ApiError(config.ErrorCode.NOT_FOUND,
                                      "接口不存在：%s" % path)
            data = handler(match, body, query)
            return data, "ok", config.ErrorCode.OK
        raise config.ApiError(config.ErrorCode.NOT_FOUND,
                              "接口不存在：%s %s" % (method, path))

    # ---------------------------------------------------------------- 响应
    def json_response(self, code: int, message: str,
                      data: Any, http_status: int = 200) -> None:
        """写统一格式 JSON 响应 ``{code, message, data}``。"""
        payload = {"code": int(code), "message": message or "", "data": data}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_static(self, path: str) -> None:
        """托管 ``static/`` 目录下的构建产物，未知路径回退 index.html。"""
        rel = (path or "/").lstrip("/")
        if not rel:
            rel = "index.html"
        target = (config.STATIC_DIR / rel).resolve()
        try:
            if not str(target).startswith(str(config.STATIC_DIR.resolve())):
                raise OSError("路径越界")
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                target = config.STATIC_DIR / "index.html"
            if not target.exists():
                raise OSError("缺少静态资源")
            raw = target.read_bytes()
        except OSError:
            self._serve_placeholder()
            return
        self._response_sent = True
        ctype, _ = mimetypes.guess_type(str(target))
        if ctype is None:
            ctype = "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_placeholder(self) -> None:
        """构建产物缺失时的占位页（提示先执行前端构建）。"""
        html = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>%s</title><style>body{background:#121212;color:#E6E6E6;"
            "font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',"
            "sans-serif;display:flex;align-items:center;justify-content:"
            "center;height:100vh;margin:0}div{max-width:520px;line-height:1.8;"
            "padding:24px;border-radius:12px;background:#1E1E1E}code{color:"
            "#4C8DF6}</style></head><body><div><h2>%s</h2>"
            "<p>前端页面还没有构建完成。</p>"
            "<p>请在项目根目录执行：</p><p><code>cd web &amp;&amp; npm install "
            "&amp;&amp; npm run build</code></p>"
            "<p>然后重新启动：<code>python3 start.py</code></p></div></body>"
            "</html>" % (config.APP_NAME, config.APP_NAME)
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        try:
            self.wfile.write(html)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ================================================================ API impl
    # 1 健康检查
    def api_health(self, match: Match[str], body: Dict[str, Any],
                   query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        return {
            "app": config.APP_NAME,
            "version": config.VERSION,
            "port": ctx.port,
            "host": config.HOST,
            "home": ctx.home,
            "sandbox_root": ctx.sandbox_root,
            "data_dir": str(config.DATA_DIR),
            "db_path": str(config.DB_PATH),
            "time": config.now_iso(),
        }

    # 2 磁盘概览
    def api_disk_overview(self, match: Match[str], body: Dict[str, Any],
                          query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        usage = config.disk_usage_within(ctx.scanner.home)
        stats = ctx.store.stats()
        reclaimable = 0
        try:
            reclaimable = int(
                ctx.scanner.result()["totals"]["reclaimable_bytes"])
        except Exception:
            reclaimable = 0
        usage.update({
            "reclaimable_bytes": reclaimable,
            "reclaimable_text": config.format_bytes(reclaimable),
            "cumulative_freed_bytes": int(
                stats.get("cumulative_freed_bytes", 0)),
            "clean_count": int(stats.get("clean_count", 0)),
            "first_used_at": stats.get("first_used_at"),
            "last_used_at": stats.get("last_used_at"),
        })
        return usage

    # 3 类目清单
    def api_categories(self, match: Match[str], body: Dict[str, Any],
                       query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        settings = ctx.settings()
        return {"categories": ctx.registry.describe(
            settings, ctx.home, ctx.sandbox_root)}

    # 4 启动扫描
    def api_scan_start(self, match: Match[str], body: Dict[str, Any],
                       query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        ids = body.get("category_ids")
        if ids is not None and not isinstance(ids, list):
            raise config.ApiError(config.ErrorCode.BAD_REQUEST,
                                  "category_ids 必须是字符串数组")
        settings = ctx.settings()
        scan_id = ctx.scanner.start(ids, settings)
        ctx.store.log("info", "启动扫描：%s" % scan_id)
        return {"scan_id": scan_id, "started_at": config.now_iso()}

    # 5 扫描进度
    def api_scan_progress(self, match: Match[str], body: Dict[str, Any],
                          query: Dict[str, List[str]]) -> Dict[str, Any]:
        return self.ctx.scanner.progress()

    # 6 扫描结果
    def api_scan_result(self, match: Match[str], body: Dict[str, Any],
                        query: Dict[str, List[str]]) -> Dict[str, Any]:
        return self.ctx.scanner.result()

    # 7 取消扫描
    def api_scan_cancel(self, match: Match[str], body: Dict[str, Any],
                        query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        cancelled = ctx.scanner.cancel()
        ctx.store.log("info", "请求停止扫描（生效：%s）" % cancelled)
        return {"status": config.JOB_ABORTED if cancelled else
                ctx.scanner.progress()["status"],
                "cancelled": cancelled}

    # 8 清理预览（dry-run）
    def api_clean_preview(self, match: Match[str], body: Dict[str, Any],
                          query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        ids = body.get("items")
        if not isinstance(ids, list) or not ids:
            raise config.ApiError(config.ErrorCode.BAD_REQUEST,
                                  "请先勾选要清理的项目")
        mode = body.get("mode") or ctx.settings().get("mode")
        items, missing = ctx.scanner.resolve_items([str(i) for i in ids])
        if missing:
            ctx.store.log("warning", "预览时 %d 个条目已失效，已忽略"
                          % len(missing))
        if not items:
            raise config.ApiError(
                config.ErrorCode.NOT_FOUND,
                "所选项目已失效，请重新扫描后再试")
        return ctx.cleaner.preview(items, mode)

    # 9 预览清单 CSV
    def api_preview_csv(self, match: Match[str], body: Dict[str, Any],
                        query: Dict[str, List[str]]) -> Any:
        token = match.group(1)
        preview = self.ctx.cleaner.get_preview(token)
        if preview is None:
            raise config.ApiError(config.ErrorCode.INVALID_PREVIEW_TOKEN)
        rows = ["路径,大小(字节),大小,类目,操作时间"]
        stamp = config.now_iso()
        for item in preview.items:
            path = str(item.get("path") or "")
            size = int(item.get("size", 0) or 0)
            rows.append('"%s",%d,%s,%s,%s' % (
                path.replace('"', '""'), size,
                config.format_bytes(size),
                str(item.get("category_id") or ""), stamp))
        text = "\n".join(rows)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="clean-preview-%s.csv"' % token)
        raw = ("﻿" + text).encode("utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    # 10 执行清理
    def api_clean_execute(self, match: Match[str], body: Dict[str, Any],
                          query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        token = str(body.get("preview_token") or "")
        if not token:
            raise config.ApiError(config.ErrorCode.BAD_REQUEST,
                                  "缺少 preview_token")
        mode = body.get("mode") or ctx.settings().get("mode")
        job_id = ctx.cleaner.execute(
            token, mode,
            bool(body.get("confirm_outside_home")),
            body.get("confirm_text"))
        ctx.store.log("info", "启动清理任务：%s（模式 %s）" % (job_id, mode))
        return {"job_id": job_id, "started_at": config.now_iso()}

    # 11 清理进度
    def api_clean_progress(self, match: Match[str], body: Dict[str, Any],
                           query: Dict[str, List[str]]) -> Dict[str, Any]:
        return self.ctx.cleaner.progress()

    # 12 取消清理
    def api_clean_cancel(self, match: Match[str], body: Dict[str, Any],
                         query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        cancelled = ctx.cleaner.cancel()
        return {"status": config.JOB_ABORTED if cancelled else
                ctx.cleaner.progress()["status"], "cancelled": cancelled}

    # 13 历史列表
    def api_history_list(self, match: Match[str], body: Dict[str, Any],
                         query: Dict[str, List[str]]) -> Dict[str, Any]:
        limit = _query_int(query, "limit", 50)
        offset = _query_int(query, "offset", 0)
        return self.ctx.store.list_history(limit, offset)

    # 14 历史明细
    def api_history_detail(self, match: Match[str], body: Dict[str, Any],
                           query: Dict[str, List[str]]) -> Dict[str, Any]:
        hid = int(match.group(1))
        found = self.ctx.store.get_history(hid)
        if found is None:
            raise config.ApiError(config.ErrorCode.NOT_FOUND,
                                  "该历史记录不存在")
        return found

    # 15 清空历史
    def api_history_clear(self, match: Match[str], body: Dict[str, Any],
                          query: Dict[str, List[str]]) -> Dict[str, Any]:
        if not bool(body.get("confirm")):
            raise config.ApiError(config.ErrorCode.CONFIRM_REQUIRED,
                                  "清空历史需要先二次确认")
        cleared = self.ctx.store.clear_history()
        return {"cleared": cleared}

    # 16 读取设置
    def api_settings_get(self, match: Match[str], body: Dict[str, Any],
                         query: Dict[str, List[str]]) -> Dict[str, Any]:
        return {"settings": self.ctx.settings()}

    # 17 更新设置
    def api_settings_put(self, match: Match[str], body: Dict[str, Any],
                         query: Dict[str, List[str]]) -> Dict[str, Any]:
        ctx = self.ctx
        patch = body.get("settings")
        if not isinstance(patch, dict) or not patch:
            raise config.ApiError(config.ErrorCode.BAD_REQUEST,
                                  "缺少要更新的设置内容")
        merged = ctx.store.save_settings(patch)
        ctx.refresh()
        ctx.store.log("info", "设置已更新：%s" % ",".join(sorted(patch.keys())))
        return {"settings": merged}

    # 18 日志
    def api_logs(self, match: Match[str], body: Dict[str, Any],
                 query: Dict[str, List[str]]) -> Dict[str, Any]:
        log_type = _query_str(query, "type", "audit")
        lines = _query_int(query, "lines", 500)
        return {"type": log_type, "lines": self.ctx.store.read_logs(
            log_type, lines)}


def _query_int(query: Dict[str, List[str]], key: str, fallback: int) -> int:
    """从 query 字典提取 int，缺失或非法时回落到 fallback。"""
    values = query.get(key)
    if not values:
        return fallback
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return fallback


def _query_str(query: Dict[str, List[str]], key: str, fallback: str) -> str:
    """从 query 字典提取字符串。"""
    values = query.get(key)
    if not values:
        return fallback
    return str(values[0])


def build_handler(ctx: AppContext) -> type:
    """把 :class:`AppContext` 绑定到 :class:`ApiHandler` 并返回可用 handler 类。

    Args:
        ctx: 全局上下文。

    Returns:
        ``BaseHTTPRequestHandler`` 子类，可直接交给 ``HTTPServer``。
    """
    return type("BoundApiHandler", (ApiHandler,), {"ctx": ctx})
