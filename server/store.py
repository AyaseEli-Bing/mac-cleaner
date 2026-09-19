# -*- coding: utf-8 -*-
"""持久化层：SQLite 历史 / 设置 / 日志。

职责：
  * 建表（clean_history / clean_history_item / settings）；
  * 历史与明细读写，明细自动裁剪到最近 :data:`config.HISTORY_DETAIL_KEEP` 次；
  * 设置 KV（JSON 值，读取时与默认值深合并）；
  * ``audit.log`` 安全留痕与 ``app.log`` 运行日志（按大小滚动，见 A6）。

并发策略：单连接 + ``check_same_thread=False`` + 写锁串行化（架构 10.5 第 7 条）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS clean_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    mode           TEXT    NOT NULL,
    item_count     INTEGER NOT NULL DEFAULT 0,
    success_count  INTEGER NOT NULL DEFAULT 0,
    skipped_count  INTEGER NOT NULL DEFAULT 0,
    failed_count   INTEGER NOT NULL DEFAULT 0,
    freed_bytes    INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL,
    categories     TEXT    NOT NULL DEFAULT '[]',
    has_detail     INTEGER NOT NULL DEFAULT 1
);
"""

_CREATE_HISTORY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_history_started "
    "ON clean_history(started_at DESC);"
)

_CREATE_ITEM = """
CREATE TABLE IF NOT EXISTS clean_history_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id  INTEGER NOT NULL REFERENCES clean_history(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    category_id TEXT    NOT NULL,
    result      TEXT    NOT NULL,
    reason_code TEXT,
    reason_text TEXT
);
"""

_CREATE_ITEM_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_item_history "
    "ON clean_history_item(history_id);"
)

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 最近 N 次之外的明细归档（Q7 / HIST-04）
_PRUNE_ITEMS = """
DELETE FROM clean_history_item
WHERE history_id NOT IN (
    SELECT id FROM clean_history ORDER BY started_at DESC LIMIT ?
);
"""

_PRUNE_HISTORY_FLAG = """
UPDATE clean_history SET has_detail = 0
WHERE id NOT IN (
    SELECT id FROM clean_history ORDER BY started_at DESC LIMIT ?
);
"""


class Store:
    """SQLite 持久化门面。

    Args:
        db_path: 数据库文件路径，默认 ``data/mac_cleaner.db``。
        audit_path: 审计日志路径，默认 ``data/audit.log``。
        app_log_path: 运行日志路径，默认 ``data/app.log``。
    """

    SETTINGS_KEY = "app"

    def __init__(self, db_path: Optional[str] = None,
                 audit_path: Optional[str] = None,
                 app_log_path: Optional[str] = None) -> None:
        self._db_path = str(db_path or config.DB_PATH)
        self._audit_path = str(audit_path or config.AUDIT_LOG_PATH)
        self._app_log_path = str(app_log_path or config.APP_LOG_PATH)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        config.ensure_dirs()
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self.init_db()

    # ---------------------------------------------------------------- 建表
    def init_db(self) -> None:
        """创建全部表与索引（可重复调用）。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(_CREATE_HISTORY)
            cur.execute(_CREATE_HISTORY_INDEX)
            cur.execute(_CREATE_ITEM)
            cur.execute(_CREATE_ITEM_INDEX)
            cur.execute(_CREATE_SETTINGS)
            self._conn.commit()

    # ---------------------------------------------------------------- 历史
    def total_freed_bytes(self) -> int:
        """返回历史累计释放字节数（全部 ``completed`` 记录求和）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(freed_bytes), 0) AS total "
                "FROM clean_history WHERE status = 'completed';").fetchone()
            return int(row["total"]) if row else 0

    def stats(self) -> Dict[str, Any]:
        """返回历史汇总指标：累计释放、清理次数、首次使用时间。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(freed_bytes), 0) AS freed, "
                "COUNT(*) AS cnt, MIN(started_at) AS first_at "
                "FROM clean_history WHERE status = 'completed';").fetchone()
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM clean_history;").fetchone()
            last_row = self._conn.execute(
                "SELECT MAX(started_at) AS last_at FROM clean_history;"
            ).fetchone()
        return {
            "cumulative_freed_bytes": int(row["freed"]) if row else 0,
            "clean_count": int(count_row["cnt"]) if count_row else 0,
            "completed_count": int(row["cnt"]) if row else 0,
            "first_used_at": row["first_at"] if row else None,
            "last_used_at": last_row["last_at"] if last_row else None,
        }

    def list_history(self, limit: int = 50,
                     offset: int = 0) -> Dict[str, Any]:
        """分页返回历史记录（按时间倒序）。

        Returns:
            ``{"total", "cumulative_freed_bytes", "clean_count",
            "first_used_at", "records"}``
        """
        limit = config.clamp_int(limit, 1, 500, 50)
        offset = config.clamp_int(offset, 0, 100000, 0)
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM clean_history;").fetchone()["c"]
            rows = self._conn.execute(
                "SELECT id, started_at, finished_at, duration_ms, mode, "
                "item_count, success_count, skipped_count, failed_count, "
                "freed_bytes, status, categories, has_detail "
                "FROM clean_history ORDER BY started_at DESC, id DESC "
                "LIMIT ? OFFSET ?;", (limit, offset)).fetchall()
        records: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["categories"] = json.loads(item.get("categories") or "[]")
            except (TypeError, ValueError):
                item["categories"] = []
            item["has_detail"] = bool(item.get("has_detail"))
            records.append(item)
        summary = self.stats()
        return {
            "total": int(total),
            "records": records,
            **summary,
        }

    def get_history(self, hid: int) -> Optional[Dict[str, Any]]:
        """按 id 取单条历史及其明细。

        Returns:
            ``{"record": {...}, "items": [...]}``；不存在时返回 None。
            明细已被归档时 ``record.has_detail`` 为 False 且 ``items`` 为空列表。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clean_history WHERE id = ?;", (hid,)).fetchone()
            if row is None:
                return None
            record = dict(row)
            try:
                record["categories"] = json.loads(
                    record.get("categories") or "[]")
            except (TypeError, ValueError):
                record["categories"] = []
            record["has_detail"] = bool(record.get("has_detail"))
            items: List[Dict[str, Any]] = []
            if record["has_detail"]:
                items = [dict(r) for r in self._conn.execute(
                    "SELECT id, path, size, category_id, result, "
                    "reason_code, reason_text FROM clean_history_item "
                    "WHERE history_id = ? ORDER BY id ASC;",
                    (hid,)).fetchall()]
        return {"record": record, "items": items}

    def save_history(self, rec: Dict[str, Any],
                     items: Optional[List[Dict[str, Any]]] = None) -> int:
        """写入一条历史记录与明细（同一事务），并完成最近 5 次明细裁剪。

        Args:
            rec: 汇总字段字典，键同 ``clean_history`` 表列。
            items: 路径级明细列表，元素含
                ``path/size/category_id/result/reason_code/reason_text``。

        Returns:
            新记录的自增 id。
        """
        items = items or []
        modes = (config.MODE_TRASH, config.MODE_DELETE)
        mode = rec.get("mode", config.MODE_TRASH)
        if mode not in modes:
            mode = config.MODE_TRASH
        started_at = rec.get("started_at") or config.now_iso()
        finished_at = rec.get("finished_at") or config.now_iso()
        categories = rec.get("categories") or []
        payload = (
            started_at,
            finished_at,
            int(rec.get("duration_ms", 0) or 0),
            mode,
            int(rec.get("item_count", len(items)) or 0),
            int(rec.get("success_count", 0) or 0),
            int(rec.get("skipped_count", 0) or 0),
            int(rec.get("failed_count", 0) or 0),
            int(rec.get("freed_bytes", 0) or 0),
            rec.get("status") or config.JOB_DONE,
            json.dumps(categories, ensure_ascii=False),
            1 if items else 0,
        )
        with self._write_lock, self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO clean_history (started_at, finished_at, "
                "duration_ms, mode, item_count, success_count, skipped_count, "
                "failed_count, freed_bytes, status, categories, has_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", payload)
            hid = int(cur.lastrowid)
            if items:
                rows = [
                    (hid,
                     str(item.get("path", "")),
                     int(item.get("size", 0) or 0),
                     str(item.get("category_id", "")),
                     str(item.get("result", config.RESULT_SKIPPED)),
                     item.get("reason_code"),
                     item.get("reason_text"))
                    for item in items]
                self._conn.executemany(
                    "INSERT INTO clean_history_item (history_id, path, size, "
                    "category_id, result, reason_code, reason_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);", rows)
            keep = int(config.HISTORY_DETAIL_KEEP)
            self._conn.execute(_PRUNE_ITEMS, (keep,))
            self._conn.execute(_PRUNE_HISTORY_FLAG, (keep,))
        return hid

    def clear_history(self) -> int:
        """清空全部历史与明细。

        Returns:
            被删除的历史记录条数。
        """
        with self._write_lock, self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM clean_history;").fetchone()
            count = int(row["c"]) if row else 0
            self._conn.execute("DELETE FROM clean_history_item;")
            self._conn.execute("DELETE FROM clean_history;")
        self.log("info", "已清空全部历史记录，共 %d 条" % count)
        return count

    # ---------------------------------------------------------------- 设置
    def get_settings(self) -> Dict[str, Any]:
        """返回与默认值深合并后的完整设置字典。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?;",
                (self.SETTINGS_KEY,)).fetchone()
        raw: Dict[str, Any] = {}
        if row:
            try:
                loaded = json.loads(row["value"])
                if isinstance(loaded, dict):
                    raw = loaded
            except (TypeError, ValueError):
                raw = {}
        return config.deep_merge(config.DEFAULT_SETTINGS, raw)

    def save_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """局部更新设置并返回合并后的全量设置。

        Args:
            patch: 局部设置字典，非法值会被校验器过滤/纠正。
        """
        patch = self._validate(patch or {})
        merged = config.deep_merge(self.get_settings(), patch)
        with self._write_lock, self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                (self.SETTINGS_KEY,
                 json.dumps(merged, ensure_ascii=False, sort_keys=True)))
        return merged

    @staticmethod
    def _validate(patch: Dict[str, Any]) -> Dict[str, Any]:
        """校验并纠正设置取值（违反约定即落入安全区间）。"""
        result: Dict[str, Any] = {}
        for key, value in (patch or {}).items():
            if key == "mode":
                result[key] = (config.MODE_DELETE
                               if value == config.MODE_DELETE
                               else config.MODE_TRASH)
            elif key == "scan_external_volumes":
                result[key] = bool(value)
            elif key == "category_enabled":
                result[key] = {str(k): bool(v)
                               for k, v in (value or {}).items()}
            elif key == "category_default_selected":
                result[key] = {str(k): bool(v)
                               for k, v in (value or {}).items()}
            elif key == "limits":
                src = value or {}
                result[key] = {
                    "max_bytes_per_clean": config.clamp_int(
                        src.get("max_bytes_per_clean", 21474836480),
                        1024 ** 3, 10 * 1024 ** 4, 21474836480),
                    "max_items_per_clean": config.clamp_int(
                        src.get("max_items_per_clean", 50000),
                        100, 500000, 50000),
                }
            elif key == "downloads":
                src = value or {}
                result[key] = {
                    "min_size_bytes": config.clamp_int(
                        src.get("min_size_bytes", 104857600),
                        1024 ** 2, 100 * 1024 ** 3, 104857600),
                    "min_days_unused": config.clamp_int(
                        src.get("min_days_unused", 30), 1, 3650, 30),
                    "time_basis": ("mtime"
                                   if src.get("time_basis") == "mtime"
                                   else "atime_or_mtime"),
                }
            elif key == "scan":
                src = value or {}
                result[key] = {
                    "concurrency": config.clamp_int(
                        src.get("concurrency", 4), 1, 16, 4),
                    "timeout_sec": config.clamp_int(
                        src.get("timeout_sec", config.SCAN_TIMEOUT_SEC),
                        30, 3600, config.SCAN_TIMEOUT_SEC),
                }
            elif key == "appearance":
                result[key] = (value if value in ("system", "light", "dark")
                               else "dark")
            else:
                # 未知键原样透传，保证向前兼容
                result[key] = value
        return result

    # ---------------------------------------------------------------- 审计
    def audit(self, path: str, code: str, message: str) -> None:
        """写入一条安全审计留痕（SEC-10）。

        格式：``ISO8601 | code | path | message``
        """
        line = "%s | %s | %s | %s" % (
            config.now_iso(), code, path, message)
        self._write_log(self._audit_path, line)

    def log(self, level: str, msg: str) -> None:
        """写入运行日志，格式 ``ISO8601 [LEVEL] message``。"""
        line = "%s [%s] %s" % (config.now_iso(), str(level).upper(), msg)
        self._write_log(self._app_log_path, line)

    # ---------------------------------------------------------------- 日志读写
    def _write_log(self, path: str, line: str) -> None:
        """追加一行日志，超过 LOG_MAX_BYTES 时滚动（A6：保留 3 份）。"""
        with self._lock:
            try:
                config.ensure_dirs()
                if os.path.exists(path):
                    try:
                        if os.path.getsize(path) >= config.LOG_MAX_BYTES:
                            self._rotate(path)
                    except OSError:
                        pass
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # 日志失败绝不能影响主流程
                pass

    @staticmethod
    def _rotate(path: str) -> None:
        """把已有日志滚动为 ``.1`` ~ ``.3``，最旧的丢弃。"""
        for index in range(config.LOG_BACKUP_COUNT, 0, -1):
            src = "%s.%d" % (path, index)
            if not os.path.exists(src):
                continue
            if index >= config.LOG_BACKUP_COUNT:
                try:
                    os.remove(src)
                except OSError:
                    pass
                continue
            dst = "%s.%d" % (path, index + 1)
            try:
                os.replace(src, dst)
            except OSError:
                pass
        try:
            os.replace(path, "%s.1" % path)
        except OSError:
            pass

    def read_logs(self, log_type: str = "audit",
                  lines: int = 500) -> List[str]:
        """读取日志尾部若干行。

        Args:
            log_type: ``audit`` 或 ``app``。
            lines: 最多返回的行数（1~5000）。

        Returns:
            按文件中顺序（旧 → 新）排列的行列表。
        """
        path = self._app_log_path if log_type == "app" else self._audit_path
        lines = config.clamp_int(lines, 1, 5000, 500)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.readlines()
        except OSError:
            return []
        return [line.rstrip("\n") for line in content[-lines:]]

    # ---------------------------------------------------------------- 关闭
    def close(self) -> None:
        """关闭数据库连接（进程退出时调用）。"""
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass


def timestamp_ms(started: float) -> int:
    """把 ``time.time()`` 起始值换算为毫秒耗时。"""
    return int((time.time() - started) * 1000)
