"""时间语义（SPEC §21.1）。

规则：
- 持久化一律 UTC ISO-8601（毫秒 + `Z`），比较用字符串即可（同格式字典序 = 时间序）；
- 展示默认 `Asia/Shanghai`；
- 相对日期（"明天"）按**原文发出时间及其时区**解释，不按处理当天重新解释；
- `TemporalField` 保留原文标签与语义，禁止把"报名截止时间"当"活动开始时间"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

DISPLAY_TZ_NAME = "Asia/Shanghai"

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """UTC ISO-8601 字符串，毫秒精度，带 `Z`。非 UTC 输入会被转换。"""
    if moment.tzinfo is None:
        raise ValueError("naive datetime 不允许入库：时间必须带时区")
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utcnow_iso() -> str:
    return to_iso(utcnow())


def parse_iso(value: str) -> datetime:
    if not _ISO_Z.match(value):
        raise ValueError(f"时间格式必须是 UTC ISO-8601（毫秒 + Z）: {value!r}")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def add_seconds(iso_value: str, seconds: int) -> str:
    return to_iso(parse_iso(iso_value) + timedelta(seconds=seconds))


def is_expired(iso_value: str, *, now: Optional[str] = None) -> bool:
    return parse_iso(iso_value) <= parse_iso(now or utcnow_iso())


def display_tz() -> Any:
    """返回展示时区。优先 `zoneinfo`（3.9+ 标准库），失败时退化为固定 UTC+8。"""
    try:  # pragma: no cover - 依赖 tzdata
        from zoneinfo import ZoneInfo

        return ZoneInfo(DISPLAY_TZ_NAME)
    except Exception:  # pragma: no cover
        return timezone(timedelta(hours=8))


def to_display(iso_value: str) -> str:
    return parse_iso(iso_value).astimezone(display_tz()).isoformat(timespec="seconds")


@dataclass
class TemporalField:
    """时间字段：原文标签 + 语义 + 来源时区 + 归一化时间（SPEC §7.4、§21.1）。

    语义（`semantic`）是必填项且必须来自原文标签，例如：
    `registration_deadline`（报名截止）、`event_start`（活动开始）、
    `submission_deadline`、`meeting_time`、`reply_deadline`、`unknown`。
    """

    #: 原文中出现的标签，如「报名截止时间」
    original_label: str
    #: 语义槽位；`unknown` 表示未能判定，必须追问而不是猜测
    semantic: str
    #: 原文时间文本，如「10月12日 23:59」
    original_text: str
    #: 来源时区名（如 Asia/Shanghai）
    source_time_zone: str = DISPLAY_TZ_NAME
    #: 归一化 UTC 时间；无法解析时为 None
    normalized_utc: Optional[str] = None
    #: 解析置信度 0..1；<1 时必须标记不确定
    confidence: float = 1.0
    #: 依据引用（Citation.source_object_id 列表）
    evidence: list = field(default_factory=list)

    SEMANTIC_SLOTS = (
        "registration_deadline",
        "event_start",
        "event_end",
        "submission_deadline",
        "reply_deadline",
        "meeting_time",
        "payment_deadline",
        "other",
        "unknown",
    )

    def validate(self) -> None:
        if not self.original_label:
            raise ValueError("TemporalField.original_label 必填（禁止丢弃原文标签）")
        if self.semantic not in self.SEMANTIC_SLOTS:
            raise ValueError(f"未知的时间语义槽位: {self.semantic}")
        if self.semantic == "unknown" and self.normalized_utc is not None:
            raise ValueError("语义未判定时不得给出归一化时间：必须追问（SPEC §7.4）")
        if self.normalized_utc is not None:
            parse_iso(self.normalized_utc)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 必须在 0..1")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "original_label": self.original_label,
            "semantic": self.semantic,
            "original_text": self.original_text,
            "source_time_zone": self.source_time_zone,
            "normalized_utc": self.normalized_utc,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemporalField":
        obj = cls(
            original_label=data["original_label"],
            semantic=data["semantic"],
            original_text=data["original_text"],
            source_time_zone=data.get("source_time_zone", DISPLAY_TZ_NAME),
            normalized_utc=data.get("normalized_utc"),
            confidence=data.get("confidence", 1.0),
            evidence=list(data.get("evidence", [])),
        )
        obj.validate()
        return obj


def resolve_relative_date(phrase: str, *, anchor_iso: str, tz_name: str) -> str:
    """把「明天/后天/今天」按**原文发出时间**与来源时区解析（SPEC §21.1）。

    只支持确定性表述；其余抛 ValueError，由调用方转成追问。
    """
    offsets = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
    if phrase not in offsets:
        raise ValueError(f"无法确定性解析的相对日期: {phrase!r}（应改为追问而不是猜测）")
    anchor = parse_iso(anchor_iso).astimezone(display_tz())
    target = anchor + timedelta(days=offsets[phrase])
    return target.date().isoformat()
