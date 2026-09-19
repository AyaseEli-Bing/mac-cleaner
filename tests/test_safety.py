# -*- coding: utf-8 -*-
"""五道安全关卡单元测试（SEC-01 ~ SEC-11 / G1 验收项）。

执行方式::

    python3 -m unittest discover -s tests -v

全部用例都在临时沙箱内完成，绝不触碰真实主目录。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server import config, safety  # noqa: E402
from server.categories import REGISTRY, CategoryRegistry  # noqa: E402


class SafetyTestCase(unittest.TestCase):
    """安全关卡测试基类：负责搭建/拆除临时沙箱。"""

    def setUp(self) -> None:
        # macOS 上 $TMPDIR=``/var/folders/...`` 而 ``/var`` 是指向
        # ``/private/var`` 的符号链接：沙箱根必须先规范化，否则被测代码
        # 产出的 realpath（``/private/var/...``）与断言用的裸路径形态不一致，
        # 会得到一堆与被测逻辑无关的假失败。
        self.sandbox = os.path.realpath(tempfile.mkdtemp(prefix="mc-safety-"))
        self.home = os.path.join(self.sandbox, "Users", "alice")
        # 白名单：整个沙箱都是启用类目的根，便于单独测试黑名单/其它关卡
        self.whitelist = [self.sandbox]
        self.audits: list = []
        self.guard = safety.SafetyGuard(
            blacklist=list(safety.BLACKLIST),
            sip_prefixes=list(safety.SIP_PREFIXES),
            whitelist_roots=self.whitelist,
            home_dir=self.sandbox,
            audit=lambda p, c, m: self.audits.append((p, c, m)),
            sandbox_root=self.sandbox,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def make(self, rel: str, is_dir: bool = True, content: str = "x") -> str:
        """在沙箱内创建一个文件或目录，返回绝对路径。"""
        path = os.path.join(self.sandbox, rel)
        if is_dir:
            os.makedirs(path, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        return path


class TestGate1Normalize(SafetyTestCase):
    """关卡一：路径规范化（SEC-03 / SEC-11）。"""

    def test_empty_path_rejected(self) -> None:
        for raw in ("", "   ", None):
            result = self.guard.gate1_normalize(raw if raw is not None else "")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "empty_path")

    def test_root_path_rejected(self) -> None:
        for raw in ("/", "~", self.sandbox):
            result = self.guard.gate1_normalize(raw)
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "root_path")

    def test_dotdot_traversal_rejected(self) -> None:
        # 只需构造出「越出沙箱」的路径字符串，无需真的在沙箱外落盘
        target = os.path.join(self.sandbox, "Library", "Caches",
                              "..", "..", "..", "..", "etc", "passwd")
        result = self.guard.check(target)
        self.assertFalse(result.ok)
        self.assertIn(result.code, ("path_traversal", "root_path", "empty_path"))

    def test_symlink_escape_rejected(self) -> None:
        outside = tempfile.mkdtemp(prefix="mc-outside-")
        try:
            link = os.path.join(self.sandbox, "escape")
            os.symlink(outside, link)
            result = self.guard.check(os.path.join(link, "inner.txt"))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "path_traversal")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_normal_path_ok(self) -> None:
        target = self.make("Library/Caches/com.demo/Data", False)
        result = self.guard.gate1_normalize(target)
        self.assertTrue(result.ok)
        self.assertTrue(result.normalized_path.startswith(self.sandbox))


class TestGate2Whitelist(SafetyTestCase):
    """关卡二：白名单制（SEC-04）。"""

    def test_outside_whitelist_rejected(self) -> None:
        other = tempfile.mkdtemp(prefix="mc-other-")
        try:
            guard = safety.SafetyGuard(
                whitelist_roots=[os.path.join(self.sandbox, "Library", "Caches")],
                home_dir=self.sandbox,
                sandbox_root=self.sandbox)
            target = self.make("Library/Logs/app.log", False)
            result = guard.gate2_whitelist(os.path.realpath(target))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "not_in_whitelist")
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_random_paths_outside_catalog_rejected(self) -> None:
        guard = safety.SafetyGuard(
            whitelist_roots=[os.path.join(self.sandbox, "Library", "Caches")],
            home_dir=self.sandbox,
            sandbox_root=self.sandbox)
        targets = [os.path.join(self.sandbox, "Library", "Logs", str(i))
                   for i in range(20)]
        for target in targets:
            result = guard.gate2_whitelist(os.path.realpath(target))
            self.assertFalse(result.ok, target)

    def test_inside_whitelist_ok(self) -> None:
        guard = safety.SafetyGuard(
            whitelist_roots=[os.path.join(self.sandbox, "Library", "Caches")],
            home_dir=self.sandbox,
            sandbox_root=self.sandbox)
        target = self.make("Library/Caches/com.demo/Cache.db", False)
        self.assertTrue(guard.gate2_whitelist(os.path.realpath(target)).ok)


class TestGate3Blacklist(SafetyTestCase):
    """关卡三：黑名单全覆盖（SEC-01，26 条逐条验证）。"""

    def _guard_with_exceptions(self, exceptions=None) -> safety.SafetyGuard:
        return safety.SafetyGuard(
            whitelist_roots=[self.sandbox],
            home_dir=self.sandbox,
            sandbox_root=self.sandbox,
            blacklist_exceptions=exceptions or {})

    def test_every_blacklist_entry_blocked(self) -> None:
        guard = self._guard_with_exceptions()
        for entry in safety.BLACKLIST:
            rel = entry[2:] if entry.startswith("~/") else entry.lstrip("/")
            rel = rel.replace("/**", "/sub")
            path = os.path.join(self.sandbox, rel)
            os.makedirs(os.path.dirname(path) or self.sandbox, exist_ok=True)
            result = guard.gate3_blacklist(os.path.normpath(path))
            self.assertFalse(result.ok, "黑名单条目未被拦截：%s" % entry)
            self.assertEqual(result.code, "blacklist")
            if entry == "/":
                # 根哨兵 ``/`` 按设计只做精确匹配（否则会误封整盘），
                # 其子女是否可清理由白名单关卡负责，不在本条断言范围内。
                continue
            # 子孙后代也必须被拦截
            child = os.path.join(path, "deeper", "leaf.bin")
            self.assertFalse(guard.gate3_blacklist(child).ok, entry)

    def test_root_only_entry_does_not_block_children(self) -> None:
        guard = self._guard_with_exceptions()
        self.assertFalse(guard.gate3_blacklist("/").ok)
        self.assertTrue(guard.gate3_blacklist(
            os.path.join(self.sandbox, "Library", "Caches")).ok)

    def test_usr_local_allowed_but_usr_blocked(self) -> None:
        guard = self._guard_with_exceptions()
        self.assertTrue(guard.gate3_blacklist("/usr/local/bin/node").ok)
        self.assertFalse(guard.gate3_blacklist("/usr/lib/libSystem.dylib").ok)

    def test_ios_backup_exception_released(self) -> None:
        backup = os.path.join(
            self.sandbox, "Library", "Application Support",
            "MobileSync", "Backup", "0000-1111")
        exceptions = {"ios_backup": [os.path.join(
            self.sandbox, "Library", "Application Support",
            "MobileSync", "Backup")]}
        guard = self._guard_with_exceptions(exceptions)
        self.assertTrue(guard.gate3_blacklist(backup, "ios_backup").ok)
        plain = self._guard_with_exceptions()
        self.assertFalse(plain.gate3_blacklist(backup, "ios_backup").ok)

    def test_blacklist_in_home_expanded(self) -> None:
        guard = self._guard_with_exceptions()
        documents = os.path.join(self.sandbox, "Documents", "report.docx")
        self.assertFalse(guard.gate3_blacklist(documents).ok)


class TestGate4Sip(SafetyTestCase):
    """关卡四：SIP 保护前缀（SEC-02）。"""

    def test_sip_prefixes_blocked(self) -> None:
        for prefix in safety.SIP_PREFIXES:
            path = os.path.join(prefix, "deep", "item")
            result = self.guard.gate4_sip(path)
            self.assertFalse(result.ok, prefix)
            self.assertEqual(result.code, "sip")

    def test_normal_path_pass(self) -> None:
        target = self.make("Library/Caches/com.demo/x", False)
        self.assertTrue(self.guard.gate4_sip(target).ok)


class TestGate5InUse(SafetyTestCase):
    """关卡五：占用检测（SEC-05）。"""

    def test_missing_path_rejected(self) -> None:
        result = self.guard.gate5_in_use(
            os.path.join(self.sandbox, "not-exist.bin"))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "not_found")

    def test_readonly_directory_rejected(self) -> None:
        target = self.make("Library/Caches/com.demo/locked")
        os.chmod(target, 0o500)
        try:
            result = self.guard.gate5_in_use(target)
            if os.access(target, os.W_OK):
                self.skipTest("当前用户拥有写权限，跳过只读目录用例")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "permission_denied")
        finally:
            os.chmod(target, 0o700)

    def test_writable_file_pass(self) -> None:
        target = self.make("Library/Caches/com.demo/cache.db", False)
        self.assertTrue(self.guard.gate5_in_use(target).ok)


class TestCheckPipeline(SafetyTestCase):
    """主控 check 管线与留痕。"""

    def test_pipeline_pass(self) -> None:
        target = self.make("Library/Caches/com.demo/cache.db", False)
        self.assertTrue(self.guard.check(target).ok)
        self.assertEqual(self.audits, [])

    def test_blocked_writes_audit(self) -> None:
        target = self.make("Documents/report.docx", False)
        result = self.guard.check(target)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "blacklist")
        self.assertTrue(any(code == "blacklist" for _, code, _ in self.audits))

    def test_skip_in_use_lightweight(self) -> None:
        missing = os.path.join(self.sandbox, "gone.bin")
        full = self.guard.check(missing)
        self.assertFalse(full.ok)
        light = self.guard.check(missing, skip_in_use=True)
        self.assertFalse(light.ok)
        self.assertEqual(light.code, "not_found")

    def test_check_batch(self) -> None:
        paths = [
            self.make("Library/Caches/a/x", False),
            self.make("Documents/b.docx", False),
            self.make("Library/Caches/c/y", False),
        ]
        results = self.guard.check_batch(paths, max_workers=3)
        self.assertEqual(len(results), 3)
        self.assertTrue(results[paths[0]].ok)
        self.assertFalse(results[paths[1]].ok)
        self.assertTrue(results[paths[2]].ok)


class TestLimits(SafetyTestCase):
    """单次清理上限（SEC-09）。"""

    def test_bytes_limit(self) -> None:
        items = [{"size": 10 * 1024 ** 3}, {"size": 11 * 1024 ** 3}]
        result = self.guard.check_limits(items, {"max_bytes_per_clean": 20 * 1024 ** 3})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "limit_exceeded")
        self.assertIn("分批", result.message)

    def test_items_limit(self) -> None:
        items = [{"size": 1} for _ in range(12)]
        result = self.guard.check_limits(items, {"max_items_per_clean": 10})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "limit_exceeded")

    def test_within_limit(self) -> None:
        items = [{"size": 1024} for _ in range(10)]
        result = self.guard.check_limits(
            items, {"max_bytes_per_clean": 20 * 1024 ** 3,
                    "max_items_per_clean": 100})
        self.assertTrue(result.ok)


class TestRegistryWhitelist(unittest.TestCase):
    """类目注册表：白名单在沙箱内的重映射。"""

    def setUp(self) -> None:
        self.sandbox = os.path.realpath(tempfile.mkdtemp(prefix="mc-registry-"))
        os.makedirs(os.path.join(self.sandbox, "Library", "Caches"), exist_ok=True)
        os.makedirs(os.path.join(self.sandbox, "Downloads"), exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def test_roots_remapped_into_sandbox(self) -> None:
        roots = REGISTRY.whitelist_roots(
            config.DEFAULT_SETTINGS, self.sandbox, self.sandbox)
        self.assertTrue(roots)
        for root in roots:
            self.assertTrue(root.startswith(self.sandbox), root)

    def test_probe_partial_for_missing_package_cache(self) -> None:
        status = REGISTRY.probe_status("pkg_manager_caches", self.sandbox,
                                       self.sandbox)
        self.assertIn(status, ("partial", "not_found"))

    def test_probe_exists_for_user_caches(self) -> None:
        status = REGISTRY.probe_status("user_caches", self.sandbox,
                                       self.sandbox)
        self.assertEqual(status, "exists")


if __name__ == "__main__":
    unittest.main(verbosity=2)
