"""对 `asistent/console` 做两项静态体检：纯标准库、Python 3.10 可用。

这是**针对本包**的检查，不替代 `tests/storage/test_python310_compat.py` 的全仓库扫描：

* 纯标准库：证明没有偷偷依赖第三方（SPEC 要求纯 stdlib，设备上没有 pip）；
* 3.10 可用：设备上的 `/usr/bin/python3` 就是 3.10.12，用不到 3.11+ 的语法/标准库
  （`tomllib`/`typing.Self`/`TaskGroup`/`datetime.UTC`/`except*` 等）。
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PACKAGE_ROOT = os.path.join(_REPO_ROOT, "asistent", "console")

#: 允许导入的标准库顶层模块（新增依赖必须显式加进来，让评审看得见）
ALLOWED_STDLIB = {
    "__future__",
    "base64",
    "ipaddress",
    "contextlib",
    "dataclasses",
    "email",
    "errno",
    "hashlib",
    "hmac",
    "http",
    "json",
    "mimetypes",
    "os",
    "posixpath",
    "secrets",
    "socket",
    "socketserver",
    "stat",
    "string",
    "sys",
    "threading",
    "time",
    "typing",
    "urllib",
    "uuid",
    "weakref",
}

#: 3.11+ 才有的东西（用到就必须改掉，否则设备上跑不起来）
FORBIDDEN_ATTRIBUTES = {
    "tomllib",
    "TaskGroup",
    "ExceptionGroup",
    "batched",
    "aclose",
}

FORBIDDEN_NAMES = {"Self"}

#: 3.11+ 的运行时 API（按点号路径匹配，例如 `datetime.UTC`、`asyncio.timeout`）
FORBIDDEN_DOTTED = {
    "datetime.UTC",
    "asyncio.timeout",
    "asyncio.TaskGroup",
    "itertools.batched",
    "typing.Self",
}


def package_files():
    for root, _dirs, files in os.walk(PACKAGE_ROOT):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def is_standard_library(module: str) -> bool:
    """是否标准库。

    判定用"白名单 ∪ 解释器自报的标准库集合"：白名单记录的是**我们刻意用过**的最小集合
    （读代码时能一眼看出依赖面），而 `sys.stdlib_module_names`（Python 3.10+ 提供）是权威集合。
    只认白名单会变成打地鼠——每引入一个标准库（例如 `ipaddress`）就误报一次"三方依赖"。
    """
    if module in ALLOWED_STDLIB:
        return True
    return module in getattr(sys, "stdlib_module_names", frozenset())


class PureStdlibTests(unittest.TestCase):
    """导入面必须只有标准库 + 包内相对导入。"""

    def test_only_stdlib_and_relative_imports(self):
        offenders = []
        for path in package_files():
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if not is_standard_library(top):
                            offenders.append((os.path.relpath(path, _REPO_ROOT), "import " + alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue  # 包内相对导入
                    top = (node.module or "").split(".")[0]
                    if node.module and node.module.startswith("asistent"):
                        continue  # 仓库内其他包
                    if not is_standard_library(top):
                        offenders.append((os.path.relpath(path, _REPO_ROOT), "from %s import" % node.module))
        self.assertEqual([], offenders, "发现非标准库导入：%r" % (offenders,))

    def test_no_third_party_server_frameworks(self):
        blob = "\n".join(open(path, encoding="utf-8").read() for path in package_files())
        for forbidden in ("fastapi", "flask", "django", "aiohttp", "tornado", "starlette", "uvicorn", "waitress"):
            self.assertNotIn(forbidden, blob.lower(), "不得引入 %s" % forbidden)

    def test_frontend_has_no_third_party_dependency(self):
        static = os.path.join(PACKAGE_ROOT, "static")
        for name in ("index.html", "app.js", "styles.css", "sw.js", "manifest.webmanifest", "icon.svg"):
            self.assertTrue(os.path.isfile(os.path.join(static, name)), "缺少静态资源 %s" % name)
        blob = ""
        for name in ("index.html", "app.js", "sw.js"):
            blob += open(os.path.join(static, name), encoding="utf-8").read()
        for forbidden in ("cdn.", "unpkg", "jsdelivr", "node_modules", "import React", "require("):
            self.assertNotIn(forbidden, blob, "前端不得依赖 %s" % forbidden)

    def test_static_js_never_puts_token_in_url(self):
        js = open(os.path.join(PACKAGE_ROOT, "static", "app.js"), encoding="utf-8").read()
        self.assertNotIn("?token=", js)
        self.assertNotIn("localStorage", js)
        self.assertIn("Authorization", js)


class Python310CompatTests(unittest.TestCase):
    """静态扫描 3.11+ 专属用法。"""

    def test_no_python311_only_constructs(self):
        offenders = []
        for path in package_files():
            source = open(path, encoding="utf-8").read()
            tree = ast.parse(source, filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    dotted = _dotted(node)
                    if dotted in FORBIDDEN_DOTTED or node.attr in FORBIDDEN_ATTRIBUTES:
                        offenders.append((os.path.relpath(path, _REPO_ROOT), dotted or node.attr))
                if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                    offenders.append((os.path.relpath(path, _REPO_ROOT), node.id))
                if isinstance(node, ast.Try) and node.handlers:
                    for handler in node.handlers:
                        if handler.type is not None and not isinstance(handler.type, ast.Name):
                            if isinstance(handler.type, ast.Starred):  # except*
                                offenders.append((os.path.relpath(path, _REPO_ROOT), "except*"))
                if isinstance(node, ast.ImportFrom) and (node.module or "") in FORBIDDEN_ATTRIBUTES:
                    offenders.append((os.path.relpath(path, _REPO_ROOT), node.module or ""))
        self.assertEqual([], offenders, "发现 Python 3.11+ 专属用法：%r" % (offenders,))

    def test_uses_only_310_standard_library(self):
        """逐个导入真正执行一次（在 3.10 上就是设备上的解释器，在开发机上是下限检查）。"""
        for path in package_files():
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and not node.module.startswith("asistent"):
                    module = node.module.split(".")[0]
                    if is_standard_library(module):
                        __import__(module)


def _dotted(node: ast.AST) -> str:
    parts = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


__all__ = ["PureStdlibTests", "Python310CompatTests"]
