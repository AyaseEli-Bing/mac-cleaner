# -*- coding: utf-8 -*-
"""五道安全关卡（SEC-01 ~ SEC-12 的核心实现）。

任何一次删除 / 移动都必须串行通过：

    关卡一 路径规范化 → 关卡二 白名单 → 关卡三 黑名单
    → 关卡四 SIP 保护 → 关卡五 占用检测

任一关卡不通过立即短路返回，并调用注入的 ``audit`` 回调留痕。
本模块为纯逻辑模块：不感知 HTTP、不直接写数据库，
因此可被单元测试 100% 覆盖（配合 ``sandbox_root`` 注入）。
"""

from __future__ import annotations

import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from . import config

# ------------------------------------------------------------------ 黑名单表
# 语义说明（用于 :meth:`SafetyGuard.gate3_blacklist`）：
#   * 以 ``/**`` 结尾  → 该目录及其全部子孙后代均禁止
#   * 其余条目        → 该路径本身及其子孙后代均禁止
#   * 特例 ``"/"``    → 仅精确匹配（否则会误封整盘）
# SEC-01 要求覆盖的清单逐条来自 PRD 4.4；第 26 条 ``com.apple.TCC``
# 是本工程额外补充的系统隐私授权库，同样不可触碰。
BLACKLIST: Tuple[str, ...] = (
    "/",
    "/System/**",
    "/usr/**",                                       # /usr/local/** 例外放行
    "/bin",
    "/sbin",
    "/etc",
    "/var/db",
    "/private/var/db",
    "/private/var/root",
    "/Library/Apple/**",
    "/Library/Preferences",
    "/Library/Keychains",
    "~/Library/Keychains",
    "~/Library/Preferences",
    "~/Library/Mail",
    "~/Library/Messages",
    "~/Library/Application Support/AddressBook",
    "~/Library/Application Support/MobileSync",      # ios_backup 例外放行 Backup
    "~/Library/Application Support/com.apple.TCC",
    "~/Documents",
    "~/Desktop",
    "~/Pictures",
    "~/Movies",
    "~/Music",
    "~/Public",
    "~/Library/Mobile Documents/**",
)

# 黑名单内的「反向白名单」——即使落在黑名单前缀下也放行。
# /usr/** 规则要求放行 /usr/local（Homebrew 等常见安装位置）。
BLACKLIST_ALLOW: Tuple[str, ...] = (
    "/usr/local",
)

# ------------------------------------------------------------------ SIP 保护前缀
# SEC-02：命中即只读扫描，一律拒绝删除。
SIP_PREFIXES: Tuple[str, ...] = (
    "/System/Library",
    "/usr/lib",
    "/Library/Apple",
    "/System/Volumes/Data/System",
    "/private/var/db",
)


class GateResult(NamedTuple):
    """单个关卡的判定结果。

    Attributes:
        ok: 是否通过该关卡。
        code: 原因码，见 config.REASON_TEXT。
        message: 简体中文人类可读说明。
        normalized_path: 关卡一产出的 realpath（未通过管线时可能为空字符串）。
    """

    ok: bool
    code: str
    message: str
    normalized_path: str

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 API 友好字典。"""
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "path": self.normalized_path,
        }


def _ok(norm_path: str) -> GateResult:
    return GateResult(True, "ok", config.REASON_TEXT["ok"], norm_path)


def _fail(code: str, norm_path: str = "") -> GateResult:
    return GateResult(False, code, config.REASON_TEXT.get(code, code), norm_path)


class SafetyGuard:
    """五道安全关卡的守卫对象。

    Args:
        blacklist: 黑名单条目列表，默认取 :data:`BLACKLIST`。
        sip_prefixes: SIP 保护前缀列表，默认取 :data:`SIP_PREFIXES`。
        whitelist_roots: 已启用类目的展开根路径列表（白名单）。
        home_dir: 当前生效的主目录（沙箱模式下为沙箱根）。
        audit: 可选留痕回调 ``audit(path, code, message)``。
        sandbox_root: 沙箱根路径；非空时所有路径判定被限制在沙箱内。
        category_roots: 可选的 ``{category_id: [根路径...]}`` 映射。
        blacklist_exceptions: 可选的 ``{category_id: [放行路径...]}`` 映射。
    """

    def __init__(self,
                 blacklist: Optional[List[str]] = None,
                 sip_prefixes: Optional[List[str]] = None,
                 whitelist_roots: Optional[List[str]] = None,
                 home_dir: Optional[str] = None,
                 audit: Optional[Callable[[str, str, str], None]] = None,
                 sandbox_root: Optional[str] = None,
                 category_roots: Optional[Dict[str, List[str]]] = None,
                 blacklist_exceptions: Optional[Dict[str, List[str]]] = None
                 ) -> None:
        self._raw_blacklist: List[str] = list(
            blacklist if blacklist is not None else BLACKLIST)
        self._raw_sip: List[str] = list(
            sip_prefixes if sip_prefixes is not None else SIP_PREFIXES)
        self._audit = audit
        self._sandbox_root: Optional[str] = (
            config.canonical_path(sandbox_root) if sandbox_root else None)
        self._home: str = config.canonical_path(home_dir or config.HOME)
        # 所有参与比较的根都必须先解析符号链接：macOS 上 ``/var``、``/tmp``、
        # ``/etc`` 均指向 ``/private/...``，一侧解析、一侧不解析会造成
        # 「路径明明在白名单内却被拒绝」的误判。
        self._whitelist_roots: List[str] = [
            p for p in (config.canonical_path(x)
                        for x in (whitelist_roots or [])) if p]
        self._category_roots: Dict[str, List[str]] = {
            k: [p for p in (config.canonical_path(x) for x in v) if p]
            for k, v in (category_roots or {}).items()}
        self._blacklist_exceptions: Dict[str, List[str]] = {
            k: [p for p in (config.canonical_path(x) for x in v) if p]
            for k, v in (blacklist_exceptions or {}).items()}

        # 预展开：``~`` 展开、``/**`` 去通配、沙箱镜像一次算好，
        # 热路径只做纯字符串前缀比较
        self._blacklist: List[Tuple[str, bool]] = []
        for entry in self._raw_blacklist:
            exact_only = str(entry).strip() == "/"      # 根哨兵：仅精确匹配
            for form in self._forms(entry):
                self._blacklist.append((form, exact_only))
        self._blacklist_allow: List[str] = []
        for entry in BLACKLIST_ALLOW:
            for form in self._forms(entry):
                if form not in self._blacklist_allow:
                    self._blacklist_allow.append(form)
        self._sip_prefixes: List[str] = []
        for entry in self._raw_sip:
            for form in self._forms(entry):
                if form not in self._sip_prefixes:
                    self._sip_prefixes.append(form)

    # ------------------------------------------------------------ 内部工具
    def _forms(self, entry: str) -> List[str]:
        """把一个黑名单 / SIP 条目展开为「应当拦截的路径形态」列表。

        规则：
          * ``/**`` 后缀与普通条目语义一致（该目录及其全部子孙），去掉通配符即可；
          * ``~/X`` 展开为当前生效主目录（沙箱模式下即沙箱根）；
          * ``/X`` 同时产出**字面形态**与**沙箱镜像形态**（``<sandbox>/X``）——
            沙箱模式下宿主绝对路径依然必须被拦截，故拦截范围只增不减；
          * 根哨兵 ``/`` 只产出精确匹配形态（否则会误封整盘）；沙箱模式下
            额外把沙箱根当作「根」来保护。
        """
        text = str(entry).strip()
        if not text:
            return []
        if text.endswith("/**"):
            text = text[: -3]
        forms: List[str] = []

        def push(candidate: str) -> None:
            norm = config.canonical_path(candidate) if candidate else ""
            if norm and norm not in forms:
                forms.append(norm)

        if text == "/":
            push("/")
            if self._sandbox_root:
                push(self._sandbox_root)
            return forms

        if text.startswith("~"):
            rest = text[1:].lstrip("/")
            push(self._home if not rest else os.path.join(self._home, rest))
            return forms

        push(text)                                   # 字面形态（真实绝对路径）
        if self._sandbox_root and text.startswith("/"):
            push(os.path.join(self._sandbox_root, text.lstrip("/")))
        return forms

    def _notify(self, path: str, result: GateResult) -> None:
        """非 ok 结果统一留痕（SEC-10）。"""
        if self._audit is None or result.ok:
            return
        self._audit(path, result.code, result.message)

    # ------------------------------------------------------------ 主控
    def check(self, raw_path: str, category_id: Optional[str] = None,
              skip_in_use: bool = False) -> GateResult:
        """串行执行五道关卡，任一不通过立即短路返回。

        Args:
            raw_path: 原始路径字符串（可能来自磁盘遍历，不可信）。
            category_id: 所属类目 id，用于黑名单例外放行。
            skip_in_use: 为 True 时跳过关卡五（预览阶段的轻量只读模式）。

        Returns:
            :class:`GateResult`。
        """
        result = self.gate1_normalize(raw_path)
        if not result.ok:
            self._notify(raw_path, result)
            return result
        norm = result.normalized_path

        for gate in (
            lambda: self.gate2_whitelist(norm, category_id),
            lambda: self.gate3_blacklist(norm, category_id),
            lambda: self.gate4_sip(norm),
        ):
            result = gate()
            if not result.ok:
                self._notify(norm, result)
                return result

        if skip_in_use:
            # 轻量模式：跳过昂贵的独占打开（``O_EXLOCK``）与可写探测，
            # 但仍必须确认路径存在——否则预览会为「已消失的文件」放行。
            if not os.path.lexists(norm):
                result = _fail("not_found", norm)
                self._notify(norm, result)
                return result
            return _ok(norm)

        result = self.gate5_in_use(norm)
        if not result.ok:
            self._notify(norm, result)
            return result
        return _ok(norm)

    # ------------------------------------------------------------ 关卡一
    def gate1_normalize(self, raw_path: str) -> GateResult:
        """路径规范化：空串 / 根 / ``~`` / ``$HOME`` 熔断 + realpath + 沙箱逃逸检测。

        Returns:
            通过时 ``normalized_path`` 为 ``os.path.realpath`` 的结果。
        """
        if raw_path is None or not str(raw_path).strip():
            return _fail("empty_path")
        raw = str(raw_path).strip()
        # 熔断：根目录 ``/`` 与 ``~`` / ``$HOME`` 本身一律拒绝（SEC-11）。
        # 必须在 expanduser 之前判定 ``~``：展开后 ``~`` 会变成长路径，
        # 沙箱模式下会被误报成 path_traversal 而不是 root_path。
        if raw in ("/", "~") or os.path.expandvars(raw) in ("/", "~"):
            return _fail("root_path")
        candidate = os.path.expanduser(os.path.expandvars(raw))
        candidate = candidate.strip()
        if not candidate:
            return _fail("empty_path")

        if os.path.normpath(candidate) in ("/", os.path.normpath(self._home)):
            return _fail("root_path")

        if not os.path.isabs(candidate) and self._sandbox_root:
            candidate = os.path.join(self._sandbox_root, candidate)

        try:
            norm = os.path.realpath(candidate)
        except OSError:
            return _fail("path_traversal")

        if norm in ("/", os.path.normpath(self._home)):
            return _fail("root_path")
        if not norm.startswith("/"):
            return _fail("path_traversal")

        # 沙箱模式：解析结果不得逃出沙箱（等价于拦截 ``..`` 穿越与软链逃逸）
        if self._sandbox_root and not config.is_within(norm, self._sandbox_root):
            return _fail("path_traversal")
        return _ok(norm)

    # ------------------------------------------------------------ 关卡二
    def gate2_whitelist(self, norm_path: str,
                        category_id: Optional[str] = None) -> GateResult:
        """白名单：必须落在某个启用类目的展开根之下（SEC-04）。"""
        if not norm_path:
            return _fail("empty_path", norm_path)
        candidates: List[str] = []
        if category_id:
            candidates.extend(self._category_roots.get(category_id, []))
        candidates.extend(self._whitelist_roots)
        for root in candidates:
            if root and config.is_within(norm_path, root):
                return _ok(norm_path)
        return _fail("not_in_whitelist", norm_path)

    # ------------------------------------------------------------ 关卡三
    def gate3_blacklist(self, norm_path: str,
                        category_id: Optional[str] = None) -> GateResult:
        """黑名单：命中硬编码黑名单即拒绝，类目例外优先放行（SEC-01）。"""
        if not norm_path:
            return _fail("empty_path", norm_path)

        # 例外放行（例：ios_backup 的 MobileSync/Backup）
        for special in self._blacklist_exceptions.get(category_id or "", []):
            if config.is_within(norm_path, special):
                return _ok(norm_path)

        # /usr/local 反向白名单优先于 /usr/** 规则
        for allowed in self._blacklist_allow:
            if config.is_within(norm_path, allowed):
                return _ok(norm_path)

        for entry, exact_only in self._blacklist:
            if not entry:
                continue
            if exact_only:
                if norm_path == entry:
                    return _fail("blacklist", norm_path)
                continue
            if config.is_within(norm_path, entry):
                return _fail("blacklist", norm_path)
        return _ok(norm_path)

    # ------------------------------------------------------------ 关卡四
    def gate4_sip(self, norm_path: str) -> GateResult:
        """SIP 保护：命中受系统保护前缀即拒绝删除（SEC-02）。"""
        if not norm_path:
            return _fail("empty_path", norm_path)
        for prefix in self._sip_prefixes:
            if not prefix:
                continue
            if config.is_within(norm_path, prefix):
                return _fail("sip", norm_path)
        return _ok(norm_path)

    # ------------------------------------------------------------ 关卡五
    def gate5_in_use(self, norm_path: str) -> GateResult:
        """占用检测：文件尝试独占打开，目录检查可写权限（SEC-05）。

        被占用或无权访问一律跳过，**绝不重试强删**。
        """
        if not norm_path:
            return _fail("empty_path", norm_path)
        if not os.path.lexists(norm_path):
            return _fail("not_found", norm_path)

        is_symlink = os.path.islink(norm_path)
        try:
            st = os.lstat(norm_path)
        except OSError as exc:
            return self._to_gate_error(exc, norm_path)

        if stat.S_ISDIR(st.st_mode) and not is_symlink:
            if not os.access(norm_path, os.W_OK | os.X_OK):
                return _fail("permission_denied", norm_path)
            return _ok(norm_path)

        # 普通文件 / 符号链接：尝试独占打开
        flags = os.O_RDWR
        exlock = getattr(os, "O_EXLOCK", 0)
        try:
            fd = os.open(norm_path, flags | exlock)
        except OSError as exc:
            if exlock and exc.errno in (errno.ENOTSUP, errno.EINVAL):
                try:
                    fd = os.open(norm_path, flags)
                except OSError as exc2:
                    return self._to_gate_error(exc2, norm_path)
            else:
                return self._to_gate_error(exc, norm_path)
        try:
            os.close(fd)
        except OSError:
            pass
        return _ok(norm_path)

    @staticmethod
    def _to_gate_error(exc: OSError, path: str) -> GateResult:
        """把 :class:`OSError` 翻译为语义化的关卡结果。"""
        if isinstance(exc, PermissionError) or exc.errno in (
                errno.EACCES, errno.EPERM, errno.EROFS):
            return _fail("permission_denied", path)
        if exc.errno in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK,
                         errno.ETXTBSY):
            return _fail("in_use", path)
        if exc.errno == errno.ENOENT:
            return _fail("not_found", path)
        return _fail("io_error", path)

    # ------------------------------------------------------------ 批量
    def check_batch(self, paths: List[str],
                    category_id: Optional[str] = None,
                    max_workers: int = 4,
                    skip_in_use: bool = False) -> Dict[str, GateResult]:
        """并发批量校验。

        Args:
            paths: 待校验的原始路径列表。
            category_id: 统一类目 id；为 None 时按白名单并集判定。
            max_workers: 并发线程数。
            skip_in_use: 是否跳过关卡五。

        Returns:
            ``{原始路径: GateResult}`` 映射。
        """
        unique_paths: List[str] = []
        for path in paths:
            if path not in unique_paths:
                unique_paths.append(path)
        if not unique_paths:
            return {}
        workers = max(1, min(max_workers, len(unique_paths)))
        results: Dict[str, GateResult] = {}
        if workers == 1:
            for path in unique_paths:
                results[path] = self.check(path, category_id, skip_in_use)
            return results
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="safety") as pool:
            futures = {
                path: pool.submit(self.check, path, category_id, skip_in_use)
                for path in unique_paths}
            for path, future in futures.items():
                try:
                    results[path] = future.result()
                except Exception as exc:                     # pragma: no cover
                    results[path] = GateResult(
                        False, "io_error", str(exc), "")
        return results

    # ------------------------------------------------------------ 上限
    def check_limits(self, items: List[dict], limits: dict) -> GateResult:
        """单次清理上限校验（SEC-09）。

        Args:
            items: 待清理条目字典列表，需含 ``size`` 字段。
            limits: ``{"max_bytes_per_clean": int, "max_items_per_clean": int}``。

        Returns:
            通过返回 ``ok``；超限返回 ``limit_exceeded`` 并给出分批建议。
        """
        limits = limits or {}
        max_bytes = int(limits.get(
            "max_bytes_per_clean",
            config.DEFAULT_SETTINGS["limits"]["max_bytes_per_clean"]))
        max_items = int(limits.get(
            "max_items_per_clean",
            config.DEFAULT_SETTINGS["limits"]["max_items_per_clean"]))

        total_bytes = sum(int(i.get("size", 0) or 0) for i in items)
        total_items = len(items)
        if max_bytes > 0 and total_bytes > max_bytes:
            batches = (total_bytes + max_bytes - 1) // max_bytes
            return GateResult(
                False, "limit_exceeded",
                "单次清理总量 %s 超过上限 %s，建议分批清理（约 %d 批）。"
                % (config.format_bytes(total_bytes),
                   config.format_bytes(max_bytes), max(2, batches)),
                "")
        if max_items > 0 and total_items > max_items:
            batches = (total_items + max_items - 1) // max_items
            return GateResult(
                False, "limit_exceeded",
                "单次清理条目 %d 条超过上限 %d 条，建议分成约 %d 批清理。"
                % (total_items, max_items, max(2, batches)),
                "")
        return _ok("")


def build_guard(settings: Dict[str, Any], registry: Any, home: str,
                sandbox_root: Optional[str] = None,
                audit: Optional[Callable[[str, str, str], None]] = None
                ) -> SafetyGuard:
    """工厂函数：按当前设置构建一个安全守卫。

    Args:
        settings: 完整设置字典。
        registry: :class:`~server.categories.CategoryRegistry` 实例。
        home: 当前生效主目录。
        sandbox_root: 沙箱根（非 None 时全部路径判定限制在沙箱内）。
        audit: 留痕回调。

    Returns:
        已装配白名单与黑名单例外的 :class:`SafetyGuard`。
    """
    roots = registry.whitelist_roots(settings, home, sandbox_root)
    category_roots: Dict[str, List[str]] = {}
    for cat in registry.enabled(settings):
        category_roots[cat.id] = cat.resolve_roots(home, sandbox_root)
    exceptions = registry.blacklist_exceptions(settings, home, sandbox_root)
    return SafetyGuard(
        blacklist=list(BLACKLIST),
        sip_prefixes=list(SIP_PREFIXES),
        whitelist_roots=roots,
        home_dir=sandbox_root or home,
        audit=audit,
        sandbox_root=sandbox_root,
        category_roots=category_roots,
        blacklist_exceptions=exceptions,
    )
