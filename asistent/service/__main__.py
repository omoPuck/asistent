"""`python3 -m asistent.service` 的入口。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - 部署入口
    sys.exit(main())
