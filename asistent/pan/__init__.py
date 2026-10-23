"""归档通道层（SPEC §12、§21.3、§21.4）。

- `bdpan.py`：真实通道 —— 设备上的官方 `bdpan` CLI → 契约 `PanClient`（写锁 + 审批引用）；
- `errors.py`：通道错误分类（确定失败 / 结果未知 / 未授权）；
- 离线回路直接用契约自带的 `asistent.contracts.pan.LocalDirPanClient`，本包不重复实现模拟远端。

边界（ADR-0003）：`LocalDirPanClient` 只是**模拟远端**，不构成真实网盘能力；
宣称"已归档到百度网盘"必须走 `BdpanCliPanClient`，且必须逐文件哈希回读校验通过。
"""

from __future__ import annotations

from .bdpan import (
    BDPAN_DEFAULT_BINARY,
    DEFAULT_BINARY_CANDIDATES,
    RESULT_FAILED_SAFE,
    RESULT_SUCCEEDED,
    RESULT_UNKNOWN,
    BdpanCliPanClient,
    BdpanProbe,
    normalize_remote,
    probe_bdpan,
    resolve_binary,
)
from .errors import (
    LOGIN_NEXT_STEP,
    PanError,
    PanNotAuthorized,
    PanNotInstalled,
    PanUnavailable,
    PanWriteLocked,
)

__all__ = [
    "BDPAN_DEFAULT_BINARY",
    "DEFAULT_BINARY_CANDIDATES",
    "LOGIN_NEXT_STEP",
    "RESULT_FAILED_SAFE",
    "RESULT_SUCCEEDED",
    "RESULT_UNKNOWN",
    "BdpanCliPanClient",
    "BdpanProbe",
    "PanError",
    "PanNotAuthorized",
    "PanNotInstalled",
    "PanUnavailable",
    "PanWriteLocked",
    "normalize_remote",
    "probe_bdpan",
    "resolve_binary",
]
