"""MVP-6 成长资产 — 适用范围与效果的**机器可读**说明（SPEC §10.2、§10.3）。

为什么需要这个文件
==================

契约里的规则对象 `Rule` 只有这些字段可用：`statement`（人可读文本）、`state`、
`version`、`scope`（`global|contact|project|case_type`）、`scope_ref`、`priority`、
`source_case_id`、`source_user_message`、`examples`、`confirmed_by/at`、
`effective_from`、`revoked_at`、`superseded_by`。

也就是说：**契约没有给"规则作用在哪个字段/槽位、要做成什么"留结构化字段**。
如果只把规则写成一句自然语言（"报名截止和活动开始不是一个字段"），那"遵守了规则"就只能
靠 Agent 自称——这正是 MVP-6 要反对的做法。所以我们把规则的**适用范围 + 效果**编码进
`Rule.scope_ref`，用一个**带版本号、可解析、可校验**的说明串（`v=1;kind=...;effect=...`）：

    v=1;kind=contact;ref=notice@example.org;subject_re=研学营;\
effect=slot_split.require_question;slots=registration_deadline>event_start

- `kind` 严格取契约允许的四种取值之一（`scope` 字段本身也写同一个值，保持契约语义）；
- `ref` / `subject_re` 是适用范围（哪个邮箱/发件人、哪个主题模式）；
- `effect` / `slots` 是**规则的机器可读内容**：对哪两个语义槽位做什么。
- 解析失败、效果不在词表里、槽位不在契约的 `TemporalField.SEMANTIC_SLOTS` 里
  → 一律 `ValidationError`（**拒绝**，不是"忽略不认识的效果"）。

效果词表（闭集）
================

| 效果 | 含义（只在"有 `slots[0]`、没有 `slots[1]`"时触发） |
|---|---|
| `slot_split.require_question` | 必须追问 `slots[1]`，**不得**用 `slots[0]` 顶替 |
| `slot_split.allow_alias` | 允许按规则把 `slots[0]` 当作 `slots[1]`（显式、留痕、可审计） |

两条规则对**同一个目标槽位**给出**不同效果**，就是冲突（SPEC §10.3"规则冲突时不得静默选一个"）；
冲突的表示见 `CONTRACT.md` §14.5：`Question(kind="conflict")` + `Case.blocked_reason`。

CP 提案（见 `docs/mvp/mvp-6.md` §8）：建议契约给 `Rule` 加结构化 `applies_to` / `effect`；
本模块的编码是**在不改契约前提下的保守做法**，且编码自带 `v=1` 版本号，将来契约补齐后
可以平滑迁移（`parse_scope` 只认 `v=1`，别的版本直接拒绝）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contracts.errors import ValidationError
from ..contracts.timeutil import TemporalField

#: 说明串的版本（只有 v1；遇到未知版本直接拒绝，不做"猜着解析"）
SPEC_VERSION = 1

#: 契约允许的适用范围种类
SCOPE_KINDS: Tuple[str, ...] = ("global", "contact", "project", "case_type")

#: 效果：缺目标槽位时必须追问（不得用来源槽位顶替）
EFFECT_REQUIRE_QUESTION = "slot_split.require_question"
#: 效果：允许把来源槽位当作目标槽位（显式顶替，留痕）
EFFECT_ALLOW_ALIAS = "slot_split.allow_alias"

#: 效果闭集：不认识的效果一律拒绝
EFFECTS: Tuple[str, ...] = (EFFECT_REQUIRE_QUESTION, EFFECT_ALLOW_ALIAS)

#: 效果的人可读说明（写进 `Rule.statement` 与追问文本）
EFFECT_LABELS: Dict[str, str] = {
    EFFECT_REQUIRE_QUESTION: "缺失时必须追问，不得用另一个槽位顶替",
    EFFECT_ALLOW_ALIAS: "允许把来源槽位按规则当作目标槽位",
}

_SLOT_LABELS: Dict[str, str] = {
    "registration_deadline": "报名截止时间",
    "submission_deadline": "提交截止时间",
    "reply_deadline": "回复截止时间",
    "payment_deadline": "缴费截止时间",
    "event_start": "活动开始时间",
    "event_end": "活动结束时间",
    "meeting_time": "会议时间",
}


def slot_label(slot: str) -> str:
    """槽位的人类可读名（没有登记就原样返回，不编造）。"""
    return _SLOT_LABELS.get(slot, slot)


def _escape(value: str) -> str:
    """只转义会破坏说明串语法的三个字符，其余保持可读。"""
    return str(value).replace("%", "%25").replace(";", "%3B").replace("=", "%3D")


def _unescape(value: str) -> str:
    return str(value).replace("%3B", ";").replace("%3D", "=").replace("%25", "%")


def _validate_slot(slot: str, *, where: str) -> str:
    text = str(slot or "").strip()
    if not text:
        raise ValidationError("%s 需要非空槽位名" % where)
    if text not in TemporalField.SEMANTIC_SLOTS:
        raise ValidationError(
            "%s 的槽位 %r 不在契约语义槽位表里（可用：%s）"
            % (where, text, ", ".join(TemporalField.SEMANTIC_SLOTS))
        )
    return text


@dataclass(frozen=True)
class RuleScope:
    """一条规则的适用范围 + 机器可读效果。

    不变量（构造即校验，错了就拒绝）：

    - `effect` ∈ `EFFECTS`；
    - `slots` 对 `slot_split.*` 恰好两个槽位，且都在契约语义槽位表里；
    - `kind` ∈ `SCOPE_KINDS`；`kind == "global"` 时 `ref` 必须为空；
      `kind != "global"` 时 `ref` 必须非空（否则"范围"名不副实）；
    - `subject_re` 必须是可编译的正则。
    """

    effect: str
    slots: Tuple[str, str]
    kind: str = "global"
    ref: str = ""
    subject_re: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        name = str(self.effect or "").strip()
        if name not in EFFECTS:
            raise ValidationError(
                "未知规则效果 %r（允许：%s）——不认识的规则必须拒绝，不能静默忽略"
                % (self.effect, ", ".join(EFFECTS))
            )
        slots = tuple(str(item or "").strip() for item in (self.slots or ()))
        if len(slots) != 2:
            raise ValidationError("效果 %s 需要两个槽位（来源>目标），实际 %r" % (name, slots))
        first = _validate_slot(slots[0], where="来源槽位")
        second = _validate_slot(slots[1], where="目标槽位")
        if first == second:
            raise ValidationError("来源槽位与目标槽位不能相同：%s" % first)
        kind = str(self.kind or "").strip() or "global"
        if kind not in SCOPE_KINDS:
            raise ValidationError("未知适用范围种类 %r（允许：%s）" % (self.kind, ", ".join(SCOPE_KINDS)))
        ref = str(self.ref or "").strip()
        if kind == "global" and ref:
            raise ValidationError(
                "global 范围不允许带 ref（要限定发件人请用 kind=contact）：%r" % ref
            )
        if kind != "global" and not ref:
            raise ValidationError("范围种类 %s 必须给出 ref" % kind)
        pattern = str(self.subject_re or "").strip()
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:  # pragma: no cover - 只有写错正则才走到
                raise ValidationError("subject_re 不是合法正则: %r（%s）" % (pattern, exc)) from exc
        object.__setattr__(self, "effect", name)
        object.__setattr__(self, "slots", (first, second))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "ref", ref)
        object.__setattr__(self, "subject_re", pattern)
        object.__setattr__(self, "note", str(self.note or ""))

    # ------------------------------------------------------------------ 派生视图
    @property
    def source_slot(self) -> str:
        return self.slots[0]

    @property
    def target_slot(self) -> str:
        return self.slots[1]

    @property
    def spec(self) -> str:
        """序列化成写进 `Rule.scope_ref` 的说明串。"""
        parts = [
            ("v", str(SPEC_VERSION)),
            ("kind", self.kind),
            ("ref", self.ref),
            ("subject_re", self.subject_re),
            ("effect", self.effect),
            ("slots", "%s>%s" % self.slots),
        ]
        return ";".join("%s=%s" % (key, _escape(value)) for key, value in parts)

    @property
    def logical_key(self) -> str:
        """同一条"逻辑规则"的全部版本共用这个键（版本号不在里面）。"""
        return "|".join([self.kind, self.ref, self.subject_re, self.effect, ">".join(self.slots)])

    @property
    def effect_label(self) -> str:
        return EFFECT_LABELS.get(self.effect, self.effect)

    def describe(self) -> str:
        """人可读的一句话（写进 `Rule.statement` / 追问背景）。"""
        target = "「%s」" % slot_label(self.target_slot)
        source = "「%s」" % slot_label(self.source_slot)
        if self.kind == "global":
            where = "所有通知"
        elif self.kind == "contact":
            where = "来自 %s 的通知" % self.ref
        else:
            where = "%s=%s 的输入" % (self.kind, self.ref)
        if self.subject_re:
            where += "（主题匹配 /%s/）" % self.subject_re
        return "%s：有%s而没有%s时，%s" % (where, source, target, self.effect_label)

    # ------------------------------------------------------------------ 匹配
    def matches(self, *, sender: str = "", subject: str = "", refs: Sequence[str] = ()) -> bool:
        """这条规则是否适用于本次输入。

        只做**确定性**匹配，不做模糊猜测：

        - `contact`：把发件人头部里的**邮箱地址**抽出来逐个比较
          （`市青少年科技中心 <notice@example.org>` 里的 `notice@example.org` 要能匹配上）；
          `ref` 以 `@` 开头时按域名后缀匹配；
        - `project` / `case_type`：`ref` 精确匹配调用方给出的范围引用，或出现在主题里；
        - `subject_re`：大小写不敏感正则 `search`，与上面的条件**同时**成立才算匹配。
        """
        raw_sender = str(sender or "").strip()
        lowered_sender = raw_sender.lower()
        lowered_ref = self.ref.lower()
        if self.kind == "contact":
            if not lowered_sender:
                return False
            if lowered_ref.startswith("@"):
                if not any(item.endswith(lowered_ref) for item in _addresses(lowered_sender)):
                    return False
            elif lowered_ref not in _addresses(lowered_sender) and lowered_sender != lowered_ref:
                return False
        elif self.kind in ("project", "case_type"):
            haystack = {str(item).strip().lower() for item in refs}
            if lowered_ref not in haystack and lowered_ref not in str(subject or "").lower():
                return False
        if self.subject_re:
            if not re.search(self.subject_re, str(subject or ""), re.IGNORECASE):
                return False
        return True


#: 从发件人头部里抽邮箱地址（`显示名 <addr@host>` / 裸地址 / 多个地址都能抽）
_ADDRESS_RE = re.compile(r"[^<>\s,;\"']+@[^<>\s,;\"']+")


def _addresses(text: str) -> set:
    return {item.strip().lower() for item in _ADDRESS_RE.findall(text or "") if "@" in item}


def parse_scope(text: Optional[str]) -> RuleScope:
    """把 `Rule.scope_ref` 解析回 `RuleScope`。解析不了就拒绝（不猜）。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValidationError("规则缺少适用范围说明（scope_ref 为空）")
    fields: Dict[str, str] = {}
    for chunk in raw.split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        key, sep, value = piece.partition("=")
        if not sep:
            raise ValidationError("适用范围说明格式错误（应为 key=value 用 ; 分隔）: %r" % raw)
        fields[key.strip()] = _unescape(value.strip())
    version = fields.get("v", "")
    if version != str(SPEC_VERSION):
        raise ValidationError(
            "适用范围说明版本 %r 不受支持（本实现只认 v=%d）" % (version, SPEC_VERSION)
        )
    missing = [key for key in ("kind", "effect", "slots") if key not in fields]
    if missing:
        raise ValidationError("适用范围说明缺少字段 %s: %r" % (", ".join(missing), raw))
    slots_raw = fields.get("slots", "")
    slots = tuple(item.strip() for item in slots_raw.split(">"))
    return RuleScope(
        effect=fields["effect"],
        slots=slots,  # type: ignore[arg-type]
        kind=fields.get("kind", "global"),
        ref=fields.get("ref", ""),
        subject_re=fields.get("subject_re", ""),
        note=fields.get("note", ""),
    )


def format_scope(scope: RuleScope) -> str:
    return scope.spec


@dataclass(frozen=True)
class MatchInput:
    """一次匹配的输入（纯数据，方便测试直接构造）。"""

    sender: str = ""
    subject: str = ""
    refs: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"sender": self.sender, "subject": self.subject, "refs": list(self.refs)}


def describe_effects(effects: Iterable[str]) -> str:
    """把若干效果拼成人可读串（冲突展示用）。"""
    return " / ".join("%s（%s）" % (name, EFFECT_LABELS.get(name, name)) for name in effects)


def sort_by_version(rules: Sequence[Any]) -> List[Any]:
    """按 `version` 升序（版本号是权威顺序，不依赖时间戳精度）。"""
    return sorted(rules, key=lambda item: int(getattr(item, "version", 0)))


__all__ = [
    "SPEC_VERSION",
    "SCOPE_KINDS",
    "EFFECTS",
    "EFFECT_ALLOW_ALIAS",
    "EFFECT_LABELS",
    "EFFECT_REQUIRE_QUESTION",
    "MatchInput",
    "RuleScope",
    "describe_effects",
    "format_scope",
    "parse_scope",
    "slot_label",
    "sort_by_version",
]
