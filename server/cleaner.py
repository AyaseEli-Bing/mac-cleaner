# -*- coding: utf-8 -*-
"""清理引擎。

安全红线（不能被绕过）：
  1. 外部请求**只能携带 item_id**，服务端自行回查 ScanItem（不接受裸路径）；
  2. 每条路径在执行前逐条走过 :class:`~server.safety.SafetyGuard` 五道关卡；
  3. 预览（dry-run）阶段生成 ``preview_token`` 快照，执行时必须携带且未过期，
     防止预览后路径被替换（TOCTOU）。

默认模式为「移到废纸篓」（``shutil.move`` 到 ``~/.Trash``，零第三方依赖）。
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, safety
from .categories import CategoryRegistry, REGISTRY


def _ts_to_iso(timestamp: float) -> str:
    """把 UNIX 时间戳转换为 ISO 8601 本地时间字符串。"""
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(
        timespec="seconds")


class CleanPreview:
    """一次 dry-run 预览的服务端快照。"""

    def __init__(self, token: str, items: List[Dict[str, Any]], mode: str,
                 ttl: int = config.PREVIEW_TTL_SEC) -> None:
        self.token: str = token
        self.items: List[Dict[str, Any]] = items
        self.mode: str = mode
        self.created_at: float = time.time()
        self.expires_at: float = self.created_at + max(30, ttl)
        self.requires: Dict[str, Any] = {}

    def expired(self, now: Optional[float] = None) -> bool:
        """判断该预览是否已过期（TTL 默认 10 分钟）。"""
        return (now if now is not None else time.time()) > self.expires_at


class Cleaner:
    """清理引擎（后台执行、可中断、逐项回执）。

    Args:
        store: 持久化门面，用于写历史与留痕。
        settings: 当前设置字典。
        registry: 类目注册表。
        sandbox_root: 沙箱根；非 None 时废纸篓与白名单均在沙箱内。
        home_dir: 真实主目录。
    """

    def __init__(self, store: Any,
                 settings: Optional[Dict[str, Any]] = None,
                 registry: Optional[CategoryRegistry] = None,
                 sandbox_root: Optional[str] = None,
                 home_dir: Optional[str] = None) -> None:
        self._store = store
        self._registry: CategoryRegistry = registry or REGISTRY
        self._settings: Dict[str, Any] = config.deep_merge(
            config.DEFAULT_SETTINGS, settings or {})
        self._sandbox_root: Optional[str] = (
            config.canonical_path(sandbox_root) if sandbox_root else None)
        self._home: str = config.canonical_path(home_dir or config.HOME)
        self._eff_home: str = self._sandbox_root or self._home

        self._guard: safety.SafetyGuard = self._build_guard()
        self._previews: Dict[str, CleanPreview] = {}
        self._preview_lock = threading.RLock()
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._job: Dict[str, Any] = self._empty_job()

    # ---------------------------------------------------------------- 基础
    @staticmethod
    def _empty_job() -> Dict[str, Any]:
        return {
            "job_id": "",
            "status": config.JOB_IDLE,
            "mode": config.MODE_TRASH,
            "total": 0,
            "processed": 0,
            "current_path": None,
            "freed_bytes": 0,
            "planned_bytes": 0,
            "success": 0,
            "skipped": 0,
            "failed": 0,
            "started_at": None,
            "finished_at": None,
            "duration_ms": 0,
            "history_id": None,
            "categories": [],
            "results": [],
        }

    @property
    def guard(self) -> safety.SafetyGuard:
        """当前生效的安全守卫。"""
        return self._guard

    @property
    def trash_root(self) -> str:
        """废纸篓目录根（沙箱模式下位于沙箱内）。"""
        base = self._sandbox_root or self._home
        return os.path.join(base, config.TRASH_DIR_NAME)

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """更新设置并重建安全守卫（白名单随类目开关变化）。"""
        if isinstance(settings, dict) and settings:
            self._settings = config.deep_merge(self._settings, settings)
            self._guard = self._build_guard()

    def _build_guard(self) -> safety.SafetyGuard:
        """按当前设置装配安全守卫，并把留痕接到 Store.audit。"""
        audit: Optional[Callable[[str, str, str], None]] = None
        if self._store is not None:
            audit = lambda path, code, message: self._store.audit(  # noqa: E731
                path, code, message)
        return safety.build_guard(
            self._settings, self._registry, self._home,
            self._sandbox_root, audit=audit)

    def _cat_map(self) -> Dict[str, Any]:
        """返回 ``{category_id: CategoryDef}`` 映射。"""
        return {c.id: c for c in self._registry.all()}

    # ---------------------------------------------------------------- 预览
    def preview(self, items: List[Dict[str, Any]],
                mode: Optional[str] = None) -> Dict[str, Any]:
        """dry-run 预览：不产生任何针对目标路径的写操作（CLEAN-01）。

        Args:
            items: ScanItem 字典列表（服务端按 id 回查得到，非用户输入路径）。
            mode: ``trash`` / ``delete``，为空时沿用设置默认模式。

        Returns:
            符合架构 3.5 节的预览响应 ``data`` 结构。

        Raises:
            config.ApiError: 超限时抛出 ``LIMIT_EXCEEDED``。
        """
        mode = self._normalize_mode(mode)
        items = list(items or [])
        self._purge_previews()

        if not items:
            raise config.ApiError(
                config.ErrorCode.BAD_REQUEST, "请先勾选要清理的项目")

        limits = self._settings.get("limits") or {}
        limit_result = self._guard.check_limits(items, limits)
        if not limit_result.ok:
            raise config.ApiError(
                config.ErrorCode.LIMIT_EXCEEDED,
                limit_result.message,
                {"reason_code": limit_result.code})

        ok_items: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(str(item.get("category_id") or ""),
                               []).append(item)

        for category_id, group in grouped.items():
            paths = [str(i.get("path") or "") for i in group]
            # 预览阶段跳过关卡五（占用检测），保证 dry-run 绝对只读
            checks = self._guard.check_batch(
                paths, category_id, max_workers=4, skip_in_use=True)
            for item in group:
                path = str(item.get("path") or "")
                result = checks.get(path)
                if result is None or not result.ok:
                    code = result.code if result else "not_in_whitelist"
                    blocked.append({
                        "item_id": item.get("id"),
                        "path": path,
                        "category_id": category_id,
                        "code": code,
                        "message": result.message if result else "不在白名单内",
                    })
                else:
                    ok_items.append(item)

        if not ok_items:
            raise config.ApiError(
                config.ErrorCode.SAFETY_BLOCKED,
                "所选项目全部被安全策略拦截，未执行任何清理",
                {"blocked": blocked})

        summary = self._summarize(ok_items)
        in_home = summary["in_home"]
        outside = summary["outside_home"]
        cat_map = self._cat_map()
        needs_text = False
        for item in ok_items:
            cat = cat_map.get(str(item.get("category_id") or ""))
            if cat is not None and cat.needs_confirm_text:
                needs_text = True
                break
        requires = {
            "confirm_outside_home": bool(outside["items"]),
            "confirm_text": config.CONFIRM_TEXT if needs_text else None,
        }

        token = self._new_token(ok_items)
        snapshot = CleanPreview(token, ok_items, mode)
        snapshot.requires = dict(requires)
        with self._preview_lock:
            self._previews[token] = snapshot

        return {
            "preview_token": token,
            "mode": mode,
            "expires_at": _ts_to_iso(snapshot.expires_at),
            "summary": {
                "planned_items": len(ok_items),
                "planned_bytes": summary["bytes"],
                "category_count": summary["category_count"],
                "in_home": in_home,
                "outside_home": outside,
                "blocked": blocked,
            },
            "top_paths": summary["top_paths"],
            "requires": requires,
            "limit_ok": True,
        }

    # ---------------------------------------------------------------- 执行
    def execute(self, preview_token: str, mode: Optional[str] = None,
                confirm_outside_home: bool = False,
                confirm_text: Optional[str] = None) -> str:
        """按预览快照启动后台清理任务。

        Returns:
            ``job_id``。

        Raises:
            config.ApiError: 令牌失效 ``INVALID_PREVIEW_TOKEN``、
                清理进行中 ``CLEAN_RUNNING``、确认不足 ``CONFIRM_REQUIRED``。
        """
        mode = self._normalize_mode(mode)
        with self._preview_lock:
            preview = self._previews.get(str(preview_token or ""))
            if preview is None or preview.expired():
                if preview is not None:
                    self._previews.pop(preview_token, None)
                raise config.ApiError(config.ErrorCode.INVALID_PREVIEW_TOKEN)
        with self._lock:
            if self._job.get("status") == config.JOB_RUNNING:
                raise config.ApiError(config.ErrorCode.CLEAN_RUNNING)

        requires = preview.requires or {}
        if requires.get("confirm_outside_home") and not confirm_outside_home:
            raise config.ApiError(
                config.ErrorCode.CONFIRM_REQUIRED,
                "本次清理包含主目录之外的项目，请先勾选确认后再执行")
        expected = requires.get("confirm_text")
        if expected and (confirm_text or "").strip() != expected:
            raise config.ApiError(
                config.ErrorCode.CONFIRM_REQUIRED,
                "高危类目需要输入「%s」才能执行清理" % expected)

        started = time.time()
        with self._lock:
            self._cancel = threading.Event()
            self._started_ts = started
            self._job = self._empty_job()
            self._job.update({
                "job_id": "job_%d" % int(started),
                "status": config.JOB_RUNNING,
                "mode": mode,
                "total": len(preview.items),
                "planned_bytes": sum(int(i.get("size", 0) or 0)
                                     for i in preview.items),
                "started_at": config.now_iso(),
                "categories": sorted({str(i.get("category_id") or "")
                                      for i in preview.items}),
            })
            job_id = self._job["job_id"]
            self._thread = threading.Thread(
                target=self._run, args=(preview, mode),
                name="cleaner-%s" % job_id, daemon=True)
            self._thread.start()
        return job_id

    def cancel(self) -> bool:
        """请求中断当前清理任务（当前项完成后即停）。"""
        with self._lock:
            if self._job.get("status") != config.JOB_RUNNING:
                return False
        self._cancel.set()
        return True

    def progress(self) -> Dict[str, Any]:
        """返回当前清理任务进度快照。"""
        with self._lock:
            job = dict(self._job)
            job["results"] = list(self._job["results"])
        if job["status"] == config.JOB_RUNNING and self._started_ts:
            job["duration_ms"] = int((time.time() - self._started_ts) * 1000)
        return job

    def get_preview(self, token: str) -> Optional[CleanPreview]:
        """取出未过期的预览快照，供 CSV 导出使用。"""
        with self._preview_lock:
            preview = self._previews.get(token)
            if preview is None or preview.expired():
                return None
            return preview

    # ---------------------------------------------------------------- 主循环
    def _run(self, preview: CleanPreview, mode: str) -> None:
        """后台清理主循环：单线程保序，保证中断语义清晰。"""
        started = time.time()
        results: List[Dict[str, Any]] = []
        freed = 0
        success = skipped = failed = 0
        aborted = False

        for item in preview.items:
            if self._cancel.is_set():
                aborted = True
                break
            path = str(item.get("path") or "")
            with self._lock:
                self._job["current_path"] = path
            row = self._apply_one(item, mode)
            results.append(row)
            if row["status"] == config.RESULT_SUCCESS:
                success += 1
                freed += int(row.get("size", 0) or 0)
            elif row["status"] == config.RESULT_SKIPPED:
                skipped += 1
            else:
                failed += 1
            with self._lock:
                self._job["processed"] = len(results)
                self._job["freed_bytes"] = freed
                self._job["success"] = success
                self._job["skipped"] = skipped
                self._job["failed"] = failed
                self._job["results"] = results

        finished = time.time()
        duration_ms = int((finished - started) * 1000)
        status = config.JOB_ABORTED if aborted else config.JOB_DONE
        history_id = self._save_history(preview, mode, results, freed,
                                        success, skipped, failed, status,
                                        duration_ms)
        with self._lock:
            self._job.update({
                "status": status,
                "current_path": None,
                "history_id": history_id,
                "finished_at": config.now_iso(),
                "duration_ms": duration_ms,
                "processed": len(results),
                "freed_bytes": freed,
                "success": success,
                "skipped": skipped,
                "failed": failed,
                "results": results,
            })
        if self._store is not None:
            self._store.log(
                "info", "清理任务完成：成功 %d / 跳过 %d / 失败 %d，释放 %s"
                % (success, skipped, failed, config.format_bytes(freed)))

    def _save_history(self, preview: CleanPreview, mode: str,
                      results: List[Dict[str, Any]], freed: int,
                      success: int, skipped: int, failed: int,
                      status: str, duration_ms: int) -> Optional[int]:
        """写入历史记录并完成明细裁剪。"""
        if self._store is None:
            return None
        record = {
            "started_at": config.now_iso(),
            "finished_at": config.now_iso(),
            "duration_ms": duration_ms,
            "mode": mode,
            "item_count": len(results),
            "success_count": success,
            "skipped_count": skipped,
            "failed_count": failed,
            "freed_bytes": freed,
            # 任务状态 done/aborted/error → 历史状态 completed/aborted/error，
            # Store 的累计释放量按 'completed' 过滤，必须转换后再写库
            "status": config.HISTORY_STATUS_BY_JOB.get(
                status, config.HISTORY_COMPLETED),
            "categories": sorted({str(i.get("category_id") or "")
                                  for i in preview.items}),
        }
        items = [{
            "path": r.get("path"),
            "size": int(r.get("size", 0) or 0),
            "category_id": r.get("category_id"),
            "result": r.get("status"),
            "reason_code": r.get("reason_code"),
            "reason_text": r.get("reason_text"),
        } for r in results]
        try:
            return self._store.save_history(record, items)
        except Exception as exc:                            # pragma: no cover
            self._store.log("error", "写入清理历史失败：%s" % exc)
            return None

    # ---------------------------------------------------------------- 单条
    def _apply_one(self, item: Dict[str, Any], mode: str) -> Dict[str, Any]:
        """对单条路径执行完整五关卡 + 实际操作。

        Returns:
            逐项回执字典 ``{item_id, path, size, category_id, status,
            reason_code, reason_text, protected_by_sip}``。
        """
        path = str(item.get("path") or "")
        category_id = str(item.get("category_id") or "")
        size = int(item.get("size", 0) or 0)
        base = {
            "item_id": item.get("id"),
            "path": path,
            "size": size,
            "category_id": category_id,
            "status": config.RESULT_SKIPPED,
            "reason_code": None,
            "reason_text": None,
            "protected_by_sip": False,
        }

        gate = self._guard.check(path, category_id, skip_in_use=False)
        if not gate.ok:
            base.update({
                "reason_code": gate.code,
                "reason_text": gate.message,
                "protected_by_sip": gate.code == "sip",
            })
            return base

        norm = gate.normalized_path
        keep_dir = bool(item.get("keep_dir"))
        try:
            if mode == config.MODE_TRASH:
                self._move_to_trash(norm)
            else:
                self._delete_path(norm, keep_dir)
        except PermissionError as exc:
            base.update({
                "reason_code": "permission_denied",
                "reason_text": config.REASON_TEXT["permission_denied"],
            })
            if self._store is not None:
                self._store.audit(path, "permission_denied", str(exc))
            return base
        except OSError as exc:
            # 注意：下面的分支必须全部走完再 return —— 曾经把 ``return base``
            # 写在 ENOENT 判断之前，导致「文件已消失」被误报成「被占用」，
            # 且 ENOENT 与 io_error 分支永远不可达。
            if exc.errno == errno.ENOENT:
                base.update({
                    "reason_code": "not_found",
                    "reason_text": config.REASON_TEXT["not_found"],
                })
                if self._store is not None:
                    self._store.audit(path, "not_found", str(exc))
                return base
            if exc.errno in (errno.EBUSY, errno.ETXTBSY, errno.EAGAIN,
                             errno.EWOULDBLOCK):
                base.update({
                    "reason_code": "in_use",
                    "reason_text": config.REASON_TEXT["in_use"],
                })
                if self._store is not None:
                    self._store.audit(path, "in_use", str(exc))
                return base
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                base.update({
                    "reason_code": "permission_denied",
                    "reason_text": config.REASON_TEXT["permission_denied"],
                })
                if self._store is not None:
                    self._store.audit(path, "permission_denied", str(exc))
                return base
            base.update({
                "status": config.RESULT_FAILED,
                "reason_code": "io_error",
                "reason_text": "%s（%s）" % (
                    config.REASON_TEXT["io_error"], exc.strerror or exc),
            })
            if self._store is not None:
                self._store.audit(path, "io_error", str(exc))
            return base
        except Exception as exc:                            # pragma: no cover
            base.update({
                "status": config.RESULT_FAILED,
                "reason_code": "io_error",
                "reason_text": config.REASON_TEXT["io_error"],
            })
            if self._store is not None:
                self._store.audit(path, "io_error", str(exc))
            return base

        base["status"] = config.RESULT_SUCCESS
        return base

    # ---------------------------------------------------------------- 实际动作
    def _move_to_trash(self, path: str) -> None:
        """把路径移动到 ``~/.Trash/<basename>.<uniq>``（D4，零第三方依赖）。"""
        trash = self.trash_root
        os.makedirs(trash, exist_ok=True)
        base_name = os.path.basename(path.rstrip("/")) or "item"
        target = os.path.join(trash, "%s.%d" % (base_name, int(time.time())))
        suffix = 1
        candidate = target
        while os.path.exists(candidate):
            candidate = "%s_%d" % (target, suffix)
            suffix += 1
        shutil.move(path, candidate)

    def _delete_path(self, path: str, keep_dir: bool) -> None:
        """删除路径；``keep_dir`` 为真时只清内容、保留目录本身（CLEAN-09）。"""
        if os.path.islink(path):
            os.remove(path)
            return
        if stat.S_ISDIR(os.lstat(path).st_mode):
            if keep_dir:
                self._clear_dir_contents(path)
            else:
                shutil.rmtree(path)
            return
        os.remove(path)

    def _clear_dir_contents(self, path: str) -> None:
        """清空目录内的全部条目（含子目录），保留目录本身与其属性。"""
        with os.scandir(path) as iterator:
            entries = list(iterator)
        for entry in entries:
            target = entry.path
            try:
                if stat.S_ISDIR(os.lstat(target).st_mode) \
                        and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
            except FileNotFoundError:
                continue

    # ---------------------------------------------------------------- 辅助
    def _normalize_mode(self, mode: Optional[str]) -> str:
        """规范化清理模式：非法值一律回落到默认设置。"""
        if mode in (config.MODE_TRASH, config.MODE_DELETE):
            return mode
        return self._settings.get("mode", config.MODE_TRASH)

    @staticmethod
    def _summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """统计预览摘要：分区字节数、条目数、类目数、Top5 路径。"""
        in_home = {"items": 0, "bytes": 0}
        outside = {"items": 0, "bytes": 0}
        categories = set()
        total = 0
        for item in items:
            size = int(item.get("size", 0) or 0)
            total += size
            bucket = in_home if item.get("in_home", True) else outside
            bucket["items"] += 1
            bucket["bytes"] += size
            categories.add(str(item.get("category_id") or ""))
        top = sorted(items, key=lambda i: int(i.get("size", 0) or 0),
                     reverse=True)[:5]
        home = config.HOME
        return {
            "bytes": total,
            "items": len(items),
            "category_count": len(categories),
            "in_home": in_home,
            "outside_home": outside,
            "top_paths": [{"path": config.abbreviate_home(
                str(i.get("path") or ""), home), "size": int(
                    i.get("size", 0) or 0)} for i in top],
        }

    def _purge_previews(self) -> None:
        """清理已过期的预览快照（防止内存泄漏）。"""
        now = time.time()
        with self._preview_lock:
            expired = [token for token, pv in self._previews.items()
                       if pv.expired(now)]
            for token in expired:
                self._previews.pop(token, None)

    def _new_token(self, items: List[Dict[str, Any]]) -> str:
        """生成预览令牌 ``pv_<时间戳>_<批次指纹>``。

        指纹由「类目 + 路径 + 体积」的有序摘要得出，用途有二：
          1. 保证同一批条目在 TTL 内拿到可读、可追溯的令牌；
          2. 执行阶段凭令牌取回服务端快照（而不是信任客户端回传的路径），
             从根上切断 TOCTOU 与「客户端伪造路径」两条攻击路径。

        Args:
            items: 已通过安全关卡、即将进入预览快照的条目字典列表。

        Returns:
            形如 ``pv_1758268800_9f3a1c2b`` 的令牌字符串。
        """
        digest = hashlib.sha1()
        for item in items:
            digest.update(("%s|%s|%s;" % (
                item.get("category_id") or "",
                item.get("path") or "",
                int(item.get("size", 0) or 0),
            )).encode("utf-8", "surrogateescape"))
        return "pv_%d_%s" % (int(time.time()), digest.hexdigest()[:8])
