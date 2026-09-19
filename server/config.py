# -*- coding: utf-8 -*-
"""全局配置常量与共享工具函数（后端「共享知识中枢」）。

约定见《架构设计》第 10 节：路径常量、端口、错误码、风险等级、原因码、
默认设置与格式化工具全部集中在本模块，其他模块一律从这里引用，
杜绝魔法字符串散落各处。

本模块只依赖 Python 3 标准库。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# ------------------------------------------------------------------ 基本信息
APP_NAME = "Mac 清理助手"
VERSION = "1.0.0"
USER_AGENT = "mac-cleaner/1.0.0"

# ------------------------------------------------------------------ 路径常量
PROJECT_ROOT = Path(__file__).resolve().parent.parent      # mac-cleaner/
DATA_DIR = PROJECT_ROOT / "data"                           # 唯一可写数据目录
DB_PATH = DATA_DIR / "mac_cleaner.db"
AUDIT_LOG_PATH = DATA_DIR / "audit.log"                    # SEC-10 安全审计留痕
APP_LOG_PATH = DATA_DIR / "app.log"                        # 运行日志
STATIC_DIR = PROJECT_ROOT / "static"                       # 前端构建产物
WEB_DIR = PROJECT_ROOT / "web"                             # 前端源码
HOME = str(Path.home())

# ------------------------------------------------------------------ 服务常量
HOST = "127.0.0.1"          # 系统红线：绝不绑 0.0.0.0
DEFAULT_PORT = 8765
PORT_RETRY = 10             # A1：端口占用自动 +1，最多重试 10 次
API_PREFIX = "/api"

# ------------------------------------------------------------------ 业务常量
SCAN_TIMEOUT_SEC = 300              # ERR-06：扫描超时 5 分钟
SCAN_DEFAULT_CONCURRENCY = 4        # SCAN-15：默认并发线程数
HISTORY_DETAIL_KEEP = 5             # Q7：路径级明细保留最近 5 次
CONFIRM_TEXT = "确认删除"            # SEC-07：高危类目输入式确认文本
PREVIEW_TTL_SEC = 600               # 预览令牌 TTL 10 分钟，防 TOCTOU
TRASH_DIR_NAME = ".Trash"           # CLEAN-07：移到废纸篓目标目录名
LOG_MAX_BYTES = 5 * 1024 * 1024     # A6：日志单文件上限 5 MB
LOG_BACKUP_COUNT = 3                # A6：滚动保留 3 份
SCAN_PROGRESS_REPORT_EVERY = 200    # 每 200 项上报一次进度
MAX_ITEMS_PER_CATEGORY = 5000       # 单个类目返回的最大条目数，防止响应体过大

# ------------------------------------------------------------------ 错误码枚举
# 一律 HTTP 200 + body 中的 code 字段表达业务错误（10.2 节约定）
class ErrorCode:
    """统一错误码枚举。"""

    OK = 0
    BAD_REQUEST = 1001
    NOT_FOUND = 1002
    SCAN_RUNNING = 1003
    SCAN_NOT_RUNNING = 1004
    INVALID_PREVIEW_TOKEN = 1005
    CLEAN_RUNNING = 1006
    SETTINGS_INVALID = 1007
    SAFETY_BLOCKED = 2001
    CONFIRM_REQUIRED = 2002
    LIMIT_EXCEEDED = 2003
    PERMISSION_DENIED = 3001
    FILE_IN_USE = 3002
    IO_ERROR = 3003
    INTERNAL_ERROR = 9001


ERROR_MESSAGE: Dict[int, str] = {
    ErrorCode.OK: "ok",
    ErrorCode.BAD_REQUEST: "请求参数不合法",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.SCAN_RUNNING: "扫描正在进行中，请稍后再试",
    ErrorCode.SCAN_NOT_RUNNING: "还没有扫描结果，请先执行一次扫描",
    ErrorCode.INVALID_PREVIEW_TOKEN: "预览已过期，请重新预览后再清理",
    ErrorCode.CLEAN_RUNNING: "清理正在进行中，请先等待本次清理结束",
    ErrorCode.SETTINGS_INVALID: "设置项取值不合法",
    ErrorCode.SAFETY_BLOCKED: "安全检查未通过，已阻止该操作",
    ErrorCode.CONFIRM_REQUIRED: "需要先完成确认才能继续",
    ErrorCode.LIMIT_EXCEEDED: "单次清理量超出上限，建议分批清理",
    ErrorCode.PERMISSION_DENIED: "没有访问权限",
    ErrorCode.FILE_IN_USE: "文件正被其他程序占用",
    ErrorCode.IO_ERROR: "文件系统读写错误",
    ErrorCode.INTERNAL_ERROR: "服务内部错误，详情已写入日志文件",
}


class ApiError(Exception):
    """业务异常：被 api.py 统一捕获并转换为结构化响应。

    Attributes:
        code: 业务错误码，取自 ErrorCode。
        message: 面向用户的简体中文提示。
        data: 可选的补充信息（如安全拦截的明细）。
    """

    def __init__(self, code: int, message: Optional[str] = None,
                 data: Any = None) -> None:
        super().__init__(message or ERROR_MESSAGE.get(code, "未知错误"))
        self.code = code
        self.message = message or ERROR_MESSAGE.get(code, "未知错误")
        self.data = data

    def to_dict(self) -> Dict[str, Any]:
        """转换为统一响应体的字典形式。"""
        return {"code": self.code, "message": self.message, "data": self.data}


class SafetyError(ApiError):
    """安全关卡拦截异常，携带原因码便于前端展示原因文案。"""

    def __init__(self, reason_code: str, message: Optional[str] = None,
                 data: Any = None) -> None:
        super().__init__(ErrorCode.SAFETY_BLOCKED, message or REASON_TEXT.get(
            reason_code, reason_code), data)
        self.reason_code = reason_code


# ------------------------------------------------------------------ 风险等级枚举
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

RISK_TEXT: Dict[str, str] = {
    RISK_LOW: "低风险",
    RISK_MEDIUM: "中风险",
    RISK_HIGH: "高风险",
}

RISK_COLOR: Dict[str, str] = {
    RISK_LOW: "#3FB950",
    RISK_MEDIUM: "#F5A623",
    RISK_HIGH: "#F2545B",
}

# ------------------------------------------------------------------ 原因码枚举（4.3 节）
REASON_TEXT: Dict[str, str] = {
    "ok": "通过",
    "empty_path": "路径为空，已拒绝",
    "root_path": "拒绝清理根目录",
    "path_traversal": "路径含跳跃或符号链接逃逸，已拒绝",
    "not_in_whitelist": "不在可清理类目清单内，已拒绝",
    "blacklist": "属于受保护的关键目录，已拒绝",
    "sip": "受系统保护，已跳过",
    "in_use": "文件正在被其他程序使用，已跳过",
    "permission_denied": "无访问权限，已跳过",
    "outside_home": "位于主目录之外，需额外确认",
    "limit_exceeded": "单次清理量超限，建议分批清理",
    "not_found": "路径已不存在，已跳过",
    "io_error": "系统错误，已跳过",
}

# ------------------------------------------------------------------ 清理/扫描状态枚举
JOB_IDLE = "idle"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_ABORTED = "aborted"
JOB_ERROR = "error"

MODE_TRASH = "trash"
MODE_DELETE = "delete"

RESULT_SUCCESS = "success"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"

# 历史记录状态（clean_history.status 列口径）。
# 注意：与「任务状态」JOB_* 不是同一套取值 —— 任务用 done，
# 历史用 completed，Store 的累计释放统计按 completed 过滤，
# 二者之间必须经 HISTORY_STATUS_BY_JOB 转换，否则统计恒为 0。
HISTORY_COMPLETED = "completed"
HISTORY_ABORTED = "aborted"
HISTORY_ERROR = "error"

HISTORY_STATUS_BY_JOB: Dict[str, str] = {
    JOB_DONE: HISTORY_COMPLETED,
    JOB_ABORTED: HISTORY_ABORTED,
    JOB_ERROR: HISTORY_ERROR,
}

# 无权限时的授权引导文案（ERR-03）
PERMISSION_GUIDE = (
    "需要授权访问，可在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 中为本工具授权"
)

# 检验結果二的 Vista / 体积单位
BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

# ------------------------------------------------------------------ 默认设置
# 读取时与库中值深合并，缺失键自动回落到此处默认值（向前兼容）
DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": MODE_TRASH,                      # Q1：默认移到废纸篓
    "scan_external_volumes": False,          # Q2：默认只扫启动盘
    "category_enabled": {
        "user_caches": True,
        "app_caches": True,
        "system_caches": True,
        "user_logs": True,
        "system_logs": False,
        "temp_files": True,
        "trash_residue": True,
        "downloads_large": True,             # Q5：纳入扫描但默认不勾选
        "xcode_junk": False,                 # Q8：开发者类目默认关闭
        "pkg_manager_caches": False,
        "browser_caches": False,             # Q3：隐私敏感项默认关闭
        "ios_backup": False,                 # Q6：提供但默认关闭
        "external_volumes": False,           # Q2：默认只扫启动盘
    },
    "category_default_selected": {           # Q5/Q6：个人数据类默认不勾选
        "downloads_large": False,
        "ios_backup": False,
    },
    "limits": {                              # SEC-09
        "max_bytes_per_clean": 21474836480,  # 20 GB
        "max_items_per_clean": 50000,
    },
    "downloads": {                           # SCAN-09
        "min_size_bytes": 104857600,         # 100 MB
        "min_days_unused": 30,
        "time_basis": "atime_or_mtime",      # A4：以 max(atime, mtime) 计算
    },
    "scan": {                                # SCAN-15
        "concurrency": SCAN_DEFAULT_CONCURRENCY,
        "timeout_sec": SCAN_TIMEOUT_SEC,
    },
    # UI-02：出厂为深色主题（团队决策），仍提供 follow system / light 选项
    "appearance": "dark",
}


# ------------------------------------------------------------------ 工具函数
def ensure_dirs() -> None:
    """确保运行时数据目录存在（start.py 与 Store 均可安全调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    """返回 ISO 8601 本地时间字符串（含时区偏移）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_ts() -> float:
    """返回当前时间戳（秒，float）。"""
    return time.time()


def format_bytes(num: int) -> str:
    """把字节数格式化为人类可读字符串，保留 1 位小数。

    Args:
        num: 原始字节数（负数按 0 处理）。

    Returns:
        形如 ``"1.4 GB"`` / ``"512 KB"`` / ``"128 B"`` 的字符串。
    """
    value = float(max(0, int(num)))
    unit_index = 0
    while value >= 1024.0 and unit_index < len(BYTE_UNITS) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return "%d B" % int(value)
    if value >= 100:
        return "%.0f %s" % (value, BYTE_UNITS[unit_index])
    return "%.1f %s" % (value, BYTE_UNITS[unit_index])


def parse_bytes(text: str) -> int:
    """把 ``"1.5GB"`` / ``"200 MB"`` / ``"1024"`` 解析为字节数。

    Args:
        text: 人类输入的容量字符串。

    Returns:
        字节数 int；无法解析时返回 0。
    """
    raw = str(text).strip().upper().replace(" ", "")
    if not raw:
        return 0
    scale = 1
    for exp, unit in enumerate(BYTE_UNITS):
        if raw.endswith(unit) and unit != "B":
            scale = 1024 ** exp
            raw = raw[: -len(unit)]
            break
    else:
        if raw.endswith("B"):
            raw = raw[:-1]
    try:
        return int(float(raw) * scale)
    except (TypeError, ValueError):
        return 0


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并两个字典，返回新字典（不修改入参）。

    ``patch`` 中的同类型 dict 会递归合并，其余类型直接覆盖。
    """
    result: Dict[str, Any] = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def abbreviate_home(path: str, home: Optional[str] = None) -> str:
    """把位于主目录下的路径缩写为 ``~/...`` 形式，便于 UI 展示。"""
    base = home or HOME
    if not path:
        return ""
    if base and path == base:
        return "~"
    if base and path.startswith(base + os.sep):
        return "~" + path[len(base):]
    return path


def clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    """把任意输入安全转换为 ``[low, high]`` 区间内的整数。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, num))


def is_within(child: str, parent: str) -> bool:
    """判断 ``child`` 是否位于 ``parent`` 之内（含自身），带路径分隔符边界。"""
    if not child or not parent:
        return False
    parent = parent.rstrip(os.sep) or os.sep
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def canonical_path(path: Optional[str]) -> str:
    """把路径规范化为「解析符号链接后的绝对路径」。

    macOS 上 ``/tmp``、``/var``、``/etc`` 都是指向 ``/private/...`` 的符号链接。
    若比较双方一侧解析了符号链接、另一侧没有，就会出现「路径明明在沙箱/白名单
    内，却被判定为不在」的误判。因此凡是参与路径比较的根（沙箱根、主目录、
    白名单根、黑名单/SIP 前缀）一律先过这里做统一规范化。

    Args:
        path: 任意路径；``None`` / 空串返回空串。

    Returns:
        规范化后的绝对路径；异常时回退到 ``os.path.normpath``。
    """
    if not path:
        return ""
    try:
        return os.path.realpath(str(path))
    except (OSError, ValueError):                # pragma: no cover - 极端输入
        return os.path.normpath(str(path))


def disk_usage_within(path: str) -> Dict[str, int]:
    """返回 ``path`` 所在卷的容量信息。

    Returns:
        ``{"total_bytes", "used_bytes", "free_bytes", "used_percent"}``
        取值失败时全部为 0。
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0,
                "used_percent": 0.0}
    total = int(st.f_blocks * st.f_frsize)
    free = int(st.f_bavail * st.f_frsize)
    avail_raw = int(st.f_bfree * st.f_frsize)
    used = max(0, total - avail_raw)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": round(used * 100.0 / total, 1) if total else 0.0,
    }


def safe_int(value: Any, fallback: int = 0) -> int:
    """把任意输入安全转换为 int，失败时返回 fallback。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
