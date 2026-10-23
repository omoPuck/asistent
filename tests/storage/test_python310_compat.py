"""Python 3.10 兼容 + 零第三方依赖的静态检查。

运行时证据是设备上的 3.10.12 复跑（见 docs/mvp/mvp-0.md）；这里再加一层**静态**闸门：
只要有人引入 3.11+/3.12+ 才有的标准库或语法，或者引入第三方依赖，测试立刻变红。

（设备事实：`/usr/bin/python3` = 3.10.12，sqlite3 3.37.2，没有 pip，装不了任何东西。）

注意：本文件要扫描它自己，所以被扫描的关键字都用片段拼接构造，
源码里不会出现完整的禁用名（否则扫描器会自己举报自己）。
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

from .support import REPO_ROOT

#: 3.11+ / 3.12+ 才有的东西：在 3.10 上会 NameError / AttributeError / SyntaxError
FORBIDDEN_311_PLUS = (
    ("toml" + "lib", "该模块仅 3.11+ 提供，本项目不需要 TOML"),
    ("typing" + ".Self", "该类型仅 3.11+ 提供"),
    ("Exception" + "Group", "该类型仅 3.11+ 提供"),
    ("except" + "*", "星号 except 语法仅 3.11+"),
    ("Task" + "Group", "该类型仅 3.11+ 提供"),
    ("asyncio" + ".timeout", "该 API 仅 3.11+ 提供"),
    ("datetime" + ".UTC", "该常量仅 3.11+ 提供（3.10 用 timezone.utc）"),
    ("sys" + ".exception(", "该函数仅 3.11+ 提供"),
    ("sqlite3" + ".autocommit", "该属性仅 3.12+ 提供"),
    ("get" + "limit", "sqlite3 的 limit 接口仅 3.11+ 提供"),
    ("itertools" + ".batched", "该 API 仅 3.12+ 提供"),
    ("typing" + ".override", "该装饰器仅 3.12+ 提供"),
)

#: 允许的导入根：标准库 + 本项目
ALLOWED_ROOTS = {"asistent", "tests"}

#: 本任务自己的写作用域（禁止用法扫描只扫这里，避免误报冻结契约里的说明文字）
OWNED_DIRS = ("asistent/storage", "tests/storage")


def _walk(subdir: str):
    root = os.path.join(REPO_ROOT, subdir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield os.path.join(dirpath, filename)


def _source_files(subdir: str):
    for path in _walk(subdir):
        with open(path, "r", encoding="utf-8-sig") as handle:
            yield path, handle.read()


def _bytes_files(subdir: str):
    for path in _walk(subdir):
        with open(path, "rb") as handle:
            yield path, handle.read()


def _relative(path: str) -> str:
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


class Python310CompatibilityTest(unittest.TestCase):
    def test_no_python_311_plus_constructs_in_owned_sources(self) -> None:
        offences = []
        for subdir in OWNED_DIRS:
            for path, text in _source_files(subdir):
                for token, why in FORBIDDEN_311_PLUS:
                    if token in text:
                        offences.append("%s: %s（%s）" % (_relative(path), token, why))
        self.assertEqual([], offences, "发现 3.11+ 专有用法: %r" % (offences,))

    def test_sources_parse_as_python_310_grammar(self) -> None:
        """按 3.10 的语法版本解析整棵树（读字节，兼容契约文件里的 UTF-8 BOM）。"""
        checked = 0
        for subdir in ("asistent", "tests"):
            for path, raw in _bytes_files(subdir):
                try:
                    ast.parse(raw, filename=path, feature_version=(3, 10))
                except SyntaxError as exc:  # pragma: no cover - 有错才走到
                    self.fail("%s 不是合法的 Python 3.10 语法: %s" % (_relative(path), exc))
                checked += 1
        self.assertGreaterEqual(checked, 20, "解析的文件数太少，扫描路径可能不对")

    def test_only_standard_library_imports(self) -> None:
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        self.assertTrue(stdlib, "当前解释器必须提供 sys.stdlib_module_names（3.10+）")
        offences = []
        for subdir in ("asistent", "tests"):
            for path, raw in _bytes_files(subdir):
                tree = ast.parse(raw, filename=path)
                roots = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        roots.extend(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        if node.level:  # 相对导入：包内模块，天然允许
                            continue
                        roots.append((node.module or "").split(".")[0])
                for root in roots:
                    if root and root not in stdlib and root not in ALLOWED_ROOTS:
                        offences.append("%s: %s" % (_relative(path), root))
        self.assertEqual([], offences, "出现非标准库导入（MVP-0 禁止第三方依赖）: %r" % (offences,))

    def test_third_party_storage_drivers_are_absent(self) -> None:
        """持久化必须走标准库 sqlite3：不得出现 sqlite 驱动包或 ORM。"""
        banned = (
            "import " + "apsw",
            "pysqlite" + "3",
            "sql" + "alchemy",
            "peew" + "ee",
            "sql" + "model",
            "import sqlite" + "_utils",
        )
        for subdir in OWNED_DIRS:
            for path, text in _source_files(subdir):
                for token in banned:
                    self.assertNotIn(token, text, "%s 出现第三方存储依赖: %s" % (_relative(path), token))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
