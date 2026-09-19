# -*- coding: utf-8 -*-
"""扫描引擎。

设计要点（对应架构 D2 / D3 / D6）：
  * ``ThreadPoolExecutor`` 并发遍历类目，``os.scandir`` 比 ``os.walk`` 更快；
  * 符号链接**不递归**（``follow_symlinks=False`` 天然满足 STAT-06）；
  * 权限 / 不存在等异常一律降级为类目级 warning，绝不向上抛（D3）；
  * 支持取消（≤3 秒响应）与 5 分钟超时熔断；
  * ``sandbox_root`` 注入后所有类目根被重映射，QA 可完全隔离真实目录（D6）。
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .categories import CategoryDef, CategoryRegistry, REGISTRY


def item_id(path: str) -> str:
    """计算条目的稳定 id：``sha1(path)[:16]``（架构 10.5 第 4 条）。"""
    return hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]


class ScanItem:
    """一条可清理条目。"""

    __slots__ = ("id", "path", "display", "category_id", "size", "type",
                 "mtime", "atime", "risk", "in_home", "selected", "flags",
                 "keep_dir", "exists")

    def __init__(self, path: str, category_id: str, size: int,
                 item_type: str, mtime: float, atime: float, risk: str,
                 in_home: bool, selected: bool, flags: List[str],
                 keep_dir: bool, display: Optional[str] = None,
                 oid: Optional[str] = None) -> None:
        self.id: str = oid or item_id(path)
        self.path: str = path
        self.display: str = display or os.path.basename(path.rstrip("/")) or path
        self.category_id: str = category_id
        self.size: int = int(max(0, size))
        self.type: str = item_type
        self.mtime: float = float(mtime or 0.0)
        self.atime: float = float(atime or 0.0)
        self.risk: str = risk
        self.in_home: bool = bool(in_home)
        self.selected: bool = bool(selected)
        self.flags: List[str] = list(flags or [])
        self.keep_dir: bool = bool(keep_dir)
        self.exists: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 API 响应使用的字典。"""
        return {
            "id": self.id,
            "path": self.path,
            "display": self.display,
            "category_id": self.category_id,
            "size": self.size,
            "type": self.type,
            "mtime": self.mtime,
            "atime": self.atime,
            "risk": self.risk,
            "in_home": self.in_home,
            "selected": self.selected,
            "flags": list(self.flags),
            "keep_dir": self.keep_dir,
        }


class CategoryResult:
    """单个类目的聚合结果。"""

    def __init__(self, category: CategoryDef) -> None:
        self.category_id: str = category.id
        self.name: str = category.name
        self.group: str = category.group
        self.risk: str = category.risk
        self.desc: str = category.desc
        self.note: str = category.note
        self.size: int = 0
        self.item_count: int = 0
        self.percent: float = 0.0
        self.status: str = "ok"             # ok | not_found | permission_denied
        self.warnings: List[Dict[str, Any]] = []
        self.items: List[ScanItem] = []
        self.selected: bool = True
        self.privacy_sensitive: bool = category.privacy_sensitive
        self.needs_confirm_text: bool = category.needs_confirm_text
        self.keep_dir: bool = category.keep_dir
        self.resolved_roots: List[str] = []
        self.probe_status: str = "exists"

    def add(self, item: ScanItem) -> None:
        """累加一个条目到本类目。"""
        self.items.append(item)
        self.size += item.size
        self.item_count += 1

    def finalize(self) -> None:
        """扫描结束后的收尾：条目按体积降序，并截断到上限。"""
        self.items.sort(key=lambda i: i.size, reverse=True)
        if len(self.items) > config.MAX_ITEMS_PER_CATEGORY:
            self.items = self.items[: config.MAX_ITEMS_PER_CATEGORY]
            self.warnings.append({
                "code": "truncated",
                "message": "条目过多，仅返回体积最大的 %d 项"
                           % config.MAX_ITEMS_PER_CATEGORY,
                "guide": "可在类目内逐项勾选后清理，不影响结果准确性",
            })
        self.item_count = len(self.items)
        self.size = sum(i.size for i in self.items)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 API 响应使用的字典。"""
        return {
            "category_id": self.category_id,
            "name": self.name,
            "group": self.group,
            "risk": self.risk,
            "risk_text": config.RISK_TEXT.get(self.risk, self.risk),
            "risk_color": config.RISK_COLOR.get(self.risk, "#8A8A8A"),
            "desc": self.desc,
            "note": self.note,
            "size": self.size,
            "item_count": self.item_count,
            "percent": self.percent,
            "status": self.status,
            "status_text": _STATUS_TEXT.get(self.status, self.status),
            "selected": self.selected,
            "privacy_sensitive": self.privacy_sensitive,
            "needs_confirm_text": self.needs_confirm_text,
            "keep_dir": self.keep_dir,
            "resolved_roots": list(self.resolved_roots),
            "warnings": list(self.warnings),
            "items": [i.to_dict() for i in self.items],
        }


_STATUS_TEXT: Dict[str, str] = {
    "ok": "正常",
    "not_found": "未检测到 / 未安装",
    "permission_denied": "无访问权限",
}


class Scanner:
    """扫描引擎（可由 ``sandbox_root`` 完全沙箱化）。

    Args:
        sandbox_root: 沙箱根路径；非 None 时类目根被重映射到沙箱内。
        home_dir: 真实主目录（用于 ``~`` 展开），默认 ``config.HOME``。
        concurrency: 并发线程数，默认 4。
        timeout_sec: 扫描超时秒数，默认 300。
        registry: 类目注册表，默认全局 :data:`REGISTRY`。
    """

    def __init__(self, sandbox_root: Optional[str] = None,
                 home_dir: Optional[str] = None,
                 concurrency: int = config.SCAN_DEFAULT_CONCURRENCY,
                 timeout_sec: int = config.SCAN_TIMEOUT_SEC,
                 registry: Optional[CategoryRegistry] = None) -> None:
        self._sandbox_root: Optional[str] = (
            config.canonical_path(sandbox_root) if sandbox_root else None)
        self._home: str = config.canonical_path(home_dir or config.HOME)
        self._eff_home: str = self._sandbox_root or self._home
        self._concurrency: int = max(1, int(concurrency or 1))
        self._timeout_sec: int = max(30, int(timeout_sec or 30))
        self._registry: CategoryRegistry = registry or REGISTRY

        self._settings: Dict[str, Any] = dict(config.DEFAULT_SETTINGS)
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._lock = threading.RLock()

        self._scan_id: str = ""
        self._status: str = config.JOB_IDLE
        self._started_ts: float = 0.0
        self._elapsed_ms: int = 0
        self._timed_out: bool = False
        self._message: str = ""

        self._results: Dict[str, CategoryResult] = {}
        self._order: List[str] = []
        self._progress: Dict[str, Any] = self._empty_progress()

    # ---------------------------------------------------------------- 状态
    @staticmethod
    def _empty_progress() -> Dict[str, Any]:
        return {
            "status": config.JOB_IDLE,
            "scanned_dirs": 0,
            "scanned_entries": 0,
            "scanned_bytes": 0,
            "found_bytes": 0,
            "found_items": 0,
            "elapsed_ms": 0,
            "current_category": None,
            "current_category_name": None,
            "message": "",
        }

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """更新扫描使用的设置（并发数、超时、类目开关、下载阈值等）。"""
        if isinstance(settings, dict) and settings:
            self._settings = config.deep_merge(self._settings, settings)
            scan_conf = self._settings.get("scan") or {}
            self._concurrency = config.clamp_int(
                scan_conf.get("concurrency", self._concurrency), 1, 16,
                self._concurrency)
            self._timeout_sec = config.clamp_int(
                scan_conf.get("timeout_sec", self._timeout_sec), 30, 3600,
                self._timeout_sec)

    @property
    def settings(self) -> Dict[str, Any]:
        """当前生效的设置字典。"""
        return self._settings

    @property
    def home(self) -> str:
        """用于 in_home 判定的「生效主目录」（沙箱模式下为沙箱根）。"""
        return self._eff_home

    @property
    def sandbox_root(self) -> Optional[str]:
        """沙箱根路径，生产环境为 None。"""
        return self._sandbox_root

    def is_running(self) -> bool:
        """是否正在扫描。"""
        return self._running

    # ---------------------------------------------------------------- 启动 / 取消
    def start(self, category_ids: Optional[List[str]] = None,
              settings: Optional[Dict[str, Any]] = None) -> str:
        """启动一次后台扫描。

        Args:
            category_ids: 指定类目 id 列表；为空时使用全部启用类目。
            settings: 本次扫描使用的设置；为空沿用上次设置。

        Returns:
            本次扫描的 ``scan_id``。

        Raises:
            config.ApiError: 已有扫描在运行时抛出 ``SCAN_RUNNING``。
        """
        if settings:
            self.set_settings(settings)
        with self._lock:
            if self._running:
                raise config.ApiError(config.ErrorCode.SCAN_RUNNING)
            self._cancel = threading.Event()
            self._results = {}
            self._order = []
            self._timed_out = False
            self._message = ""
            self._started_ts = time.time()
            self._scan_id = "scan_%d" % int(self._started_ts)
            self._status = config.JOB_RUNNING
            self._progress = self._empty_progress()
            self._progress["status"] = config.JOB_RUNNING
            self._running = True
            targets = self._targets(category_ids)
            self._order = [c.id for c in targets]
            self._thread = threading.Thread(
                target=self._run, args=(targets,),
                name="scanner-%s" % self._scan_id, daemon=True)
            self._thread.start()
        return self._scan_id

    def cancel(self) -> bool:
        """请求取消当前扫描（≤3 秒内生效）。

        Returns:
            是否成功发出取消请求。
        """
        if not self._running:
            return False
        self._cancel.set()
        return True

    # ---------------------------------------------------------------- 结果访问
    def progress(self) -> Dict[str, Any]:
        """返回当前扫描进度快照。"""
        with self._lock:
            data = dict(self._progress)
        if self._running:
            data["elapsed_ms"] = int((time.time() - self._started_ts) * 1000)
        data["status"] = self._status if not self._running else config.JOB_RUNNING
        return data

    def result(self) -> Dict[str, Any]:
        """返回扫描结果汇总（含各类目百分比）。

        Raises:
            config.ApiError: 尚无扫描结果时抛出 ``SCAN_NOT_RUNNING``。
        """
        with self._lock:
            if not self._order and self._status == config.JOB_IDLE:
                raise config.ApiError(config.ErrorCode.SCAN_NOT_RUNNING)
            elapsed = self._elapsed_ms or int(
                (time.time() - self._started_ts) * 1000) if self._started_ts else 0
            categories = [self._results[cid].to_dict()
                          for cid in self._order if cid in self._results]
            data = {
                "scan_id": self._scan_id,
                "status": self._status,
                "elapsed_ms": int(elapsed),
                "message": self._message,
                "timed_out": self._timed_out,
                "totals": self._totals_locked(),
                "categories": categories,
            }
            if self._timed_out:
                data["message"] = data["message"] or "扫描超时，可缩小范围重试"
        return data

    def _totals_locked(self) -> Dict[str, int]:
        total_bytes = 0
        item_count = 0
        for cid in self._order:
            res = self._results.get(cid)
            if res is None or not res.selected:
                continue
            total_bytes += res.size
            item_count += res.item_count
        return {"reclaimable_bytes": total_bytes, "item_count": item_count}

    def all_items(self) -> Dict[str, ScanItem]:
        """返回 ``{item_id: ScanItem}`` 全量映射（供清理器按 id 回查）。"""
        items: Dict[str, ScanItem] = {}
        with self._lock:
            for res in self._results.values():
                for item in res.items:
                    items[item.id] = item
        return items

    def resolve_items(self, ids: List[str]) -> Tuple[List[Dict[str, Any]],
                                                     List[str]]:
        """按 item id 回查条目，避免接受客户端传入的裸路径。

        Args:
            ids: 前端传来的 id 列表。

        Returns:
            ``(找到的条目字典列表, 未找到的 id 列表)``。
        """
        mapping = self.all_items()
        found: List[Dict[str, Any]] = []
        missing: List[str] = []
        for oid in ids or []:
            item = mapping.get(str(oid))
            if item is None:
                missing.append(str(oid))
            else:
                found.append(item.to_dict())
        return found, missing

    def whitelist_roots(self) -> List[str]:
        """返回本次扫描对应的白名单根路径（沙箱感知）。"""
        return self._registry.whitelist_roots(
            self._settings, self._home, self._sandbox_root)

    # ---------------------------------------------------------------- 内部
    def _targets(self, category_ids: Optional[List[str]]) -> List[CategoryDef]:
        """计算本次需要扫描的类目列表。

        未显式指定时取全部「启用」类目；``external_volumes`` 额外受
        ``scan_external_volumes`` 总开关约束（Q2：默认只扫启动盘）。

        显式传入 ``category_ids`` 时按**全量类目清单**解析（而不是只看启用项）：
        前端已经做过一遍开关过滤，此处再过滤一次会让「临时想看某个默认关闭的
        类目」永远拿不到结果（探活/未安装提示也无从展示）。
        """
        if not category_ids:
            enabled = self._registry.enabled(self._settings)
            if self._settings.get("scan_external_volumes", False):
                return list(enabled)
            return [c for c in enabled if c.id != "external_volumes"]
        result: List[CategoryDef] = []
        for cid in category_ids:
            cat = self._registry.get(str(cid))
            if cat is not None and cat not in result:
                result.append(cat)
        return result

    def _timed_out_now(self) -> bool:
        if self._timeout_sec <= 0:
            return False
        return (time.time() - self._started_ts) > self._timeout_sec

    def _run(self, targets: List[CategoryDef]) -> None:
        """扫描主流程：并发遍历各类目，汇总结果后计算百分比。"""
        started = time.time()
        workers = max(1, min(self._concurrency, max(1, len(targets))))
        try:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="scan") as pool:
                futures = {pool.submit(self._scan_category, cat): cat.id
                           for cat in targets}
                for future, cid in futures.items():
                    cat = self._registry.get(cid)
                    try:
                        result = future.result()
                    except config.ApiError:
                        raise
                    except Exception as exc:                 # 绝不向上抛
                        result = CategoryResult(cat or self._registry.all()[0])
                        result.status = "ok"
                        result.warnings.append({
                            "code": "io_error",
                            "message": "扫描该类目时发生异常，已跳过",
                            "guide": str(exc),
                        })
                    with self._lock:
                        self._results[cid] = result
        finally:
            with self._lock:
                self._compute_percents()
                self._elapsed_ms = int((time.time() - started) * 1000)
                if self._cancel.is_set():
                    self._status = config.JOB_ABORTED
                elif self._timed_out:
                    self._status = config.JOB_ABORTED
                    self._timed_out = True
                    self._message = "扫描超时，可缩小范围重试"
                else:
                    self._status = config.JOB_DONE
                self._running = False
                self._progress["status"] = self._status
                self._progress["elapsed_ms"] = self._elapsed_ms

    def _compute_percents(self) -> None:
        """按「已勾选且通过安全校验」的类目计算百分比（STAT-02）。"""
        denominator = 0
        for cid in self._order:
            res = self._results.get(cid)
            if res is None or not res.selected:
                continue
            denominator += res.size
        if denominator <= 0:
            for res in self._results.values():
                res.percent = 0.0
            return
        for res in self._results.values():
            if not res.selected:
                res.percent = 0.0
                continue
            res.percent = round(res.size * 100.0 / denominator, 1)

    def _bump(self, dirs: int = 0, entries: int = 0, base_bytes: int = 0,
              found_bytes: int = 0, found_items: int = 0) -> None:
        """累加全局进度计数器。"""
        with self._lock:
            self._progress["scanned_dirs"] += dirs
            self._progress["scanned_entries"] += entries
            self._progress["scanned_bytes"] += base_bytes
            self._progress["found_bytes"] += found_bytes
            self._progress["found_items"] += found_items
            if self._started_ts:
                self._progress["elapsed_ms"] = int(
                    (time.time() - self._started_ts) * 1000)

    def _aborted(self) -> bool:
        """是否应当立即中止当前遍历。"""
        if self._cancel.is_set():
            return True
        if self._timed_out_now():
            self._timed_out = True
            self._cancel.set()
            return True
        return False

    # ---------------------------------------------------------------- 类目扫描
    def _scan_category(self, cat: CategoryDef) -> CategoryResult:
        """扫描单个类目，内部绝不向外抛异常。"""
        result = CategoryResult(cat)
        result.selected = self._registry.is_selected(cat, self._settings)
        probe = cat.probe(self._home, self._sandbox_root)
        result.probe_status = probe["status"]
        result.resolved_roots = probe["roots"]
        with self._lock:
            self._progress["current_category"] = cat.id
            self._progress["current_category_name"] = cat.name

        if not probe["roots"]:
            result.status = "not_found"
            result.warnings.append({
                "code": "not_found",
                "message": "未检测到「%s」，可能尚未安装或已被清理" % cat.name,
                "guide": "这是正常现象，不影响其他类目的扫描",
            })
            return result

        if probe["status"] == "permission_denied":
            result.status = "permission_denied"
            result.warnings.append({
                "code": "permission_denied",
                "count": len(probe["roots"]),
                "message": "%d 个目录无权限访问" % len(probe["roots"]),
                "guide": config.PERMISSION_GUIDE,
            })
        if probe["missing"]:
            result.warnings.append({
                "code": "not_found",
                "count": len(probe["missing"]),
                "message": "有 %d 个子路径不存在，已自动跳过" % len(
                    probe["missing"]),
                "guide": "、".join(probe["missing"][:3]),
            })

        root_keys = {os.path.realpath(r) for r in probe["roots"]}
        permission_errors = 0
        for root in probe["roots"]:
            if self._aborted():
                break
            try:
                children = self._list_children(root)
            except PermissionError:
                permission_errors += 1
                continue
            except FileNotFoundError:
                continue
            except OSError:
                permission_errors += 1
                continue

            if cat.item_depth == 0:
                candidates: List[Tuple[str, bool]] = [(root, True)]
            else:
                candidates = children

            for path, is_real_dir in candidates:
                if self._aborted():
                    break
                item, item_errors = self._build_item(
                    path, is_real_dir, cat, root_keys)
                permission_errors += item_errors
                if item is None:
                    continue
                result.add(item)
                self._bump(found_bytes=item.size, found_items=1)

        if permission_errors:
            result.status = "permission_denied"
            if not any(w.get("code") == "permission_denied"
                       for w in result.warnings):
                result.warnings.append({
                    "code": "permission_denied",
                    "count": permission_errors,
                    "message": "%d 个目录无权限访问，已自动跳过"
                               % permission_errors,
                    "guide": config.PERMISSION_GUIDE,
                })
        result.finalize()
        return result

    def _build_item(self, path: str, is_real_dir: bool, cat: CategoryDef,
                    root_keys: set) -> Tuple[Optional[ScanItem], int]:
        """为一个候选路径构造 ScanItem。

        Args:
            path: 候选绝对路径。
            is_real_dir: 是否真实目录（符号链接视为 False）。
            cat: 所属类目定义。
            root_keys: 该类目根路径的 realpath 集合（用于 ``keep_dir`` 判定）。

        Returns:
            ``(条目或 None, 递归统计时遇到的权限错误次数)``。
            权限错误必须回传给调用方汇总为类目级 warning（D3：权限不足
            只降级、不中断），否则用户会看到「扫描完成但体积明显偏小」
            却没有任何解释。
        """
        try:
            st = os.lstat(path)
        except OSError:
            return None, 0
        is_link = stat.S_ISLNK(st.st_mode)
        item_type = "dir" if (is_real_dir and not is_link) else "file"
        mtime = float(st.st_mtime)
        atime = float(st.st_atime)

        perm_errors = 0
        if item_type == "dir":
            size, dir_mtime, dir_atime, perm_errors = self._dir_stat(path)
            mtime = max(mtime, dir_mtime)
            atime = max(atime, dir_atime)
        else:
            size = int(st.st_size)

        if not cat.match_item(path, size, mtime, atime, self._settings,
                              item_type == "dir"):
            return None, perm_errors
        if size <= 0 and item_type == "file":
            return None, perm_errors

        in_home = config.is_within(path, self._eff_home)
        flags: List[str] = []
        if cat.privacy_sensitive:
            flags.append("privacy")
        if not in_home:
            flags.append("outside_home")
        if cat.needs_confirm_text:
            flags.append("confirm_text")
        if perm_errors:
            flags.append("partial_permission")
        keep_dir = bool(cat.keep_dir and os.path.realpath(path) in root_keys)
        item = ScanItem(
            path=os.path.normpath(path),
            category_id=cat.id,
            size=size,
            item_type=item_type,
            mtime=mtime,
            atime=atime,
            risk=cat.risk,
            in_home=in_home,
            selected=self._registry.is_selected(cat, self._settings),
            flags=flags,
            keep_dir=keep_dir,
        )
        return item, perm_errors

    def _list_children(self, root: str) -> List[Tuple[str, bool]]:
        """列出根目录的直接子项，返回 ``[(path, 是否真实目录), ...]``。

        Raises:
            PermissionError / OSError: 交由调用方降级处理。
        """
        children: List[Tuple[str, bool]] = []
        with os.scandir(root) as iterator:
            for entry in iterator:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                children.append((entry.path, stat.S_ISDIR(st.st_mode)))
        return children

    def _dir_stat(self, path: str) -> Tuple[int, float, float, int]:
        """递归统计目录体积（不跟随符号链接）。

        Returns:
            ``(总字节数, 最大 mtime, 最大 atime, 权限错误次数)``
        """
        total = 0
        mtime = 0.0
        atime = 0.0
        errors = 0
        entries_seen = 0
        stack: List[str] = [path]
        while stack:
            if self._aborted():
                break
            current = stack.pop()
            try:
                iterator = os.scandir(current)
            except PermissionError:
                errors += 1
                continue
            except FileNotFoundError:
                continue
            except NotADirectoryError:
                continue
            except OSError:
                errors += 1
                continue
            try:
                with iterator as it:
                    entries = list(it)
            except OSError:
                errors += 1
                continue
            self._bump(dirs=1, entries=len(entries))
            for entry in entries:
                entries_seen += 1
                if entries_seen % config.SCAN_PROGRESS_REPORT_EVERY == 0 \
                        and self._aborted():
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    errors += 1
                    continue
                total += int(st.st_size)
                # lstat 模式下符号链接不会被识别为目录，天然不递归
                if stat.S_ISDIR(st.st_mode):
                    stack.append(entry.path)
                else:
                    if st.st_mtime > mtime:
                        mtime = st.st_mtime
                    if st.st_atime > atime:
                        atime = st.st_atime
        return total, mtime, atime, errors
