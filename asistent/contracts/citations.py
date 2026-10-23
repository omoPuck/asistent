"""资料引用模型（SPEC §6.2、R-006）。

回答、行动建议和草稿引用资料时，至少提供：来源对象与路径/标识、原文位置、
原文时间或有效时间、使用时的版本/哈希、以及该引用支持的结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .timeutil import DISPLAY_TZ_NAME, to_display


@dataclass
class Citation:
    """一条可定位到原文的引用。字段缺失即视为不合格引用。"""

    #: 来源对象 ID（SourceDocument / Fact / DraftVersion ...）
    source_object_id: str
    #: 来源类型，便于跨对象反查
    source_object_type: str
    #: 文件路径或外部标识（可读位置）
    path: str
    #: 原文定位：`line:12-18` / `message:<uid>/part:1` / `paragraph:3` / `kv:subject`
    locator: str
    #: 原文时间（原文中声明的时间）或资料有效时间
    source_time: Optional[str] = None
    #: 使用时的版本号（对象 revision 或文件版本）
    source_version: Optional[int] = None
    #: 使用时的内容哈希（sha256）
    content_hash: Optional[str] = None
    #: 该引用支持的结论（一句话）
    supports: str = ""
    #: 引用者（哪个 Agent/Execution 用了它）
    used_by_execution_id: Optional[str] = None
    #: 展示时区名，仅用于 UI 呈现
    display_time_zone: str = DISPLAY_TZ_NAME
    #: 摘录片段（不用于代替原文）
    excerpt: str = ""

    def validate(self) -> None:
        missing: List[str] = []
        for name in ("source_object_id", "source_object_type", "path", "locator", "supports"):
            if not getattr(self, name):
                missing.append(name)
        if not self.content_hash:
            missing.append("content_hash")
        if missing:
            raise ValueError("引用缺少必填字段（SPEC §6.2）: " + ", ".join(missing))

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "source_object_id": self.source_object_id,
            "source_object_type": self.source_object_type,
            "path": self.path,
            "locator": self.locator,
            "source_time": self.source_time,
            "source_version": self.source_version,
            "content_hash": self.content_hash,
            "supports": self.supports,
            "used_by_execution_id": self.used_by_execution_id,
            "display_time_zone": self.display_time_zone,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Citation":
        obj = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        obj.validate()
        return obj

    def describe(self) -> str:
        """人类可读的引用串，用于手机端与审计展示。"""
        stamp = f" @{to_display(self.source_time)}" if self.source_time else ""
        version = f" v{self.source_version}" if self.source_version is not None else ""
        return f"[{self.source_object_type}:{self.source_object_id} {self.path}#{self.locator}{version}{stamp}] {self.supports}"


@dataclass
class EvidenceSet:
    """一组引用 + 结论：草稿、行动建议、规则命中的依据载体。"""

    conclusion: str
    citations: List[Citation] = field(default_factory=list)
    #: 事实与推断必须分开标注（SPEC §5.4）
    contains_inference: bool = False
    uncertainty_notes: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.conclusion:
            raise ValueError("EvidenceSet.conclusion 必填")
        for citation in self.citations:
            citation.validate()

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "conclusion": self.conclusion,
            "citations": [c.to_dict() for c in self.citations],
            "contains_inference": self.contains_inference,
            "uncertainty_notes": list(self.uncertainty_notes),
        }


def lines_citation(
    *,
    source_object_id: str,
    path: str,
    start_line: int,
    end_line: int,
    content_hash: str,
    supports: str,
    source_version: Optional[int] = None,
    source_object_type: str = "SourceDocument",
) -> Citation:
    """按行号范围构造引用（Markdown/文本资料的常见定位方式）。"""
    citation = Citation(
        source_object_id=source_object_id,
        source_object_type=source_object_type,
        path=path,
        locator=f"line:{start_line}-{end_line}",
        source_version=source_version,
        content_hash=content_hash,
        supports=supports,
    )
    citation.validate()
    return citation
