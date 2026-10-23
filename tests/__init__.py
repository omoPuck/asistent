"""测试包的总入口——只做一件事：让"编码"不可能伪装成产品缺陷。

## 为什么需要这个文件（真实踩坑）

`tests/actions/test_single_execution.py::test_cross_process_concurrency_has_exactly_one_winner`
曾经在**默认 Windows 控制台**上稳定报 `ERROR`，而换一台机器/加一个环境变量就绿：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\ufffd' in position 151
  File "tests/actions/test_single_execution.py", line 118, in test_cross_process_concurrency...
    print("[evidence] 跨进程竞争 =", dump(sorted(rows, ...)))
```

链条是这样的（**每一环单独看都"没事"，叠起来才是陷阱**）：

1. 测试拉起子进程，但没有给子进程指定 stdio 编码 → 子进程按宿主 locale（Windows 上是 cp936）写中文；
2. 父进程按 UTF-8 解码 → 得到替换字符 `U+FFFD`（`errors="replace"` 把不一致**藏了起来**）；
3. `print` 这段证据到 cp936 控制台 → cp936 **编不出** `U+FFFD` → 抛 `UnicodeEncodeError`；
4. unittest 把这个异常记成"测试出错" → 于是 **一个纯编码问题看起来像并发竞态缺陷**。

代价是真实的：排查它花了接近两小时，还先把"机器休眠导致测试挂住"当成了另一个原因。

## 修法（两层，各自独立有效）

- **断掉第 1 环**：让子进程真的说 UTF-8。子进程继承父进程环境，所以在这里给
  `PYTHONUTF8` / `PYTHONIOENCODING` 设默认值，**整套测试里任何一个 `subprocess`
  拉起的子进程都自动是 UTF-8 stdio**——不需要每个包各写一遍，也不需要调用者记得在命令行前面加环境变量。
  用 `setdefault`，所以外部显式指定的值仍然优先（有人要测别的编码时不会被我们按住）。
- **去掉第 3 环的杀伤力**：把 stdout/stderr 的 `errors` 调成 `replace`。证据里出现看不懂的字符，
  应该"照原样打出来 + 看得见"，而不是把整条测试判成失败。**证据打印永远不该有能力让测试变红。**

注意这里**没有**动产品代码：产品自己的 stdio 纪律（`PYTHONUTF8=1` + 子进程强制 UTF-8）
仍然由各自模块负责；这个文件只保证"测试跑起来时，编码问题不会伪装成别的故障"。

## 边界（不要把这个文件当成万能药）

- **能保证**：本套件里任何子进程的 stdio 都是 UTF-8；证据打印永远不能把测试判红。
- **不能保证**：**测试运行器自己**（即 `python -m unittest` 这个父进程）的输出编码。
  父进程的解释器在 import 本文件之前就已经初始化了 stdio，事后改 `os.environ` 对它无效。
  所以如果父进程的 stdout 是管道且宿主 locale 是 cp936，**它的输出仍可能是 cp936**——
  这会让重定向出来的日志出现乱码或 `U+FFFD`。这是**调用方的 shell 问题**，不是测试缺陷；
  日志要干净就带上 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` 跑，或者用 Python 直接抓字节再按 UTF-8 解码。
  实测记录：修复后再复现，严格按 UTF-8 解码**父进程输出**仍会在父进程自己那几行中文上失败（如 `0xb4`），
  而子进程输出严格解码通过——两者要分开判断，别混为一谈。
- 刻意**不**在这里 `reconfigure(encoding="utf-8")`：那会把交互式控制台上正常显示的中文变成乱码，
  代价大于收益。宁可日志乱码（看得见、能修），也不要"测试变红"（看不出、会误判）。
"""

import os
import sys

#: 子进程 stdio 的编码基线。`setdefault`：调用者显式指定的值优先。
CHILD_STDIO_DEFAULTS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}

for _key, _value in CHILD_STDIO_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
del _key, _value


def _harden(stream) -> None:
    """让"打印证据"这件事不再可能因为编码而抛异常。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # 被替换过的流对象（例如测试里的捕获器）
        return
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError):  # 已经写过东西 / 不是可重配的文本流
        pass


_harden(sys.stdout)
_harden(sys.stderr)
