# -*- coding: utf-8 -*-
"""扫描引擎沙箱测试（G2 验收：体积与 du 偏差 ≤5%、软链不递归、权限降级、取消）。

执行方式::

    python3 -m unittest tests.test_scanner_sandbox -v

所有用例在临时目录内构造假的主目录，绝不触碰真实 ``~/Library``。
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server import config  # noqa: E402
from server.scanner import Scanner  # noqa: E402


def raw_size(path: str) -> int:
    """用与 du 相同的规则递归统计目录体积（不跟随符号链接）。"""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            total += st.st_size
            if stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
    return total


class ScannerSandboxTestCase(unittest.TestCase):
    """扫描器沙箱测试基类。"""

    def setUp(self) -> None:
        # 见 test_safety：macOS 上 $TMPDIR 的 ``/var`` 是指向 ``/private/var``
        # 的符号链接，沙箱根必须先规范化才能与 realpath 形态对齐。
        self.sandbox = os.path.realpath(tempfile.mkdtemp(prefix="mc-scan-"))
        self.caches = os.path.join(self.sandbox, "Library", "Caches")
        os.makedirs(self.caches, exist_ok=True)
        self.settings = config.deep_merge(config.DEFAULT_SETTINGS, {
            "scan": {"concurrency": 2, "timeout_sec": 300},
        })

    def tearDown(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def write(self, rel: str, size: int) -> str:
        path = os.path.join(self.sandbox, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return path

    def scanner(self, **kwargs) -> Scanner:
        return Scanner(sandbox_root=self.sandbox, home_dir=self.sandbox,
                       **kwargs)

    def run_scan(self, scanner: Scanner,
                 categories=None, timeout: float = 30.0,
                 settings: dict = None) -> dict:
        scanner.start(categories if categories is not None else ["user_caches"],
                      settings if settings is not None else self.settings)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.15)
            try:
                progress = scanner.progress()
            except Exception:                               # pragma: no cover
                continue
            if progress.get("status") != "running":
                break
        return scanner.result()


class TestScanAccuracy(ScannerSandboxTestCase):
    """体积统计准确性（STAT-02 / STAT-06）。"""

    def test_size_matches_du_within_5_percent(self) -> None:
        self.write("Library/Caches/com.alpha/data.bin", 400_000)
        self.write("Library/Caches/com.alpha/nested/x.bin", 300_000)
        self.write("Library/Caches/com.beta/y.bin", 200_000)

        result = self.run_scan(self.scanner())
        category = [c for c in result["categories"]
                    if c["category_id"] == "user_caches"][0]
        expected = raw_size(self.caches)
        actual = category["size"]
        self.assertGreater(actual, 0)
        if expected > 0:
            deviation = abs(actual - expected) / float(expected)
            self.assertLessEqual(deviation, 0.05,
                                 "偏差 %.3f%% 超过 5%%" % (deviation * 100))
        self.assertEqual(category["item_count"], 2)

    def test_symlink_not_recursed(self) -> None:
        outside = tempfile.mkdtemp(prefix="mc-linktarget-")
        try:
            with open(os.path.join(outside, "huge.bin"), "wb") as handle:
                handle.write(b"y" * 5_000_000)
            link = os.path.join(self.caches, "com.linkApp")
            os.makedirs(link, exist_ok=True)
            os.symlink(outside, os.path.join(link, "linked"))
            self.write("Library/Caches/com.linkApp/local.bin", 1024)

            result = self.run_scan(self.scanner())
            category = [c for c in result["categories"]
                        if c["category_id"] == "user_caches"][0]
            # 只应计入 local.bin 与链接本身的大小，不得递归到 5MB 的目标
            self.assertLess(category["size"], 100_000)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_percent_sum_to_100(self) -> None:
        self.write("Library/Caches/com.alpha/a.bin", 300_000)
        result = self.run_scan(self.scanner())
        total = sum(c["percent"] for c in result["categories"]
                    if c["selected"])
        if total > 0:
            self.assertAlmostEqual(total, 100.0, delta=1.0)

    def test_missing_category_reports_not_found(self) -> None:
        result = self.run_scan(self.scanner(),
                               categories=["pkg_manager_caches"])
        category = [c for c in result["categories"]
                    if c["category_id"] == "pkg_manager_caches"][0]
        self.assertIn(category["status"], ("not_found", "partial"))
        self.assertTrue(any(w["code"] == "not_found"
                            for w in category["warnings"]))


class TestScanResilience(ScannerSandboxTestCase):
    """异常降级、可中断、超时保护。"""

    def test_permission_denied_downgraded(self) -> None:
        locked = os.path.join(self.caches, "com.locked")
        os.makedirs(locked, exist_ok=True)
        self.write("Library/Caches/com.visible/v.bin", 4096)
        os.chmod(locked, 0o000)
        try:
            if os.access(locked, os.R_OK | os.X_OK):
                self.skipTest("当前用户可访问受限目录，跳过权限降级用例")
            result = self.run_scan(self.scanner())
            category = [c for c in result["categories"]
                        if c["category_id"] == "user_caches"][0]
            self.assertTrue(any(w["code"] == "permission_denied"
                                for w in category["warnings"]),
                            category["warnings"])
        finally:
            os.chmod(locked, 0o755)

    def test_cancel_keeps_partial_result(self) -> None:
        for index in range(60):
            self.write("Library/Caches/com.big%03d/d.bin" % index, 200_000)
        scanner = self.scanner(concurrency=1)
        scanner.start(["user_caches"], self.settings)
        # 刚启动的扫描必然仍在运行，此时 cancel 必须被接受；
        # 结果需保留已完成（或已降级返回）的类目，状态标记为 aborted
        self.assertTrue(scanner.cancel(), "扫描启动后应立即可被取消")
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.1)
            if not scanner.is_running():
                break
        self.assertFalse(scanner.is_running())
        # 中断后结果依然可读
        result = scanner.result()
        self.assertEqual(result["status"], "aborted")
        self.assertIn(result["categories"][0]["category_id"], ("user_caches",))

    def test_progress_reports_counters(self) -> None:
        for index in range(20):
            self.write("Library/Caches/com.p%02d/d.bin" % index, 20_000)
        result = self.run_scan(self.scanner())
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertGreaterEqual(result["totals"]["reclaimable_bytes"], 0)

    def test_item_id_is_stable_hash(self) -> None:
        import hashlib
        self.write("Library/Caches/com.hash/h.bin", 1024)
        result = self.run_scan(self.scanner())
        items = result["categories"][0]["items"]
        self.assertTrue(items)
        for item in items:
            self.assertEqual(
                item["id"],
                hashlib.sha1(item["path"].encode("utf-8")).hexdigest()[:16])

    def test_downloads_size_age_filter(self) -> None:
        # 用「小阈值」代替真的写 200 MB：判定逻辑（体积 + 未访问天数）完全一致，
        # 但不会让测试跑出几十秒的磁盘写入。
        threshold = 100 * 1024
        settings = config.deep_merge(self.settings, {
            "downloads": {"min_size_bytes": threshold, "min_days_unused": 30},
        })
        old_file = self.write("Downloads/old.iso", 200 * 1024)
        old_ts = time.time() - 90 * 86400
        os.utime(old_file, (old_ts, old_ts))
        small_old = self.write("Downloads/small.bin", 1024)
        os.utime(small_old, (old_ts, old_ts))
        fresh = self.write("Downloads/fresh.iso", 200 * 1024)
        self.assertTrue(os.path.exists(fresh))

        result = self.run_scan(self.scanner(), categories=["downloads_large"],
                               settings=settings)
        category = result["categories"][0]
        names = [i["display"] for i in category["items"]]
        self.assertIn("old.iso", names)
        self.assertNotIn("small.bin", names)
        self.assertNotIn("fresh.iso", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
