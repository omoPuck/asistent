"""契约版本与模式版本（SPEC §15.1、§19、§21.1）。

契约冻结后，任何签名/语义变更必须先提案 → 主 Agent 裁决 → 发布新契约版本。
"""

from __future__ import annotations

from typing import Any, Dict

#: 人类可读契约版本，冻结时打 git tag `contracts-v0.1` … `contracts-v0.8`
CONTRACT_VERSION = "v0.9"

#: 每个持久化对象的模式版本（写入对象时记录，便于迁移）
SCHEMA_VERSION = 1

#: 事件模式的独立版本（事件可跨对象版本演进）
EVENT_SCHEMA_VERSION = 1


def snapshot() -> Dict[str, Any]:
    """返回契约快照，供 contracts/contracts-v0.1.json 一致性校验。"""
    from . import enums, objects, transitions

    return {
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "states": enums.snapshot(),
        "machines": transitions.snapshot(),
        "objects": objects.snapshot(),
    }
