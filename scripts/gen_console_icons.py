#!/usr/bin/env python3
"""生成手机端图标（PWA / iOS 主屏幕）。

为什么需要：iOS 的「添加到主屏幕」只认 PNG，SVG 图标会被忽略；Android 的
可安装性也要求 192/512 的 PNG（maskable 还要单独一张留出安全区）。
手写二进制不可维护，所以这里用 Pillow 画一遍——图案与 `icon.svg` 一致：

    奶油白底 + 陶土色"日出" + 一道地平线

用法：
    python3 scripts/gen_console_icons.py            # 写到 asistent/console/static/
    python3 scripts/gen_console_icons.py --check    # 只校验现有 PNG 是否存在/可解码

注意：这是**构建期**工具，不进入运行时依赖（板子上没有 Pillow 也能跑服务）。
"""

from __future__ import annotations

import argparse
import os
import sys

CREAM = (250, 246, 239, 255)
CREAM_DEEP = (244, 226, 209, 255)
BORDER = (233, 223, 208, 255)
CLAY = (192, 112, 74, 255)
INK = (46, 39, 33, 216)

#: 输出文件名 -> 边长
TARGETS = {
    "icon-192.png": 192,
    "icon-512.png": 512,
    "icon-maskable-512.png": 512,
    "apple-touch-icon.png": 180,
}

#: 画布放大倍数（先大后缩，得到抗锯齿边缘）
SUPERSAMPLE = 4


def _draw(size: int, *, maskable: bool, rounded: bool):
    from PIL import Image, ImageDraw

    scale = SUPERSAMPLE
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 背景：maskable 必须铺满整块（系统会裁成任意形状），普通图标画圆角
    if maskable:
        draw.rectangle([0, 0, canvas, canvas], fill=CREAM)
        inner_scale = 0.62  # 安全区：图形只占中间 62%
    else:
        radius = int(canvas * (0.225 if rounded else 0.0))
        if rounded:
            draw.rounded_rectangle([0, 0, canvas - 1, canvas - 1], radius=radius, fill=CREAM)
            draw.rounded_rectangle(
                [1, 1, canvas - 2, canvas - 2], radius=max(1, radius - 1), outline=BORDER, width=scale * 2
            )
        else:
            draw.rectangle([0, 0, canvas, canvas], fill=CREAM)
        inner_scale = 0.78 if rounded else 0.86

    box = canvas * inner_scale
    ox = (canvas - box) / 2.0
    oy = (canvas - box) / 2.0

    def px(fx: float, fy: float) -> tuple[float, float]:
        return (ox + box * fx, oy + box * fy)

    # 日出：下半圆（用饼形填充 180°~360°）
    cx, cy = px(0.5, 0.62)
    r = box * 0.30
    draw.pieslice([cx - r, cy - r, cx + r, cy + r], start=180, end=360, fill=CLAY)

    # 地平线
    x0, y0 = px(0.16, 0.72)
    x1, _ = px(0.84, 0.72)
    draw.line([x0, y0, x1, y0], fill=INK, width=max(2, int(box * 0.045)), joint="curve")

    # 地平线下的小点（留一个呼吸的位置）
    dot_x, dot_y = px(0.5, 0.845)
    dot_r = box * 0.032
    draw.ellipse([dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r], fill=(192, 112, 74, 140))

    return image.resize((size, size), Image.LANCZOS)


def generate(target_dir: str) -> list[str]:
    written = []
    for name, size in TARGETS.items():
        maskable = "maskable" in name
        rounded = name != "apple-touch-icon.png"  # iOS 自己裁圆角，给满幅底
        image = _draw(size, maskable=maskable, rounded=rounded)
        path = os.path.join(target_dir, name)
        image.save(path, format="PNG", optimize=True)
        written.append(path)
    return written


def check(target_dir: str) -> int:
    from PIL import Image

    missing = []
    for name, size in TARGETS.items():
        path = os.path.join(target_dir, name)
        if not os.path.isfile(path):
            missing.append("%s（缺失）" % name)
            continue
        try:
            with Image.open(path) as handle:
                if handle.size != (size, size):
                    missing.append("%s（尺寸 %r，期望 %dx%d）" % (name, handle.size, size, size))
                    continue
                handle.load()
        except Exception as exc:  # noqa: BLE001 - 校验工具，任何解码失败都要报出来
            missing.append("%s（解码失败：%s）" % (name, exc))
    if missing:
        print("图标校验失败：")
        for item in missing:
            print("  - %s" % item)
        return 1
    print("图标校验通过：%d 个 PNG 全部存在且尺寸正确" % len(TARGETS))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成手机端图标")
    parser.add_argument("--check", action="store_true", help="只校验，不重新生成")
    parser.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        help="仓库根目录（默认按脚本位置推断）",
    )
    args = parser.parse_args(argv)

    target_dir = os.path.join(args.out, "asistent", "console", "static")
    if not os.path.isdir(target_dir):
        print("找不到静态资源目录：%s" % target_dir, file=sys.stderr)
        return 2

    if args.check:
        return check(target_dir)

    for path in generate(target_dir):
        print("已生成 %s（%d 字节）" % (os.path.relpath(path, args.out), os.path.getsize(path)))
    return check(target_dir)


if __name__ == "__main__":
    raise SystemExit(main())
