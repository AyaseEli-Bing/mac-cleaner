# -*- coding: utf-8 -*-
"""类目清单（白名单唯一来源）。

类目表是 SEC-04 白名单制的**唯一数据源**：所有能被清理的路径，
都必须能被追溯到某个已启用类目的某个展开根路径之下。

本模块同时负责：
  1. 根路径展开（支持 glob 通配、``~`` 展开、``sandbox_root`` 重映射）；
  2. 存在性降级探活（ERR-02 / PRD 第 9 节实测事实）；
  3. 与设置合并计算「启用 / 默认勾选」状态。
"""

from __future__ import annotations

import glob
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

# ------------------------------------------------------------------ 分组文案
GROUP_TEXT: Dict[str, str] = {
    "cache": "缓存",
    "log": "日志",
    "temp": "临时文件",
    "trash": "回收站",
    "dev": "开发者",
    "privacy": "隐私敏感",
    "personal": "个人文件",
    "external": "外置卷",
}

STATUS_TEXT: Dict[str, str] = {
    "exists": "已检测到",
    "partial": "部分缺失",
    "not_found": "未检测到 / 未安装",
    "permission_denied": "无访问权限",
}


def _map_home_pattern(pattern: str, home: str,
                      sandbox_root: Optional[str]) -> str:
    """把单个根路径模式重映射到真实（或沙箱）绝对路径。

    规则见架构 10.5 第 8 条：
      - ``~/X``  → ``<home>/X``     ；沙箱模式下 → ``<sandbox>/X``
      - ``/X``   → ``/X``           ；沙箱模式下 → ``<sandbox>/X``
      - ``{a,b}`` 花括号展开在 :func:`_brace_expand` 中处理。
    """
    if pattern.startswith("~"):
        rest = pattern[1:].lstrip("/")
        base = sandbox_root.rstrip("/") if sandbox_root else home.rstrip("/")
        return base if not rest else base + "/" + rest
    if sandbox_root:
        return sandbox_root.rstrip("/") + "/" + pattern.lstrip("/")
    return pattern


def _brace_expand(text: str) -> List[str]:
    """把 ``a/{b,c}/d`` 展开为 ``["a/b/d", "a/c/d"]``（可嵌套，取笛卡尔积）。"""
    results: List[str] = [text]
    pattern = re.compile(r"\{([^{}]*)\}")
    while True:
        changed = False
        expanded: List[str] = []
        for item in results:
            match = pattern.search(item)
            if not match:
                expanded.append(item)
                continue
            head, tail = item[: match.start()], item[match.end():]
            for choice in match.group(1).split(","):
                expanded.append(head + choice.strip() + tail)
            changed = True
        if not changed:
            return results
        results = expanded


class CategoryDef:
    """单个可清理类目的结构化定义。

    Attributes:
        id: 类目唯一标识，如 ``user_caches``。
        name: 前端展示的友好中文名。
        group: 分组 key，取值见 :data:`GROUP_TEXT`。
        desc: 一句大白话说明（UI-03）。
        risk: 风险等级 ``low|medium|high``。
        default_enabled: 是否默认纳入扫描。
        default_selected: 是否默认勾选。
        privacy_sensitive: 是否隐私敏感（Q3 浏览器缓存）。
        needs_confirm_text: 是否需要输入确认文本（SEC-07）。
        keep_dir: 清理时是否保留目录本身（CLEAN-09）。
        roots: 根路径模式列表，支持 ``~``、glob 通配与 ``{a,b}`` 花括号。
        item_depth: 0 = 条目即展开后的根本身；1 = 条目的直接子项。
        match: 匹配规则，形如 ``{"kind": "all"|"contains"|"size_age", ...}``。
        blacklist_exceptions: 该类目放行的黑名单例外路径。
        note: 类目额外提示（如「清理后首次编译会变慢」）。
    """

    def __init__(self, cid: str, name: str, group: str, desc: str,
                 risk: str, default_enabled: bool, default_selected: bool,
                 roots: List[str], match: Optional[Dict[str, Any]] = None,
                 keep_dir: bool = True, privacy_sensitive: bool = False,
                 needs_confirm_text: bool = False, item_depth: int = 1,
                 blacklist_exceptions: Optional[List[str]] = None,
                 note: str = "") -> None:
        self.id = cid
        self.name = name
        self.group = group
        self.desc = desc
        self.risk = risk
        self.default_enabled = default_enabled
        self.default_selected = default_selected
        self.privacy_sensitive = privacy_sensitive
        self.needs_confirm_text = needs_confirm_text
        self.keep_dir = keep_dir
        self.roots = list(roots)
        self.item_depth = item_depth
        self.match = dict(match or {"kind": "all"})
        self.blacklist_exceptions = list(blacklist_exceptions or [])
        self.note = note

    # ---------------------------------------------------------------- 展开
    def resolve_patterns(self, home: str,
                         sandbox_root: Optional[str] = None) -> List[str]:
        """把 ``roots`` 全部展开为可用于 glob 的绝对路径模式列表。"""
        patterns: List[str] = []
        for raw in self.roots:
            for braced in _brace_expand(raw):
                patterns.append(_map_home_pattern(braced, home, sandbox_root))
        return patterns

    def resolve_roots(self, home: str,
                      sandbox_root: Optional[str] = None) -> List[str]:
        """展开并返回**当前实际存在**的根路径列表（绝对路径）。"""
        seen: Dict[str, str] = {}
        for pattern in self.resolve_patterns(home, sandbox_root):
            if not glob.has_magic(pattern):
                candidate = os.path.normpath(pattern)
                if os.path.lexists(candidate):
                    seen.setdefault(candidate, candidate)
                continue
            for found in glob.glob(pattern):
                candidate = os.path.normpath(found)
                seen.setdefault(candidate, candidate)
        return sorted(seen.keys())

    def probe(self, home: str, sandbox_root: Optional[str] = None
              ) -> Dict[str, Any]:
        """探活该类目的根路径，支持存在性降级（ERR-02）。

        Returns:
            ``{"status", "roots", "missing", "text"}``
            status 取值：``exists`` / ``partial`` / ``not_found`` /
            ``permission_denied``。
        """
        patterns = self.resolve_patterns(home, sandbox_root)
        found: List[str] = []
        missing: List[str] = []
        for pattern in patterns:
            hits = glob.glob(pattern) if glob.has_magic(pattern) else (
                [pattern] if os.path.lexists(pattern) else [])
            if hits:
                found.extend(os.path.normpath(h) for h in hits)
            else:
                missing.append(pattern)
        # 去重保序
        uniq_found: List[str] = []
        for item in found:
            if item not in uniq_found:
                uniq_found.append(item)

        if not uniq_found:
            status = "not_found"
        elif missing:
            status = "partial"
        elif not any(os.path.isdir(p) and self._readable(p)
                     for p in uniq_found):
            status = "permission_denied"
        else:
            status = "exists"
        return {
            "status": status,
            "roots": uniq_found,
            "missing": missing,
            "text": STATUS_TEXT.get(status, status),
        }

    @staticmethod
    def _readable(path: str) -> bool:
        try:
            os.scandir(path).close()
            return True
        except OSError:
            return False

    # ---------------------------------------------------------------- 匹配
    def match_item(self, path: str, size: int, mtime: float, atime: float,
                   settings: Optional[Dict[str, Any]] = None,
                   is_dir: bool = False) -> bool:
        """判断候选条目是否符合该类目的匹配规则。

        Args:
            path: 候选条目绝对路径。
            size: 条目体积（目录为递归体积）。
            mtime: 最后修改时间。
            atime: 最后访问时间。
            settings: 完整设置字典（size_age 规则需要读取 downloads 阈值）。
            is_dir: 是否为目录。
        """
        kind = self.match.get("kind", "all")
        if kind == "all":
            return True
        if kind == "contains":
            tokens = self.match.get("tokens", ["Cache"])
            lowered = path.lower()
            for token in tokens:
                if token.lower() in lowered:
                    return True
            return False
        if kind == "size_age":
            conf = ((settings or {}).get("downloads")
                    or config.DEFAULT_SETTINGS["downloads"])
            min_size = int(conf.get("min_size_bytes", 104857600))
            min_days = int(conf.get("min_days_unused", 30))
            basis = conf.get("time_basis", "atime_or_mtime")
            if size < min_size:
                return False
            now = config.now_ts()
            if basis == "mtime":
                latest = mtime
            else:
                # A4：部分 macOS 上 atime 不更新，取 max(atime, mtime) 更保守
                latest = max(atime or 0.0, mtime or 0.0)
            days = (now - latest) / 86400.0 if latest else 0.0
            return days >= min_days
        return True

    def to_dict(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """序列化为 API 响应使用的字典。"""
        data: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "group_text": GROUP_TEXT.get(self.group, self.group),
            "desc": self.desc,
            "risk": self.risk,
            "risk_text": config.RISK_TEXT.get(self.risk, self.risk),
            "risk_color": config.RISK_COLOR.get(self.risk, "#8A8A8A"),
            "default_enabled": self.default_enabled,
            "default_selected": self.default_selected,
            "privacy_sensitive": self.privacy_sensitive,
            "needs_confirm_text": self.needs_confirm_text,
            "keep_dir": self.keep_dir,
            "roots": list(self.roots),
            "match": dict(self.match),
            "blacklist_exceptions": list(self.blacklist_exceptions),
            "note": self.note,
        }
        if extra:
            data.update(extra)
        return data


# ------------------------------------------------------------------ 13 个类目
# 顺序即 UI 展示顺序（体积排序由 scanner 负责，此处为声明顺序）。
CATEGORIES: List[CategoryDef] = [
    CategoryDef(
        cid="user_caches",
        name="用户缓存",
        group="cache",
        desc="App 运行时产生的临时数据，删除后 App 会自动重建，不影响你的文件",
        risk=config.RISK_LOW,
        default_enabled=True,
        default_selected=True,
        roots=["~/Library/Caches"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
    ),
    CategoryDef(
        cid="app_caches",
        name="应用缓存",
        group="cache",
        desc="沙盒 App、共享容器与 Application Support 下的缓存目录",
        risk=config.RISK_LOW,
        default_enabled=True,
        default_selected=True,
        roots=[
            "~/Library/Containers/*/Data/Library/Caches",
            "~/Library/Group Containers/*/Library/Caches",
            "~/Library/Application Support/*/Caches",
        ],
        match={"kind": "contains", "tokens": ["Cache"]},
        keep_dir=True,
        item_depth=0,     # 展开后的根本身就是一条清理项
    ),
    CategoryDef(
        cid="system_caches",
        name="系统级缓存",
        group="cache",
        desc="系统级缓存，通常无需授权即可读取；无权限时会自动跳过",
        risk=config.RISK_MEDIUM,
        default_enabled=True,
        default_selected=True,
        roots=["/Library/Caches"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
        note="位于主目录之外，清理时需要额外确认",
    ),
    CategoryDef(
        cid="user_logs",
        name="用户日志",
        group="log",
        desc="App 与系统产生的日志文件，删除后不影响使用",
        risk=config.RISK_LOW,
        default_enabled=True,
        default_selected=True,
        roots=["~/Library/Logs"],
        match={"kind": "all"},
        keep_dir=True,     # SCAN-05：只清内容，不删日志目录本身
        item_depth=1,
    ),
    CategoryDef(
        cid="system_logs",
        name="系统日志",
        group="log",
        desc="系统级诊断日志，默认关闭，需手动开启",
        risk=config.RISK_MEDIUM,
        default_enabled=False,
        default_selected=True,
        roots=["/Library/Logs", "/private/var/log"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
        note="位于主目录之外，通常需要完全磁盘访问权限",
    ),
    CategoryDef(
        cid="temp_files",
        name="临时文件",
        group="temp",
        desc="系统与 App 的临时目录，正在被使用的文件会自动跳过",
        risk=config.RISK_LOW,
        default_enabled=True,
        default_selected=True,
        roots=["/private/var/folders/*/{T,C}", "/tmp"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
        note="正在被占用的文件会被跳过，不会强行删除",
    ),
    CategoryDef(
        cid="trash_residue",
        name="回收站残留",
        group="trash",
        desc="废纸篓中的内容，清空后才能彻底释放这部分空间",
        risk=config.RISK_LOW,
        default_enabled=True,
        default_selected=True,
        roots=["~/.Trash"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
    ),
    CategoryDef(
        cid="downloads_large",
        name="下载目录大文件",
        group="personal",
        desc="下载目录中体积大且长期未打开的文件，默认不勾选，请自行确认",
        risk=config.RISK_HIGH,
        default_enabled=True,       # Q5：纳入扫描
        default_selected=False,     # Q5：默认不勾选
        roots=["~/Downloads"],
        match={"kind": "size_age"},
        keep_dir=False,
        privacy_sensitive=False,
        needs_confirm_text=True,    # SEC-07：高危类目输入式确认
        item_depth=1,
        note="属于你的个人文件，请逐条确认后再清理",
    ),
    CategoryDef(
        cid="xcode_junk",
        name="Xcode 开发垃圾",
        group="dev",
        desc="Xcode 编译缓存、归档与模拟器数据，清理后首次编译会变慢",
        risk=config.RISK_MEDIUM,
        default_enabled=False,      # US-8：开发者项默认关闭
        default_selected=True,
        roots=[
            "~/Library/Developer/Xcode/DerivedData",
            "~/Library/Developer/Xcode/Archives",
            "~/Library/Developer/Xcode/iOS DeviceSupport",
            "~/Library/Developer/CoreSimulator",
        ],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
        note="清理后下次编译会变慢，设备支持可能需要重新下载",
    ),
    CategoryDef(
        cid="pkg_manager_caches",
        name="包管理器缓存",
        group="dev",
        desc="npm / Yarn / pnpm / Homebrew / Cargo / Gradle / Maven 的下载缓存",
        risk=config.RISK_LOW,
        default_enabled=False,
        default_selected=True,
        roots=[
            "~/.npm/_cacache",
            "~/.cache/yarn",
            "~/Library/pnpm-store",
            "~/Library/Caches/Homebrew",
            "~/.cargo/registry/cache",
            "~/.gradle/caches",
            "~/.m2/repository",
        ],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=0,               # 每个缓存根目录即一条清理项
        note="下次安装依赖时需要重新联网下载",
    ),
    CategoryDef(
        cid="browser_caches",
        name="浏览器缓存",
        group="privacy",
        desc="Safari / Chrome / Firefox / Edge 的浏览缓存，清理后网站可能需要重新登录",
        risk=config.RISK_MEDIUM,
        default_enabled=False,      # Q3：隐私敏感项默认关闭
        default_selected=True,
        privacy_sensitive=True,
        roots=[
            "~/Library/Caches/com.apple.Safari",
            "~/Library/Caches/Google/Chrome",
            "~/Library/Caches/Firefox",
            "~/Library/Caches/com.microsoft.edgemac.*",
            "~/Library/WebKit",
        ],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=0,
        note="隐私敏感：清理后部分网站可能需要重新登录",
    ),
    CategoryDef(
        cid="ios_backup",
        name="iOS 设备备份",
        group="personal",
        desc="iPhone / iPad 的整机备份，删除后无法恢复设备数据",
        risk=config.RISK_HIGH,
        default_enabled=False,      # Q6：提供但默认关闭
        default_selected=False,
        needs_confirm_text=True,    # SEC-07
        roots=["~/Library/Application Support/MobileSync/Backup"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=1,
        blacklist_exceptions=["~/Library/Application Support/MobileSync/Backup"],
        note="含个人数据，删除后不可恢复，请务必先确认已另做备份",
    ),
    CategoryDef(
        cid="external_volumes",
        name="外置卷残留",
        group="external",
        desc="外置磁盘上的回收站与 Spotlight 索引残留，默认关闭",
        risk=config.RISK_MEDIUM,
        default_enabled=False,      # Q2：默认只扫启动盘
        default_selected=True,
        roots=["/Volumes/*/.Trashes", "/Volumes/*/.Spotlight-V100"],
        match={"kind": "all"},
        keep_dir=True,
        item_depth=0,
        note="涉及外接磁盘，请确认磁盘已备份后再清理",
    ),
]


class CategoryRegistry:
    """类目注册表：类目清单的唯一访问入口。

    Usage::

        registry = CategoryRegistry()
        enabled = registry.enabled(settings)
        roots = registry.whitelist_roots(settings, home, sandbox_root)
    """

    def __init__(self, categories: Optional[List[CategoryDef]] = None) -> None:
        self._by_id: Dict[str, CategoryDef] = {}
        for cat in (categories or CATEGORIES):
            self._by_id[cat.id] = cat

    # ---------------------------------------------------------------- 基础
    def all(self) -> List[CategoryDef]:
        """返回全部类目（声明顺序）。"""
        return list(CATEGORIES)

    def get(self, cid: str) -> Optional[CategoryDef]:
        """按 id 取类目，不存在时返回 None。"""
        return self._by_id.get(cid)

    def require(self, cid: str) -> CategoryDef:
        """按 id 取类目，不存在时抛 :class:`ValueError`。"""
        cat = self._by_id.get(cid)
        if cat is None:
            raise ValueError("未知类目：%s" % cid)
        return cat

    # ---------------------------------------------------------------- 设置合并
    def is_enabled(self, cat: CategoryDef,
                   settings: Dict[str, Any]) -> bool:
        """合并默认开关与用户设置，判断该类目是否启用扫描。"""
        enabled_map = (settings or {}).get("category_enabled") or {}
        if cat.id in enabled_map:
            return bool(enabled_map[cat.id])
        return bool(cat.default_enabled)

    def is_selected(self, cat: CategoryDef,
                    settings: Dict[str, Any]) -> bool:
        """合并默认勾选与用户设置，判断该类目条目是否默认勾选。"""
        selected_map = (settings or {}).get("category_default_selected") or {}
        if cat.id in selected_map:
            return bool(selected_map[cat.id])
        return bool(cat.default_selected)

    def enabled(self, settings: Dict[str, Any]) -> List[CategoryDef]:
        """返回全部启用类目列表。"""
        return [c for c in self.all() if self.is_enabled(c, settings)]

    # ---------------------------------------------------------------- 白名单
    def whitelist_roots(self, settings: Dict[str, Any], home: str,
                        sandbox_root: Optional[str] = None) -> List[str]:
        """返回全部启用类目的展开根路径（白名单），已去重并降序排列。

        同时对存在的根追加其 ``realpath``，避免 macOS 上 ``/tmp`` →
        ``/private/tmp`` 这类符号链接导致白名单误判。
        """
        roots: List[str] = []
        for cat in self.enabled(settings):
            for root in cat.resolve_roots(home, sandbox_root):
                roots.append(root)
                if os.path.exists(root):
                    real = os.path.realpath(root)
                    if real != root:
                        roots.append(real)
        # 去重后按长度降序，便于前缀判定时先命中更具体的根
        uniq: List[str] = []
        for root in roots:
            if root not in uniq:
                uniq.append(root)
        uniq.sort(key=lambda p: len(p), reverse=True)
        return uniq

    def blacklist_exceptions(self, settings: Dict[str, Any],
                             home: str,
                             sandbox_root: Optional[str] = None
                             ) -> Dict[str, List[str]]:
        """返回 ``{category_id: [放行的绝对路径...]}`` 映射。"""
        result: Dict[str, List[str]] = {}
        for cat in self.enabled(settings):
            if not cat.blacklist_exceptions:
                continue
            paths: List[str] = []
            for raw in cat.blacklist_exceptions:
                for braced in _brace_expand(raw):
                    paths.append(_map_home_pattern(braced, home, sandbox_root))
            result[cat.id] = paths
        return result

    # ---------------------------------------------------------------- 探活
    def probe_status(self, cid: str, home: str,
                     sandbox_root: Optional[str] = None) -> str:
        """返回类目探活状态码（``exists|partial|not_found|permission_denied``）。"""
        cat = self.get(cid)
        if cat is None:
            return "not_found"
        return cat.probe(home, sandbox_root)["status"]

    def describe(self, settings: Dict[str, Any], home: str,
                 sandbox_root: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回给前端的类目清单（含启用、勾选与探活状态）。"""
        result: List[Dict[str, Any]] = []
        for cat in self.all():
            probe = cat.probe(home, sandbox_root)
            result.append(cat.to_dict({
                "enabled": self.is_enabled(cat, settings),
                "selected": self.is_selected(cat, settings),
                "status": probe["status"],
                "status_text": probe["text"],
                "resolved_roots": probe["roots"],
                "missing_roots": probe["missing"],
                "note": cat.note,
            }))
        return result


# 模块级默认注册表，供扫描器 / 安全守卫 / API 直接复用
REGISTRY = CategoryRegistry()


def symlink_of(path: str) -> bool:
    """判断 ``path`` 是否为符号链接。"""
    try:
        return os.path.islink(path)
    except OSError:
        return False


def lstat_size(path: str) -> int:
    """返回 ``path`` 的 ``lstat`` 体积（不跟随符号链接），失败返回 0。"""
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def is_dir_nofollow(path: str) -> bool:
    """判断 ``path`` 是否为真实目录（符号链接指向目录时返回 False）。"""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def normalize_root_set(roots: List[str]) -> Tuple[str, ...]:
    """规范化根路径集合，用于快速成员判定。"""
    return tuple({os.path.normpath(r) for r in roots})


def abspath_under(path: str, base: Optional[str]) -> str:
    """把相对路径拼到 ``base`` 下并返回绝对路径。"""
    if not base:
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(base, path))


def ensure_path(path: str) -> Path:
    """返回 :class:`pathlib.Path` 形式，便于做路径运算。"""
    return Path(path)
