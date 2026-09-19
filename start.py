# -*- coding: utf-8 -*-
"""一条命令启动入口（SYS-03 / G5）。

用法::

    python3 start.py                 # 常规启动
    python3 start.py --port 9000     # 指定起始端口
    python3 start.py --no-browser    # 不自动打开浏览器
    python3 start.py --build         # 强制重新构建前端

流程：环境自检 → 构建产物检查（缺失则自动 npm 构建）→ 创建数据目录
→ 端口探测（占用自动 +1）→ 启动服务 → 打开浏览器。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server import config                                   # noqa: E402
from server.api import AppContext                           # noqa: E402
from server.server import CleanServer, port_in_use          # noqa: E402

NPM_CANDIDATES = (
    "/Users/bing1111/.workbuddy/binaries/node/versions/22.22.2-3/bin/npm",
    "/usr/local/bin/npm",
    "/opt/homebrew/bin/npm",
    shutil.which("npm") or "",
)

NODE_BIN_DIRS = (
    "/Users/bing1111/.workbuddy/binaries/node/versions/22.22.2-3/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)


def _print(text: str = "") -> None:
    """统一终端输出（兼容管道重定向）。"""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _check_python() -> None:
    """检查 Python 版本，低于 3.8 直接退出。"""
    if sys.version_info < (3, 8):
        _print("需要 Python 3.8 及以上版本，当前为 %s" % sys.version.split()[0])
        sys.exit(1)


def _find_npm() -> str:
    """定位可用的 npm 可执行文件。"""
    for candidate in NPM_CANDIDATES:
        if candidate and os.path.isfile(candidate) \
                and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _npm_env() -> dict:
    """构造含 node bin 目录的 PATH 环境变量。"""
    env = dict(os.environ)
    existing = env.get("PATH", "")
    extra = os.pathsep.join(d for d in NODE_BIN_DIRS
                            if os.path.isdir(d) and d not in existing)
    if extra:
        env["PATH"] = extra + os.pathsep + existing
    return env


def build_frontend() -> bool:
    """构建前端产物到 ``static/``；返回是否成功（不抛异常）。"""
    npm = _find_npm()
    if not npm:
        _print("⚠️  未检测到 npm，无法自动构建前端。")
        _print("    请手动执行：cd web && npm install && npm run build")
        return False
    _print("▶ 正在构建前端产物（首次执行约需 1~2 分钟）…")
    web_dir = str(config.WEB_DIR)
    try:
        subprocess.run([npm, "install", "--no-audit", "--no-fund"],
                       cwd=web_dir, check=True, env=_npm_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                       timeout=900)
        subprocess.run([npm, "run", "build"], cwd=web_dir, check=True,
                       env=_npm_env(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.STDOUT, timeout=900)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError) as exc:
        _print("⚠️  前端构建失败：%s" % exc)
        _print("    可手动执行：cd web && npm install && npm run build")
        return False
    _print("✓ 前端构建完成")
    return True


def _ensure_frontend(force: bool = False) -> bool:
    """确认 ``static/index.html`` 存在，缺失时尝试自动构建。"""
    index_html = config.STATIC_DIR / "index.html"
    if not force and index_html.is_file():
        return True
    if force:
        _print("▶ --build 已指定，重新构建前端…")
    else:
        _print("▶ 未检测到前端构建产物，尝试自动构建…")
    return build_frontend()


def _prepare_data_dir() -> None:
    """创建数据目录与占位文件。"""
    config.ensure_dirs()
    gitkeep = config.DATA_DIR / ".gitkeep"
    if not gitkeep.exists():
        try:
            gitkeep.write_text("", encoding="utf-8")
        except OSError:
            pass


def _bind(server: CleanServer, host: str, start_port: int,
          retry: int = config.PORT_RETRY) -> bool:
    """尝试绑定端口，占用则自动 +1。

    Returns:
        是否绑定成功。
    """
    for offset in range(0, retry + 1):
        port = start_port + offset
        if port_in_use(host, port):
            _print("   端口 %d 已被占用，尝试 %d…" % (port, port + 1))
            continue
        try:
            server.create(port)
            if offset:
                _print("✓ 端口 %d 被占用，已自动切换到 %d" % (start_port, port))
            return True
        except OSError as exc:
            _print("   端口 %d 绑定失败：%s" % (port, exc.errno or exc))
            continue
    return False


def _open_browser(url: str, delay: float = 1.2) -> None:
    """延迟一小段时间后打开默认浏览器（等服务就绪）。"""
    def _runner() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:                                   # pragma: no cover
            pass
    threading.Thread(target=_runner, daemon=True).start()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="start.py", description="%s 一条命令启动脚本" % config.APP_NAME)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT,
                        help="起始端口，默认 %d" % config.DEFAULT_PORT)
    parser.add_argument("--host", default=config.HOST,
                        help="监听地址，仅允许本机回环地址")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    parser.add_argument("--build", action="store_true",
                        help="强制重新构建前端产物")
    parser.add_argument("--sandbox", default="",
                        help="沙箱根目录（仅供测试：把类目根重映射到该目录内）")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """程序主入口。

    Returns:
        进程退出码（0 正常，非 0 异常）。
    """
    args = parse_args(argv)
    _check_python()
    _print()
    _print("  🧹 %s v%s" % (config.APP_NAME, config.VERSION))
    _print("  %s" % ("-" * 46))

    _prepare_data_dir()
    _ensure_frontend(args.build)

    host = args.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        _print("⚠️  出于安全考虑仅支持本机回环地址，已回落到 127.0.0.1")
        host = config.HOST

    sandbox = args.sandbox or None
    if sandbox:
        _print("⚠️  沙箱模式已启用：所有扫描与清理仅在 %s 内进行" % sandbox)
    try:
        ctx = AppContext(sandbox_root=sandbox, home_dir=config.HOME)
    except Exception as exc:
        _print("✗ 初始化失败：%s" % exc)
        return 1

    server = CleanServer(ctx, host=host, port=args.port)
    if not _bind(server, host, args.port):
        _print("✗ 端口 %d~%d 均不可用，请释放端口后重试"
               % (args.port, args.port + config.PORT_RETRY))
        return 1

    index_ready = (config.STATIC_DIR / "index.html").is_file()
    _print()
    _print("  ✓ 服务已启动：%s" % server.url)
    _print("  ✓ 数据目录：%s" % config.DATA_DIR)
    _print("  ✓ 前端资源：%s"
           % (config.STATIC_DIR if index_ready else "缺失（将显示占位页）"))
    if not index_ready:
        _print("    ⚠️  前端尚未构建，请执行：cd web && npm install && npm run build")
    _print()
    _print("  按 Ctrl + C 可停止服务")
    _print()

    if not args.no_browser:
        _open_browser(server.url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _print("\n  正在停止服务…")
        server.shutdown()
        ctx.shutdown()
        _print("  ✓ 服务已停止，数据已保存")
    return 0


if __name__ == "__main__":
    sys.exit(main())
