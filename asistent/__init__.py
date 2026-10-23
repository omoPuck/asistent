"""asistent —— 会成长的 24h on-call 个人助手（实现包）。

核心约束（见 docs/adr/ADR-0001）：
- 纯标准库，Python 3.10+ 兼容（设备为 3.10.12，开发机 3.12.x）；
- 业务语义只来自 asistent/contracts（共享对象与状态机），MVP 不得自造同义对象。
"""

__all__ = ["contracts"]
__version__ = "0.1.0"
