# -*- coding: utf-8 -*-
"""HTTP 服务封装。

只做三件事：
  1. 用 :class:`ThreadingHTTPServer` 保证扫描/清理期间 UI 轮询不被阻塞；
  2. 强制绑定 ``127.0.0.1``（SYS-04，绝不对外暴露）；
  3. 提供优雅退出，便于 Ctrl+C 时释放 SQLite 连接。
"""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Optional

from . import config
from .api import AppContext, build_handler


class CleanServer:
    """HTTP 服务门面。

    Args:
        ctx: 全局上下文（领域层单例）。
        host: 监听地址，固定为 ``127.0.0.1``。
        port: 监听端口。
    """

    def __init__(self, ctx: AppContext,
                 host: str = config.HOST,
                 port: int = config.DEFAULT_PORT) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("出于安全考虑，仅允许绑定本机回环地址")
        self.host: str = host
        self.port: int = int(port)
        self.ctx: AppContext = ctx
        self.ctx.port = self.port
        self._httpd: Optional[ThreadingHTTPServer] = None

    # ---------------------------------------------------------------- 工厂
    def create(self, port: Optional[int] = None) -> ThreadingHTTPServer:
        """创建（并绑定）HTTP 服务实例。

        Args:
            port: 端口；为空时使用构造时的端口。

        Returns:
            已绑定的 :class:`ThreadingHTTPServer`。

        Raises:
            OSError: 端口被占用时向上抛出，交由启动器重试。
        """
        actual_port = self.port if port is None else int(port)
        handler_cls = build_handler(self.ctx)
        ThreadingHTTPServer.allow_reuse_address = False
        self._httpd = ThreadingHTTPServer(
            (self.host, actual_port), handler_cls)
        self._httpd.daemon_threads = True
        self.port = actual_port
        self.ctx.port = actual_port
        return self._httpd

    # ---------------------------------------------------------------- 运行
    def serve_forever(self) -> None:
        """进入请求循环（阻塞）。"""
        if self._httpd is None:
            self.create()
        assert self._httpd is not None
        try:
            self._httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self) -> None:
        """优雅退出：停止请求循环并释放资源。"""
        httpd = self._httpd
        if httpd is not None:
            try:
                threading.Thread(target=httpd.shutdown, daemon=True).start()
            except Exception:                               # pragma: no cover
                pass
        if self.ctx is not None:
            self.ctx.shutdown()

    @property
    def url(self) -> str:
        """当前访问地址。"""
        return "http://%s:%d" % (self.host, self.port)


def port_in_use(host: str, port: int) -> bool:
    """检测端口是否被占用。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.35)
            return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def create_server(ctx: AppContext, host: str = config.HOST,
                  port: int = config.DEFAULT_PORT) -> CleanServer:
    """便捷工厂：创建已装配上下文的 :class:`CleanServer`。

    Args:
        ctx: 全局上下文。
        host: 监听地址。
        port: 起始端口。

    Returns:
        已初始化的 :class:`CleanServer`（尚未绑定端口）。
    """
    return CleanServer(ctx, host=host, port=port)


def quiet_server_banner(server: ThreadingHTTPServer) -> None:
    """清理 BaseHTTPServer 的默认 stderr 输出（已通过 log_message 静音）。"""
    server.RequestHandlerClass.server_version = "mac-cleaner/%s" % config.VERSION
