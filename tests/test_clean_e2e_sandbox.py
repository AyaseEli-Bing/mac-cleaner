# -*- coding: utf-8 -*-
"""清理链路端到端测试（沙箱内完成，绝不触碰真实用户目录）。

覆盖主干：扫描 → 预览（dry-run）→ 二次确认 → 执行 → 移到废纸篓
→ 写历史 → 累计释放量，以及四类必须被拦截的情况
（黑名单路径、主目录外未确认、高危类目确认文本、单次上限）。

执行方式::

    python3 -m unittest discover -s tests -p "test_*.py" -v

设计要点：``Scanner`` / ``Cleaner`` 均注入 ``sandbox_root``，所有类目根、
废纸篓与数据库全部落在临时目录内，因此**可以在真实机器上安全运行**。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server import config  # noqa: E402
from server.cleaner import Cleaner  # noqa: E402
from server.scanner import Scanner  # noqa: E402
from server.store import Store  # noqa: E402


class CleanFlowTestCase(unittest.TestCase):
    """端到端基类：搭一个假主目录 + 独立数据库。"""

    def setUp(self) -> None:
        self.sandbox = os.path.realpath(tempfile.mkdtemp(prefix="mc-e2e-"))
        self.caches = os.path.join(self.sandbox, "Library", "Caches")
        os.makedirs(self.caches, exist_ok=True)
        self.settings = config.deep_merge(config.DEFAULT_SETTINGS, {
            "scan": {"concurrency": 2, "timeout_sec": 60},
        })
        self.store = Store(
            db_path=os.path.join(self.sandbox, "history.db"),
            audit_path=os.path.join(self.sandbox, "audit.log"),
            app_log_path=os.path.join(self.sandbox, "app.log"),
        )

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:                                   # pragma: no cover
            pass
        shutil.rmtree(self.sandbox, ignore_errors=True)

    # ------------------------------------------------------------ 工具
    def write(self, rel: str, size: int) -> str:
        """在沙箱内写一个指定体积的文件，返回绝对路径。"""
        path = os.path.join(self.sandbox, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return path

    def scanner(self) -> Scanner:
        """构造沙箱扫描器。"""
        return Scanner(sandbox_root=self.sandbox, home_dir=self.sandbox,
                       concurrency=2, timeout_sec=60)

    def cleaner(self, settings: dict = None) -> Cleaner:
        """构造沙箱清理器（废纸篓同样落在沙箱内）。"""
        return Cleaner(self.store, settings or self.settings,
                       sandbox_root=self.sandbox, home_dir=self.sandbox)

    def scan(self, scanner: Scanner, categories=None) -> list:
        """跑一次完整扫描，返回全部条目字典。"""
        scanner.start(categories or ["user_caches"], self.settings)
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.1)
            if not scanner.is_running():
                break
        self.assertFalse(scanner.is_running(), "扫描未在 30 秒内结束")
        result = scanner.result()
        items = []
        for category in result["categories"]:
            items.extend(category["items"])
        return items

    def await_job(self, cleaner: Cleaner, timeout: float = 30.0) -> dict:
        """等待后台清理任务结束，返回最终进度快照。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = cleaner.progress()
            if job["status"] != "running":
                return job
            time.sleep(0.1)
        return cleaner.progress()

    def item(self, path: str, category_id: str = "user_caches", **extra) -> dict:
        """手工构造一条条目（模拟前端按 id 回查后的结构）。"""
        data = {
            "id": "manual-" + os.path.basename(path.rstrip("/") or "root"),
            "path": path,
            "display": os.path.basename(path.rstrip("/")),
            "category_id": category_id,
            "size": 1024,
            "type": "dir" if os.path.isdir(path) else "file",
            "in_home": True,
            "keep_dir": False,
        }
        data.update(extra)
        return data

    def trash_dir(self) -> str:
        """沙箱内的废纸篓目录。"""
        return os.path.join(self.sandbox, config.TRASH_DIR_NAME)


class TestHappyPath(CleanFlowTestCase):
    """完整链路：扫描 → 预览 → 执行 → 历史。"""

    def test_scan_preview_execute_trash_writes_history(self) -> None:
        self.write("Library/Caches/com.demo/big.bin", 300_000)
        self.write("Library/Caches/com.other/small.bin", 100_000)

        items = self.scan(self.scanner())
        self.assertEqual(len(items), 2)

        cleaner = self.cleaner()
        preview = cleaner.preview(items, "trash")
        planned = preview["summary"]["planned_bytes"]
        self.assertEqual(preview["summary"]["planned_items"], 2)
        self.assertGreater(planned, 0)
        self.assertEqual(preview["summary"]["blocked"], [])
        self.assertTrue(preview["preview_token"].startswith("pv_"))

        # dry-run：预览本身不得产生任何写操作
        self.assertEqual(len(os.listdir(self.caches)), 2)

        cleaner.execute(preview["preview_token"], "trash")
        job = self.await_job(cleaner)
        self.assertEqual(job["status"], config.JOB_DONE)
        self.assertEqual(job["success"], 2)
        self.assertEqual(job["failed"], 0)
        self.assertEqual(job["freed_bytes"], planned)

        # 缓存目录已清空，内容落到沙箱废纸篓（可从废纸篓恢复）
        self.assertEqual(os.listdir(self.caches), [])
        self.assertEqual(len(os.listdir(self.trash_dir())), 2)

        # 历史记录与累计释放量
        history = self.store.list_history()
        self.assertEqual(history["total"], 1)
        record = history["records"][0]
        self.assertEqual(record["status"], config.HISTORY_COMPLETED)
        self.assertEqual(record["freed_bytes"], planned)
        self.assertEqual(record["success_count"], 2)
        self.assertIn("user_caches", record["categories"])
        self.assertEqual(self.store.total_freed_bytes(), planned)

        detail = self.store.get_history(record["id"])
        self.assertTrue(detail["record"]["has_detail"])
        self.assertEqual(len(detail["items"]), 2)
        self.assertTrue(all(i["result"] == config.RESULT_SUCCESS
                            for i in detail["items"]))

    def test_history_accumulates_across_rounds(self) -> None:
        first = self.write("Library/Caches/com.a/x.bin", 100_000)
        self.assertTrue(os.path.exists(first))
        cleaner = self.cleaner()

        rounds = []
        for index in range(2):
            self.write("Library/Caches/com.r%d/y.bin" % index, 50_000)
            items = self.scan(self.scanner())
            self.assertTrue(items, "第 %d 轮未扫到条目" % (index + 1))
            preview = cleaner.preview(items, "trash")
            cleaner.execute(preview["preview_token"], "trash")
            job = self.await_job(cleaner)
            self.assertEqual(job["status"], config.JOB_DONE)
            rounds.append(job["freed_bytes"])

        history = self.store.list_history()
        self.assertEqual(history["total"], 2)
        self.assertEqual(history["clean_count"], 2)
        self.assertEqual(self.store.total_freed_bytes(), sum(rounds))
        self.assertGreater(sum(rounds), 0)

    def test_delete_mode_keeps_root_directory(self) -> None:
        """CLEAN-09：清内容保目录。"""
        self.write("Library/Caches/com.demo/a.bin", 4096)
        root_item = self.item(self.caches, keep_dir=True,
                              size=4096, type="dir")
        cleaner = self.cleaner()
        preview = cleaner.preview([root_item], "delete")
        cleaner.execute(preview["preview_token"], "delete")
        job = self.await_job(cleaner)
        self.assertEqual(job["status"], config.JOB_DONE)
        self.assertEqual(job["success"], 1)
        self.assertTrue(os.path.isdir(self.caches), "缓存根目录必须被保留")
        self.assertEqual(os.listdir(self.caches), [])


class TestSafetyGates(CleanFlowTestCase):
    """四类必须被拦截 / 必须要求确认的情况。"""

    def test_blacklisted_path_is_blocked(self) -> None:
        target = self.write("Documents/report.docx", 1024)
        cleaner = self.cleaner()
        with self.assertRaises(config.ApiError) as ctx:
            cleaner.preview([self.item(target)], "trash")
        self.assertEqual(ctx.exception.code, config.ErrorCode.SAFETY_BLOCKED)
        blocked = ctx.exception.data["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn(blocked[0]["code"],
                      ("blacklist", "not_in_whitelist", "sip"))
        # 关键：文件必须原封不动
        self.assertTrue(os.path.exists(target))

    def test_outside_home_requires_confirmation(self) -> None:
        self.write("Library/Caches/com.demo/a.bin", 2048)
        item = self.item(self.caches + "/com.demo", size=2048, in_home=False)
        cleaner = self.cleaner()
        preview = cleaner.preview([item], "trash")
        self.assertTrue(preview["requires"]["confirm_outside_home"])
        self.assertEqual(preview["summary"]["outside_home"]["items"], 1)

        with self.assertRaises(config.ApiError) as ctx:
            cleaner.execute(preview["preview_token"], "trash")
        self.assertEqual(ctx.exception.code, config.ErrorCode.CONFIRM_REQUIRED)
        self.assertTrue(os.path.exists(item["path"]), "未确认前不得动任何文件")

        cleaner.execute(preview["preview_token"], "trash",
                        confirm_outside_home=True)
        job = self.await_job(cleaner)
        self.assertEqual(job["status"], config.JOB_DONE)
        self.assertEqual(job["success"], 1)

    def test_high_risk_category_requires_confirm_text(self) -> None:
        settings = config.deep_merge(self.settings, {
            "category_enabled": {"ios_backup": True},
        })
        backup = os.path.join(self.sandbox, "Library", "Application Support",
                              "MobileSync", "Backup", "00008110")
        os.makedirs(backup, exist_ok=True)
        with open(os.path.join(backup, "Manifest.db"), "wb") as handle:
            handle.write(b"z" * 2048)

        cleaner = self.cleaner(settings)
        preview = cleaner.preview(
            [self.item(backup, category_id="ios_backup", size=2048,
                       keep_dir=True)], "trash")
        self.assertEqual(preview["requires"]["confirm_text"], config.CONFIRM_TEXT)

        with self.assertRaises(config.ApiError) as ctx:
            cleaner.execute(preview["preview_token"], "trash")
        self.assertEqual(ctx.exception.code, config.ErrorCode.CONFIRM_REQUIRED)
        with self.assertRaises(config.ApiError) as ctx2:
            cleaner.execute(preview["preview_token"], "trash",
                            confirm_text="随便写的")
        self.assertEqual(ctx2.exception.code, config.ErrorCode.CONFIRM_REQUIRED)
        self.assertTrue(os.path.exists(backup), "确认前不得删除备份")

        cleaner.execute(preview["preview_token"], "trash",
                        confirm_text=config.CONFIRM_TEXT)
        job = self.await_job(cleaner)
        self.assertEqual(job["status"], config.JOB_DONE)
        self.assertEqual(job["success"], 1)

    def test_limit_exceeded_rejected(self) -> None:
        self.write("Library/Caches/com.demo/a.bin", 4096)
        settings = config.deep_merge(self.settings, {
            "limits": {"max_bytes_per_clean": 1000},
        })
        cleaner = self.cleaner(settings)
        with self.assertRaises(config.ApiError) as ctx:
            cleaner.preview([self.item(self.caches + "/com.demo", size=4096)],
                            "trash")
        self.assertEqual(ctx.exception.code, config.ErrorCode.LIMIT_EXCEEDED)
        self.assertIn("分批", ctx.exception.message)

    def test_expired_or_unknown_token_rejected(self) -> None:
        cleaner = self.cleaner()
        with self.assertRaises(config.ApiError) as ctx:
            cleaner.execute("pv_not_exists", "trash")
        self.assertEqual(ctx.exception.code,
                         config.ErrorCode.INVALID_PREVIEW_TOKEN)

    def test_audit_log_records_blocked_path(self) -> None:
        target = self.write("Documents/secret.txt", 64)
        cleaner = self.cleaner()
        with self.assertRaises(config.ApiError):
            cleaner.preview([self.item(target)], "trash")
        audit_path = os.path.join(self.sandbox, "audit.log")
        self.assertTrue(os.path.exists(audit_path), "被拦截的路径必须留痕")
        with open(audit_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("Documents", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
