"""手机端形态：暖白主题、可安装（PWA）、iOS 添加到主屏幕。

这里断言的都不是"看起来像"，而是能机械核对的事实：

* **主题**：显式浅色（`color-scheme: light`）+ 奶油底色；旧的深色底不能有残留，
  否则在系统深色模式下表单控件会被染黑、页脚会出现割裂感。
* **可安装**：manifest 里必须有 192/512 的 PNG（安卓认这些）、一张 maskable（自适应图标
  会裁形状，图形必须留在安全区内）、`display=standalone`。
* **iOS**：`apple-touch-icon` 必须是 **PNG 180×180**（Safari 会忽略 SVG 图标），
  并且要有 `apple-mobile-web-app-*` 三个 meta，否则"添加到主屏幕"后是普通网页而不是独立窗口。
* **缓存**：`sw.js` 的壳清单必须包含新图标并升级缓存名，否则手机升级后仍拿旧壳/旧图标。
"""

from __future__ import annotations

import json
import os
import re
import struct
import unittest

from .helpers import ConsoleTestCase

_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "asistent", "console", "static",
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: 暖白主题的锚点色（改主题时这里要一起改，是有意的"双份记录"）
CREAM_BG = "#faf6ef"
CLAY = "#c0704a"


def _png_size(path: str) -> tuple[int, int]:
    with open(path, "rb") as handle:
        head = handle.read(33)
    if head[:8] != _PNG_MAGIC:
        raise AssertionError("%s 不是 PNG（magic 不匹配）" % os.path.basename(path))
    if head[12:16] != b"IHDR":
        raise AssertionError("%s 的 IHDR 位置不对" % os.path.basename(path))
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


def _read_static(name: str) -> str:
    with open(os.path.join(_STATIC, name), encoding="utf-8") as handle:
        return handle.read()


class ThemeTests(ConsoleTestCase):
    """基调：暖白 / 奶色，且必须是浅色。"""

    def test_document_declares_light_warm_theme(self):
        page = self.client.get("/")
        self.assertStatus(200, page)
        body = page.response_body
        self.assertIn(CREAM_BG, body, "主题色应是奶油白")
        self.assertIn('content="light"', body, "必须声明 color-scheme: light")
        self.assertNotIn("#0d1117", body, "旧的深色底不该再出现")

    def test_stylesheet_is_warm_and_light(self):
        css = _read_static("styles.css")
        self.assertIn("color-scheme: light", css)
        self.assertIn("--bg: %s" % CREAM_BG, css)
        self.assertIn("--accent: %s" % CLAY, css)
        self.assertNotIn("#0d1117", css, "深色底残留：这是换肤没换干净")
        # 移动端两类硬约束：聚焦不放大（字号 >= 16px）、刘海/底部安全区
        self.assertIn("font-size: 16px", css)
        self.assertIn("env(safe-area-inset-top", css)
        self.assertIn("env(safe-area-inset-bottom", css)


class InstallableTests(ConsoleTestCase):
    """PWA 可安装性：manifest + PNG 图标。"""

    def test_manifest_has_png_icons_and_maskable(self):
        recorded = self.client.get("/manifest.webmanifest")
        self.assertStatus(200, recorded)
        self.assertEqual("application/manifest+json", recorded.response_headers.get("Content-Type"))
        manifest = recorded.json
        self.assertEqual("standalone", manifest["display"])
        self.assertEqual("/", manifest["start_url"])
        self.assertEqual(CREAM_BG, manifest["background_color"])
        self.assertEqual(CREAM_BG, manifest["theme_color"])

        icons = manifest["icons"]
        purposes = {(icon.get("purpose") or "any", icon["sizes"]) for icon in icons}
        self.assertIn(("any", "192x192"), purposes)
        self.assertIn(("any", "512x512"), purposes)
        self.assertIn(("maskable", "512x512"), purposes)
        for icon in icons:
            if icon["type"] == "image/png":
                self.assertTrue(
                    os.path.isfile(os.path.join(_STATIC, icon["src"].lstrip("/"))),
                    "manifest 指向的 PNG 不存在：%s" % icon["src"],
                )

    def test_icon_pngs_are_valid_and_sized(self):
        expected = {
            "icon-192.png": (192, 192),
            "icon-512.png": (512, 512),
            "icon-maskable-512.png": (512, 512),
            "apple-touch-icon.png": (180, 180),
        }
        for name, size in expected.items():
            path = os.path.join(_STATIC, name)
            self.assertTrue(os.path.isfile(path), "缺少图标 %s" % name)
            self.assertEqual(size, _png_size(path), "%s 尺寸不对" % name)

    def test_icon_png_is_served_with_image_content_type(self):
        recorded = self.client.get("/icon-192.png")
        self.assertStatus(200, recorded)
        self.assertEqual("image/png", recorded.response_headers.get("Content-Type"))


class IOSTests(ConsoleTestCase):
    """iOS：Safari「添加到主屏幕」认的这一组。"""

    def setUp(self) -> None:
        super().setUp()
        self.page = self.client.get("/").response_body

    def test_apple_meta_tags_present(self):
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', self.page)
        self.assertIn('name="apple-mobile-web-app-status-bar-style"', self.page)
        self.assertIn('name="apple-mobile-web-app-title" content="事务台"', self.page)
        self.assertIn('name="mobile-web-app-capable" content="yes"', self.page)
        # iOS 会因为电话号码样式把数字渲染成链接，控制台里全是 id/哈希，必须关掉
        self.assertIn('name="format-detection"', self.page)

    def test_apple_touch_icon_is_png_not_svg(self):
        match = re.search(r'<link rel="apple-touch-icon"[^>]*href="([^"]+)"', self.page)
        self.assertIsNotNone(match, "缺少 apple-touch-icon")
        href = match.group(1)
        self.assertTrue(href.endswith(".png"), "iOS 不认 SVG 图标：%s" % href)
        path = os.path.join(_STATIC, href.lstrip("/"))
        self.assertTrue(os.path.isfile(path), "apple-touch-icon 指向的文件不存在：%s" % href)
        self.assertEqual((180, 180), _png_size(path))

    def test_viewport_covers_notch(self):
        self.assertIn("viewport-fit=cover", self.page)


class ServiceWorkerTests(unittest.TestCase):
    """壳缓存：换皮肤后必须换缓存名，图标也要进清单。"""

    def test_shell_cache_version_bumped_and_icons_listed(self):
        sw = _read_static("sw.js")
        match = re.search(r"SHELL_CACHE\s*=\s*'asistent-console-shell-v(\d+)'", sw)
        self.assertIsNotNone(match, "sw.js 里找不到壳缓存名")
        self.assertGreaterEqual(int(match.group(1)), 2, "换了皮肤就该升缓存版本")
        for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
            self.assertIn(name, sw, "sw.js 的壳清单缺少 %s" % name)

    def test_api_is_never_cached(self):
        sw = _read_static("sw.js")
        self.assertIn("/api/", sw)
        self.assertNotIn("localStorage", sw)


class GateTests(ConsoleTestCase):
    """登录页是第一印象：要告诉用户令牌在哪，但又不能把令牌塞进 URL。"""

    def test_gate_explains_where_token_lives(self):
        body = self.client.get("/").response_body
        self.assertIn("连接令牌", body)
        self.assertIn("console-token", body)
        self.assertNotIn("?token=", body)

    def test_manifest_json_parses_after_edit(self):
        manifest = json.loads(_read_static("manifest.webmanifest"))
        self.assertIn("事务台", manifest["name"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
