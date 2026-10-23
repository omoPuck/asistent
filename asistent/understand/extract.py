"""确定性规则/模板抽取器（SPEC §7.4、§21.1、MVP-3 验收第 1 条）。

设计要点
========

1. **中文优先、纯标准库、零模型调用**。同一输入 → 同一输出：所有规则都是正则 + 固定表，
   没有随机、没有时间依赖（"今天/明天"以**邮件发出时间**为锚，不用处理时刻，SPEC §21.1）。
2. **时间必须带语义**。`TemporalField.original_label`（原文写法）与 `semantic`（语义槽位）
   分开保存：`报名截止时间` 只能落到 `registration_deadline`，`活动开始时间` 只能落到
   `event_start`。判定不出来的表达**不猜**：它落成 `unknown` 或变成追问候选。
3. **无引用不产物**。每个时间字段/待办/材料都通过 `Citation` 定位到原文行，
   `Citation.validate()` 缺字段即抛错；给不出引用的内容只会出现在 `inferences` / `questions`。
4. **冲突交给上层**。本模块只在**单文档内**发现"同一个语义槽位出现两个不同时间值"
   （`TimeField` 列表里同 semantic 多值），跨文档冲突由 `understander.cross_check` 处理。

时间解析细节（容易出错的地方，都有测试钉住）：

- 表达式形如 `10月12日 23:59`、`2026年10月12日23:59`、`10月12日 下午3点`、`12月1日至12月3日`、
  `明天 18:00`、`下周五`、`2026-10-12 23:59`；
- 只在日期里带 `月/日` 时，年份取锚（邮件发出时间）所在年；解析出的时间**早于锚 30 天以上**
  视为"跨年"（例如 12 月 30 日收到、通知"1月3日"），年份 +1；
- 表达式本身没写时刻时**不发明时刻**：`normalized_utc` 留 `None`、`confidence = 0.8`，
  由追问确认（宁可追问，不要凭空写一个 23:59）；
- `下周五`/`本周三` 这类相对表达在 `resolve_relative_date`（契约）不支持的范围内，
  本模块**自己解析周几**并给出 `confidence = 0.6`（仍标记不确定）；
- 时区：邮件 `Date` 头带 `+08:00` 时按其偏移解释，否则用展示时区 `Asia/Shanghai`。
"""

from __future__ import annotations

import email
import email.policy
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import Header, decode_header, make_header
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..contracts.citations import Citation
from ..contracts.timeutil import DISPLAY_TZ_NAME, TemporalField, to_iso
from .model import (
    CitationRef,
    MaterialRequirement,
    QuestionCandidate,
    SourceText,
    TimeField,
    TodoItem,
    hash_text,
    merge_time_fields,
)

__all__ = [
    "RULESET_VERSION",
    "ExtractionResult",
    "LabelRule",
    "TimeExpression",
    "LABEL_RULES",
    "DECLARATIVE_TIME_RULES",
    "TIME_EXPRESSION_RE",
    "PENDING_TIME_RE",
    "OWED_TIME_PREFIX",
    "PendingTimeTag",
    "find_pending_time_tags",
    "parse_source_text",
    "safe_text",
    "decode_header_value",
    "email_message_to_source_text",
    "decode_eml_bytes",
    "extract",
    "extract_time_expressions",
    "label_for",
    "parse_expression",
    "resolve_weekday",
    "timezone_from_date_header",
    "REQUIREMENT_MARKERS",
    "MATERIAL_MARKERS",
    "reply_requested",
]

RULESET_VERSION = "rules:mvp3-1"

#: 正文里用于判定的"要求"标记（催办/请求）——命中即为"需要回复或行动"的依据
REQUIREMENT_MARKERS: Tuple[str, ...] = (
    "请回复",
    "请确认",
    "请于",
    "请在",
    "请提交",
    "请提供",
    "请务必",
    "务必",
    "回复确认",
    "回复我",
    "确认是否",
    "需要回复",
    "需回复",
    "敬请回复",
    "烦请",
    "请及时",
)

#: 材料类标记
MATERIAL_MARKERS: Tuple[str, ...] = (
    "缺少",
    "缺",
    "未提交",
    "尚未提交",
    "未提供",
    "没有提供",
    "需提交",
    "需要提交",
    "请提交",
    "请提供",
    "需补",
    "需补齐",
    "补交",
    "missing",
)

#: 未标明语义的时间表达前面出现这些词时，说明它**是**某个时间要求的取值
OWED_TIME_PREFIX: Tuple[str, ...] = (
    "截止",
    "前",
    "于",
    "在",
    "到",
    "至",
    "为",
    "是",
    "：",
    ":",
    "，",
    ",",
    "（",
    "(",
    "时间",
)

#: 日/月数字的"独立成数"约束：`(?<!\d)` … `(?!\d)`。
#: 否则 `2026年10月5日` 会被拆出 `10月5日` 之外的东西（实测踩过：原本会匹配到 "2026年10"）。
_DAY = r"(?<!\d)\d{1,2}(?!\d)"

#: 行内时间表达：按"最长优先"匹配（`TIME_EXPRESSION_RE` 的备选顺序即优先级）
_TIME_PATTERN = "|".join(
    (
        # 2026年10月5日 / 10月5日（年份可选；日字可省）
        r"(?:\d{4}\s*[-/年]\s*)?%s\s*月\s*%s\s*日?" % (_DAY, _DAY),
        # 2026-10-05 / 2026/10/5
        r"\d{4}\s*[-/年]\s*%s\s*[-/月]\s*%s" % (_DAY, _DAY),
        # 10/5
        r"(?<!\d)%s\s*[-/]\s*%s(?!\d)" % (_DAY, _DAY),
        r"(?:今天|今日|明天|明日|后天|大后天)",
        r"(?:本|下|这)?周[一二三四五六日天]",
        r"(?:本|下|这)?星期[一二三四五六日天]",
    )
)
_TIME_OF_DAY = (
    r"(?:[上下]午|晚上?|中午|清晨|凌晨|早上)?\s*"
    r"(?:\d{1,2}\s*[:：]\s*\d{1,2}(?:\s*[:：]\s*\d{1,2})?"
    r"|\d{1,2}\s*[点時时](?:\s*\d{1,2}\s*分?)?)"
)

#: 区间尾：可以是完整表达（`12月3日 10:00`），也可以**只写时刻**（`14:00 至 16:30`）
_RANGE_TAIL = r"(?:\s*(?:至|到|-|~|—|–)\s*(?:(?:%s)(?:\s*(?:%s))?|%s))?" % (
    _TIME_PATTERN,
    _TIME_OF_DAY,
    _TIME_OF_DAY,
)

TIME_EXPRESSION_RE = re.compile("(?:%s)(?:%s)?%s" % (_TIME_PATTERN, _TIME_OF_DAY, _RANGE_TAIL))

#: 「时间待定」类表达：标签存在但值不存在 —— 这**不是**"没有时间字段"，
#: 而是"这个字段的值缺失"，必须追问（SPEC §21.1「不明确则追问」）。
PENDING_TIME_RE = re.compile(
    r"(待定|待确认|另行通知|另行告知|另行安排|稍后通知|暂未确定|尚未确定|未定|另定)"
)


@dataclass
class PendingTimeTag:
    """「报名截止时间：待定」这类：语义已知、值缺失。"""

    label: str
    semantic: str
    display: str
    text: str
    start: int
    end: int
    keyword: str



def safe_text(value: Any, *, limit: int = 4000) -> str:
    """把任意文本变成可安全入库/落 JSON 的字符串（去掉代理区与控制字符）。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    text = text.replace("\x00", "")
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _decode_header_chunk(value: Any, charset: Optional[str]) -> str:
    """把 `decode_header()` 给的一块 `(bytes|str, charset)` 解成文本。"""
    if isinstance(value, str):
        return value
    if charset is None or str(charset).lower() in ("unknown-8bit", "unknown", "8bit", "x-unknown", "default"):
        # 非 ASCII 直写头在 compat32 下就是这里：charset 是「不知道」，按 UTF-8 试
        for candidate in ("utf-8", "latin-1"):
            try:
                return value.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                continue
        return value.decode("ascii", "replace")
    for candidate in (str(charset), str(charset).replace("_", "-"), str(charset).replace("-", "_")):
        try:
            return value.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return value.decode("utf-8", "replace")


def decode_header_value(raw: Any) -> str:
    """解码头字段（RFC 2047 `=?utf-8?B?...?=` **以及**非 ASCII 直写头）。

    三种输入都要能处理（都实测过）：

    1. `str` —— 已解码文本；
    2. `email.header.Header`（默认 `compat32` policy）：先 `encode()` 取回 RFC 2047 字节串；
    3. RFC 2047 编码串 / 直写非 ASCII 串。

    不能用 `make_header()` 收尾：非 ASCII 直写头在 compat32 下 charset 是 `unknown-8bit`，
    `make_header` 会把每个非 ASCII 字节变成 U+FFFD（实测踩过：整条 Subject 变 `\\ufffd`）。
    """
    if raw is None:
        return ""
    if isinstance(raw, Header):
        raw = raw.encode()
    elif not isinstance(raw, str):
        raw = str(raw)
    if any("\udc80" <= char <= "\udcff" for char in raw):  # surrogateescape 代理字节
        try:
            return safe_text(raw.encode("ascii", "surrogateescape").decode("utf-8"))
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    try:
        chunks = decode_header(raw)
    except Exception:  # pragma: no cover - decode_header 极少失败
        return safe_text(raw)
    return safe_text("".join(_decode_header_chunk(value, charset) for value, charset in chunks))


def decode_raw_header_bytes(value: bytes) -> str:
    """把"原始头字节"解成文本：先按 UTF-8，再按 RFC 2047，最后退回 ASCII+替换。"""
    if not value:
        return ""
    try:
        return safe_text(value.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    try:
        return safe_text(value.decode("latin-1"))
    except Exception:  # pragma: no cover - latin-1 不会失败
        return safe_text(value.decode("ascii", "replace"))


# --------------------------------------------------------------------------------------
# 规则表
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelRule:
    """时间标签规则：正则 → 语义槽位。"""

    pattern: str
    semantic: str
    label: str
    #: 展示用的人类可读名
    display: str = ""

    def regex(self) -> "re.Pattern":
        return re.compile(self.pattern)


#: 顺序 = 优先级（先匹配到的先用；同一跨度只能被一条规则占用）
LABEL_RULES: Tuple[LabelRule, ...] = (
    LabelRule(r"报名截止(?:时间|日期|日)?", "registration_deadline", "报名截止", "报名截止时间"),
    LabelRule(r"截止报名(?:时间|日期|日)?", "registration_deadline", "截止报名", "报名截止时间"),
    LabelRule(r"(?:报名|登记|注册)(?:的)?截止(?:时间|日期|期限|日)?", "registration_deadline", "报名截止时间", "报名截止时间"),
    LabelRule(r"提交截止(?:时间|日期|日)?", "submission_deadline", "提交截止", "提交截止时间"),
    LabelRule(r"(?:材料|作品|论文|文件|答卷)(?:的)?提交截止(?:时间|日期|日)?", "submission_deadline", "材料提交截止", "提交截止时间"),
    LabelRule(r"交稿(?:时间|日期|日)?", "submission_deadline", "交稿", "交稿时间"),
    LabelRule(r"回复截止(?:时间|日期|日)?", "reply_deadline", "回复截止", "回复截止时间"),
    LabelRule(r"答复截止(?:时间|日期|日)?", "reply_deadline", "答复截止", "回复截止时间"),
    LabelRule(r"回执截止(?:时间|日期|日)?", "reply_deadline", "回执截止", "回复截止时间"),
    LabelRule(r"(?:请)?于[^。；;]{0,12}前回复", "reply_deadline", "前回复", "回复截止时间"),
    LabelRule(r"活动开始(?:时间|日期|日)?", "event_start", "活动开始", "活动开始时间"),
    LabelRule(r"活动(?:开始|举办)(?:时间|日期|日)?", "event_start", "活动开始时间", "活动开始时间"),
    LabelRule(r"会议开始(?:时间|日期)?", "event_start", "会议开始", "活动开始时间"),
    LabelRule(r"开始(?:时间|日期)", "event_start", "开始时间", "活动开始时间"),
    LabelRule(r"活动结束(?:时间|日期|日)?", "event_end", "活动结束", "活动结束时间"),
    LabelRule(r"结束(?:时间|日期)", "event_end", "结束时间", "活动结束时间"),
    LabelRule(r"会议时间", "meeting_time", "会议时间", "会议时间"),
    LabelRule(r"开会时间", "meeting_time", "开会时间", "会议时间"),
    LabelRule(r"签到时间", "other", "签到时间", "签到时间"),
    LabelRule(r"缴费截止(?:时间|日期)?", "payment_deadline", "缴费截止", "缴费截止时间"),
    LabelRule(r"付款截止(?:时间|日期)?", "payment_deadline", "付款截止", "缴费截止时间"),
    LabelRule(r"最迟(?:于|在)?", "other", "最迟", "最迟时间"),
    LabelRule(r"截止(?:时间|日期|期限|日)", "other", "截止时间", "截止时间"),
    LabelRule(r"截止[到至]?", "other", "截止", "截止时间"),
)

#: "声明的"时间规则：一句话里只写了时间，没有"截止/开始"这类词，靠动词决定语义。
#: 捕获组 1 = 行为（提交/回复/报名），用于生成 `original_label`（**不改写原文**）。
DECLARATIVE_TIME_RULES: Tuple[LabelRule, ...] = (
    LabelRule(r"报名", "registration_deadline", "报名", "报名截止时间"),
    LabelRule(r"提交", "submission_deadline", "提交", "提交截止时间"),
    LabelRule(r"回复", "reply_deadline", "回复", "回复截止时间"),
    LabelRule(r"答复", "reply_deadline", "答复", "回复截止时间"),
    LabelRule(r"开始", "event_start", "开始", "活动开始时间"),
    LabelRule(r"结束", "event_end", "结束", "活动结束时间"),
)

#: 反向读法（"前提交"、"前回复"）：行为在"前"之后
_REVERSED_ACTION_RE = re.compile(r"前\s*(提交|回复|答复|报名|确认|缴纳|缴费)")

#: "拒绝/无法参加"类表达：用于判定"不建议自动回复"或需要人工决定
DECLINE_MARKERS: Tuple[str, ...] = ("无法参加", "不参加", "不能出席", "无法出席", "请假")

_CONFIRM_MARKERS: Tuple[str, ...] = ("确认", "回复", "回执", "报名", "提交", "参加", "出席")

#: "不用回复"类表达：出现这些就是在**否定**回复要求，不能再按"要求"处理
NO_REPLY_MARKERS: Tuple[str, ...] = (
    "无需回复",
    "不必回复",
    "不用回复",
    "免回复",
    "无需确认",
    "不必确认",
    "请勿回复",
    "不需要回复",
    "无须回复",
    "无需回执",
)

_CLAUSE_BOUNDARY = "。；;\n！？!?"

_MATERIAL_STOPWORDS: Tuple[str, ...] = (
    "材料",
    "资料",
    "文件",
    "证明",
    "复印件",
    "扫描件",
    "照片",
    "信息",
    "内容",
    "以下",
    "以及",
    "和",
    "等",
)


@dataclass
class TimeExpression:
    """一处时间表达的原始匹配（尚未赋予语义）。"""

    text: str
    start: int
    end: int
    #: 表达式里解析出的绝对时刻（UTC ISO）；None 表示无法解析
    normalized_utc: Optional[str] = None
    #: 解析置信度：1.0 = 表达式自带完整日期与时刻；0.8 = 只有日期；0.6 = 只有相对周几
    confidence: float = 1.0
    #: 表达式里是否含时刻
    has_time_of_day: bool = False
    #: 表达式是否是区间（`至/到/-`）
    is_range: bool = False
    #: 区间尾部的归一化时间（若有）
    range_end_utc: Optional[str] = None
    #: 解析失败原因（可观察，不静默）
    error: str = ""

    def time_field(
        self,
        *,
        semantic: str,
        label: str,
        display: str,
        citations: Sequence[CitationRef],
    ) -> TimeField:
        temporal = TemporalField(
            original_label=safe_text(label, limit=64),
            semantic=semantic,
            original_text=safe_text(self.text, limit=64),
            source_time_zone=DISPLAY_TZ_NAME,
            normalized_utc=self.normalized_utc,
            confidence=self.confidence,
            evidence=[item.citation.source_object_id for item in citations],
        )
        return TimeField(
            temporal=temporal,
            citations=list(citations),
            label_text=display or label,
        )


# --------------------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------------------

_YEAR_MONTH_DAY = re.compile(r"(?:\d{4}\s*[-/年]\s*)?%s\s*月\s*%s\s*日" % (_DAY, _DAY))
_MONTH_DAY = re.compile(r"(?<!\d)%s\s*[-/]\s*%s(?!\d)" % (_DAY, _DAY))
_MONTH_DAY_CN = re.compile(r"%s\s*月\s*%s\s*日" % (_DAY, _DAY))
_REL_DAY = re.compile(r"今天|今日|明天|明日|后天|大后天")
_WEEKDAY = re.compile(r"(本|下|这)?周([一二三四五六日天])")
_XINGQI = re.compile(r"(本|下|这)?星期([一二三四五六日天])")
_TIME_OF_DAY_RE = re.compile(
    r"(?P<ampm>[上下]午|晚上|晚|中午|清晨|凌晨|早上)?\s*"
    r"(?P<hour>\d{1,2})\s*(?P<sep>[:：点時时])\s*(?P<minute>\d{1,2})?\s*分?"
)
_RANGE_SEP = re.compile(r"\s*(?:至|到|-|~|—|–)\s*")

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

#: 只有"月/日"时的跨年判定阈值：解析结果早于锚 30 天以上视为明年（如 12/30 收到"1月3日"）
_CROSS_YEAR_THRESHOLD_DAYS = 30


def timezone_from_date_header(raw: Optional[str]) -> Tuple[Any, str]:
    """从邮件 `Date` 头解析时区。返回 `(tzinfo, 展示名)`；无偏移时用展示时区。"""
    text = safe_text(raw, limit=200).strip()
    if text:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
            if parsed is not None and parsed.tzinfo is not None:
                offset = parsed.utcoffset()
                if offset is not None:
                    total_minutes = int(offset.total_seconds() // 60)
                    sign = "+" if total_minutes >= 0 else "-"
                    hours, minutes = divmod(abs(total_minutes), 60)
                    return parsed.tzinfo, "%s%02d:%02d" % (sign, hours, minutes)
        except Exception:
            pass
    return _display_tz(), DISPLAY_TZ_NAME


def _display_tz() -> Any:
    try:  # pragma: no cover - 依赖 tzdata
        from zoneinfo import ZoneInfo

        return ZoneInfo(DISPLAY_TZ_NAME)
    except Exception:  # pragma: no cover
        return timezone(timedelta(hours=8))


def _anchor_datetime(anchor_utc: str, tz: Any, anchor_original: str) -> datetime:
    base = datetime.fromisoformat(_normalize_iso(anchor_utc))
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    candidate = base.astimezone(tz)
    # 邮件 Date 头里如果本来就带同一个偏移，优先用它（保留原文表述的日期）
    text = safe_text(anchor_original, limit=200).strip()
    if text:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
            if parsed is not None and parsed.tzinfo is not None:
                return parsed
        except Exception:
            pass
    return candidate


def _normalize_iso(value: str) -> str:
    text = safe_text(value, limit=64).strip()
    if text.endswith("Z") or text.endswith("z"):
        return text[:-1] + "+00:00"
    return text


def _apply_ampm(hour: int, ampm: str) -> int:
    if ampm in ("下午", "晚上", "晚"):
        if hour < 12:
            return hour + 12
        return hour
    if ampm == "中午":
        return 12 if hour == 12 else (hour + 12 if hour < 6 else hour)
    if ampm in ("上午", "清晨", "凌晨", "早上"):
        if hour == 12:
            return 0
        return hour
    return hour


def _parse_time_of_day(text: str) -> Optional[Tuple[int, int]]:
    match = _TIME_OF_DAY_RE.search(text)
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    hour = _apply_ampm(hour, match.group("ampm") or "")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def resolve_weekday(phrase: str, *, anchor: datetime) -> Optional[datetime]:
    """解析 `本周三` / `下周五` / `周四`。无修饰词时取"不早于锚"的那一天。"""
    match = _WEEKDAY.match(phrase) or _XINGQI.match(phrase)
    if match is None:
        return None
    modifier = match.group(1) or ""
    target_index = _WEEKDAY_INDEX.get(match.group(2))
    if target_index is None:  # pragma: no cover - 正则已限定
        return None
    current_index = anchor.weekday()
    if modifier == "下":
        days_ahead = (7 - current_index) + target_index
    elif modifier in ("本", "这"):
        days_ahead = target_index - current_index
    else:
        days_ahead = (target_index - current_index) % 7
    return anchor + timedelta(days=days_ahead)


def _range_tail(text: str) -> str:
    """取出区间尾部（`10月17日 14:00 至 16:30` → `16:30`）；没有区间时返回空串。

    `-` 只在**两侧都是日期/时刻**时才算区间分隔符：`2026-10-12` 里的 `-` 不是分隔符。
    """
    for match in re.finditer(r"\s*(?:至|到|~|—|–)\s*|(?<=\d)\s*-\s*(?=\d)", text):
        left, right = text[: match.start()], text[match.end() :]
        if not right:
            continue
        if _has_date(left) and (_has_date(right) or _parse_time_of_day(right) is not None):
            return right
    return ""


def parse_expression(
    text: str,
    *,
    anchor: datetime,
    tz: Any,
    time_zone_name: str = DISPLAY_TZ_NAME,
) -> TimeExpression:
    """把一个时间表达解析为 UTC 时刻。

    - 有完整日期 + 时刻 → `confidence = 1.0`；
    - 只有日期 → `normalized_utc` 仍给出，`confidence = 0.8`（"日期确定、时刻未定"）；
    - 相对周几 → `confidence = 0.6`；
    - 可识别日期但完全无法解析 → `normalized_utc = None` 且 `error` 说明原因（**不猜**）。
    """
    expression = TimeExpression(text=safe_text(text, limit=64), start=0, end=len(text))
    tail = _range_tail(text)
    head = text[: len(text) - len(tail)] if tail else text
    if tail:
        expression.is_range = True
    head_time = _parse_time_of_day(head)
    if head_time is not None:
        expression.has_time_of_day = True

    month_day = _parse_date_parts(head)
    if month_day is None:
        # 相对日期 / 周几
        if _REL_DAY.search(head):
            phrase = _REL_DAY.search(head).group(0)
            offsets = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
            day = anchor + timedelta(days=offsets[phrase])
            moment = _combine(day, head_time)
            expression.normalized_utc = to_iso(moment)
            expression.confidence = 1.0 if head_time else 0.8
        elif _WEEKDAY.search(head) or _XINGQI.search(head):
            phrase = (_WEEKDAY.search(head) or _XINGQI.search(head)).group(0)
            day = resolve_weekday(phrase, anchor=anchor)
            if day is None:  # pragma: no cover - 正则已限定
                expression.error = "无法解析周几: %r" % (phrase,)
                return expression
            moment = _combine(day, head_time)
            expression.normalized_utc = to_iso(moment)
            expression.confidence = 1.0 if head_time else 0.6
        else:
            expression.error = "无法解析的时间表达: %r" % (text,)
            return expression
    else:
        parsed_year, month, day = month_day
        # `parsed_year == 0` 表示原文没写年份（`10月20日`）：先用锚年占位，
        # 跨年修正由 `_refine_month_day_years` 负责（12 月底收到"1月3日" → 明年）。
        year = parsed_year or anchor.year
        try:
            local = datetime(year, month, day)
        except ValueError as exc:
            expression.error = "非法日期 %r: %s" % (text, exc)
            return expression
        if not 1 <= month <= 12:
            expression.error = "非法月份: %r" % (text,)
            return expression
        moment = _combine(local.replace(tzinfo=None), head_time)
        expression.normalized_utc = to_iso(moment.replace(tzinfo=tz))
        expression.confidence = 1.0 if head_time else 0.8

    if tail:
        # 区间尾只写时刻时（`14:00 至 16:30`）沿用头部日期；写全日期时用自己的
        tail_time = _parse_time_of_day(tail)
        tail_parts = _parse_date_parts(tail)
        if tail_parts is None:
            tail_year, tail_month, tail_day = month_day if month_day is not None else (0, 0, 0)
            tail_year = tail_year or anchor.year
        else:
            tail_year, tail_month, tail_day = tail_parts
            tail_year = tail_year or (month_day[0] if month_day and month_day[0] else anchor.year)
        if tail_month and tail_day:
            try:
                end_local = datetime(tail_year, tail_month, tail_day)
            except ValueError:
                end_local = None
            if end_local is not None:
                expression.range_end_utc = to_iso(_combine(end_local, tail_time).replace(tzinfo=tz))
    return expression


def _has_date(text: str) -> bool:
    return bool(
        _YEAR_MONTH_DAY.search(text)
        or _MONTH_DAY_CN.search(text)
        or _MONTH_DAY.search(text)
        or _REL_DAY.search(text)
        or _WEEKDAY.search(text)
        or _XINGQI.search(text)
    )


def _combine(day: datetime, time_of_day: Optional[Tuple[int, int]]) -> datetime:
    """把日期与时刻合成一个 datetime（不带 tzinfo，调用方补）。"""
    if time_of_day is None:
        return day.replace(hour=0, minute=0, second=0, microsecond=0)
    hour, minute = time_of_day
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


_DATE_PARTS_CN = re.compile(r"(?:(\d{4})\s*[-/年]\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_PARTS_NUM = re.compile(r"(?:(\d{4})[年\-/])?(\d{1,2})[-/](\d{1,2})")


def _parse_date_parts(text: str) -> Optional[Tuple[int, int, int]]:
    """从表达式里取 `(year, month, day)`。

    `year == 0` 表示原文没写年份（`10月20日`），由 `_refine_month_day_years` 按锚年补全。
    """
    for pattern in (_DATE_PARTS_CN, _DATE_PARTS_NUM):
        match = pattern.search(text)
        if match is None:
            continue
        year = int(match.group(1)) if match.group(1) else 0
        return year, int(match.group(2)), int(match.group(3))
    return None


# --------------------------------------------------------------------------------------
# 原文解析
# --------------------------------------------------------------------------------------


def parse_source_text(
    raw_text: str,
    *,
    subject: str = "",
    from_addr: str = "",
    to_addrs: Optional[Sequence[str]] = None,
    date_header: str = "",
    source_time_zone: str = DISPLAY_TZ_NAME,
    anchor_utc: str = "",
) -> SourceText:
    """构造 `SourceText`：正文行号以**原始全文**为基准（引用必须能在原文里打开）。

    用 `policy=email.policy.default` 解析：`compat32` 会把"非 ASCII 直写头"
    编成 `unknown-8bit` 再变成一串 U+FFFD（实测踩过：Subject 全变 `\ufffd`）。
    """
    import_bytes = raw_text.encode("utf-8")
    message = email.message_from_bytes(import_bytes, policy=email.policy.default)
    body = _plain_body(message)
    prefix = _body_prefix(raw_text, body)
    body_start_line = prefix.count("\n") + 1 if prefix is not None else 1
    resolved_body = body if prefix is not None else raw_text
    resolved_date = safe_text(date_header or decode_header_value(message.get("Date")), limit=200)
    return SourceText(
        raw_text=raw_text,
        body_text=resolved_body,
        body_start_line=body_start_line,
        subject=safe_text(subject or decode_header_value(message.get("Subject")), limit=500),
        from_addr=safe_text(from_addr or decode_header_value(message.get("From")), limit=500),
        to_addrs=[safe_text(item, limit=500) for item in (to_addrs or _address_list(message.get("To")))],
        date_header=resolved_date,
        source_time_zone=source_time_zone,
        # 锚优先用调用方给的；没给就自己从 `Date` 头算 —— 否则"相对日期/落款同日判断"
        # 会掉到"当前时间"兜底，同一份原文换个时间跑就得到不同结果（纯函数被破坏）。
        anchor_utc=anchor_utc or anchor_from_date_header(resolved_date),
        anchor_original=resolved_date,
    )


def _address_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [item.strip() for item in str(value).replace(";", ",").split(",")]
    return [item for item in parts if item]


def _body_prefix(raw_text: str, body: str) -> Optional[str]:
    """若 `body` 确实出现在原文中，返回它之前的前缀（用于计算正文起始行号）。"""
    if not body:
        return None
    position = raw_text.find(body)
    if position < 0:
        return None
    return raw_text[:position]


def _plain_body(message: "email.message.Message") -> str:
    """取可读正文：优先 `text/plain`，退化为 HTML 去标签。"""
    plain_parts: List[str] = []
    html_parts: List[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", "replace")
        if part.get_content_type() == "text/plain":
            plain_parts.append(text)
        elif part.get_content_type() == "text/html":
            html_parts.append(_html_to_text(text))
    if plain_parts:
        return _normalize_newlines("\n".join(part.strip("\n") for part in plain_parts))
    if html_parts:
        return _normalize_newlines("\n".join(part.strip("\n") for part in html_parts))
    return ""


def _html_to_text(html: str) -> str:
    without_script = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    with_breaks = re.sub(r"(?i)<br\s*/?>", "\n", without_script)
    with_breaks = re.sub(r"(?i)</p\s*>", "\n", with_breaks)
    text = re.sub(r"(?s)<[^>]+>", " ", with_breaks)
    return unescape(text)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def email_message_to_source_text(
    message: "email.message.Message",
    *,
    raw_bytes: Optional[bytes] = None,
    anchor_utc: str = "",
    subject: str = "",
    from_addr: str = "",
    to_addrs: Optional[Sequence[str]] = None,
    date_header: str = "",
) -> SourceText:
    """从已解析的 `email.message.Message` 构造 `SourceText`（供写入层复用）。"""
    if raw_bytes is None:
        raw_bytes = message.as_bytes()
    raw_text = raw_bytes.decode("utf-8", "replace")
    tz, tz_name = timezone_from_date_header(date_header or message.get("Date"))
    del tz  # 时区对象由调用方按需重新解析；这里只暴露名字与锚时间
    return parse_source_text(
        raw_text,
        subject=subject or decode_header_value(message.get("Subject")),
        from_addr=from_addr or decode_header_value(message.get("From")),
        to_addrs=to_addrs,
        date_header=date_header or decode_header_value(message.get("Date")),
        source_time_zone=tz_name,
        anchor_utc=anchor_utc,
    )


def decode_eml_bytes(raw: bytes, *, anchor_utc: str = "") -> SourceText:
    """解析 `.eml` 原始字节为 `SourceText`（夹具与真实邮件共用这一条路径）。"""
    text = raw.decode("utf-8", "replace")
    message = email.message_from_bytes(raw)
    tz, tz_name = timezone_from_date_header(message.get("Date"))
    del tz
    return parse_source_text(
        text,
        source_time_zone=tz_name,
        anchor_utc=anchor_utc or _fallback_anchor(message.get("Date")),
    )


def anchor_from_date_header(date_header: Optional[str]) -> str:
    """把 `Date:` 头解析成 UTC 锚时刻（解析不出来返回空串，由调用方决定兜底）。"""
    text = safe_text(date_header, limit=200).strip()
    if not text:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except Exception:
        return ""
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return to_iso(parsed)


def _fallback_anchor(date_header: Optional[str]) -> str:
    """无锚时用邮件 `Date` 头；仍不可解析则用当前 UTC（调用方应优先显式给锚）。"""
    parsed = anchor_from_date_header(date_header)
    if parsed:
        return parsed
    from ..contracts.timeutil import utcnow_iso

    return utcnow_iso()


# --------------------------------------------------------------------------------------
# 标签识别
# --------------------------------------------------------------------------------------


def label_for(body: str, span: Tuple[int, int]) -> Optional[Tuple[LabelRule, str, int]]:
    """给一个时间跨度找语义标签。

    返回 `(rule, 命中的原文标签, 标签起始位置)`；找不到返回 `None`。

    三步（顺序即优先级）：

    1. **同句**内、跨度**之前 20 字符**里的时间标签（`报名截止时间：10月12日`）；
    2. 跨度前 12 字出现「前」+ 行为动词（`请在10月12日前回复`）→ 按行为定语义；
    3. 跨度**紧邻**的行为动词（`报名截止到10月12日`、`提交10月12日`）→ 按动词定语义。

    关键限制：**不跨句**。`A 句的标签 + B 句的时间` 不是证据联系（例如
    「报名截止时间：待定。场地将于10月24日开放参观」里的 10月24日 属于场地开放日，
    不属于报名截止）。跨句的裸日期成为"未解释的时间" → 追问（SPEC §21.1）。
    """
    start = span[0]
    window_start = max(0, start - 20)
    for boundary in _CLAUSE_BOUNDARY:
        position = body.rfind(boundary, 0, start)
        if position + 1 > window_start:
            window_start = position + 1
    window = body[window_start:start]
    best: Optional[Tuple[LabelRule, str, int]] = None
    best_end = -1
    for rule in LABEL_RULES:
        for match in rule.regex().finditer(window):
            if match.end() > best_end:
                best = (rule, match.group(0), window_start + match.start())
                best_end = match.end()
    if best is not None and _window_ok(window, best_end, span):
        return best

    lead_start = max(0, start - 12)
    for boundary in _CLAUSE_BOUNDARY:
        position = body.rfind(boundary, 0, start)
        if position + 1 > lead_start:
            lead_start = position + 1
    lead = body[lead_start:start]
    reversed_match = _REVERSED_ACTION_RE.search(lead)
    if reversed_match is not None:
        label = "前%s" % reversed_match.group(1)
        return (
            LabelRule(r"前\s*(?:提交|回复|答复|报名|确认|缴纳|缴费)", "submission_deadline", label),
            label,
            lead_start + reversed_match.start(),
        )

    for rule in DECLARATIVE_TIME_RULES:
        for match in rule.regex().finditer(lead):
            if len(lead) - match.end() > 4:
                continue
            return (rule, match.group(0), lead_start + match.start())
    return None


def _window_ok(window: str, label_end: int, span: Tuple[int, int]) -> bool:
    """标签与时间之间只允许出现这些字符：`：`、`是`、`为`、空白、标点。

    否则说明标签属于上一句话，不能拿来解释这个时间（例如"报名截止时间待定。会议10月12日"）。
    """
    between = window[label_end:]
    if not between:
        return True
    stripped = between.strip()
    if not stripped:
        return True
    return bool(re.fullmatch(r"[是为：:，,、\s（）()至到\-~—–前 　]*", between))


def extract_time_expressions(
    body: str,
    *,
    anchor: datetime,
    tz: Any,
    time_zone_name: str = DISPLAY_TZ_NAME,
) -> List[TimeExpression]:
    """按最长优先提取时间表达并去重（重叠时保留更长的那个）。"""
    spans: List[Tuple[int, int, str]] = []
    for match in TIME_EXPRESSION_RE.finditer(body):
        spans.append((match.start(), match.end(), match.group(0)))
    # 最长优先：先排序再剔除被包含的短跨度
    spans.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
    kept: List[Tuple[int, int, str]] = []
    for span in spans:
        if any(span[0] >= other[0] and span[1] <= other[1] for other in kept):
            continue
        kept.append(span)
    kept.sort(key=lambda item: item[0])

    out: List[TimeExpression] = []
    for start, end, text in kept:
        expression = parse_expression(text, anchor=anchor, tz=tz, time_zone_name=time_zone_name)
        expression.start = start
        expression.end = end
        out.append(expression)
    return _refine_month_day_years(out, anchor=anchor, tz=tz)


def _refine_month_day_years(
    expressions: Sequence[TimeExpression], *, anchor: datetime, tz: Any
) -> List[TimeExpression]:
    """把"只有月日"的表达按锚年补全年份（跨年时 +1）。

    `parse_expression` 不知道锚时刻，这里的补年规则是：
    解析出的日期早于锚 **30 天以上** → 认为说的是明年（12 月底收到"1月3日截止"）。
    """
    out: List[TimeExpression] = []
    for item in expressions:
        if item.normalized_utc is None:
            out.append(item)
            continue
        parts = _parse_date_parts(item.text)
        if parts is None or parts[0] != 0:
            out.append(item)
            continue
        _, month, day = parts
        try:
            candidate = datetime(anchor.year, month, day)
        except ValueError:
            out.append(item)
            continue
        if candidate < anchor.replace(tzinfo=None) - timedelta(days=_CROSS_YEAR_THRESHOLD_DAYS):
            candidate = candidate.replace(year=anchor.year + 1)
        head_time = _parse_time_of_day(item.text)
        moment = _combine(candidate, head_time).replace(tzinfo=tz)
        item.normalized_utc = to_iso(moment)
        if item.range_end_utc:
            tail = _range_tail(item.text)
            tail_parts = _parse_date_parts(tail) if tail else None
            tail_month = tail_parts[1] if tail_parts is not None else month
            tail_day = tail_parts[2] if tail_parts is not None else day
            tail_year = (tail_parts[0] if tail_parts is not None and tail_parts[0] else candidate.year)
            try:
                item.range_end_utc = to_iso(
                    _combine(datetime(tail_year, tail_month, tail_day), _parse_time_of_day(tail)).replace(tzinfo=tz)
                )
            except ValueError:  # pragma: no cover - 解析失败不抛
                item.range_end_utc = None
        out.append(item)
    return out


def _same_day_as_anchor(candidate_utc: str, anchor_utc: str, tz: Any) -> bool:
    """候选时间与锚（邮件发出时间）是否**在来源时区里**是同一天 —— 用于识别落款日期。

    按 UTC 比会错：`2026-10-08 16:45+08:00` 的 UTC 日期是 10-08，但
    `2026-10-17 14:00+08:00` 的 UTC 日期是 10-17 —— 必须换到同一时区再比日期。
    """
    try:
        left = datetime.fromisoformat(_normalize_iso(candidate_utc)).astimezone(tz)
        right = datetime.fromisoformat(_normalize_iso(anchor_utc)).astimezone(tz)
    except Exception:  # pragma: no cover - 由调用方保证格式
        return False
    return left.date() == right.date()


def reply_requested(body: str) -> bool:
    """原文是否**明确要求**回复/确认（决定"是否生成草稿"的证据，而不是猜）。

    先看否定式：`无需回复` / `不必回复` 里的 `需回复` 是 `REQUIREMENT_MARKERS` 的子串，
    只看标记会把"别回复"读成"要回复"（实测踩过）。
    """
    if any(marker in body for marker in NO_REPLY_MARKERS):
        return False
    return any(marker in body for marker in REQUIREMENT_MARKERS) or any(
        marker in body for marker in ("请回复", "回复确认", "确认参加")
    )


def find_pending_time_tags(body: str) -> List[PendingTimeTag]:
    """找「时间待定」：标签 + 待定词，中间只允许 `：`/空白/`为`/`是` 这类连接词。

    这些不是"没有时间字段"，而是"字段存在、值缺失" —— 必须追问（SPEC §21.1）。
    """
    out: List[PendingTimeTag] = []
    for keyword_match in PENDING_TIME_RE.finditer(body):
        window_start = max(0, keyword_match.start() - 24)
        window = body[window_start : keyword_match.start()]
        best: Optional[Tuple[LabelRule, str, int]] = None
        best_end = -1
        for rule in LABEL_RULES:
            for match in rule.regex().finditer(window):
                if match.end() > best_end:
                    best = (rule, match.group(0), window_start + match.start())
                    best_end = match.end()
        if best is None:
            continue
        if not _window_ok(window, best_end, (keyword_match.start(), keyword_match.end())):
            continue
        rule, label, _start = best
        out.append(
            PendingTimeTag(
                label=label,
                semantic=rule.semantic,
                display=rule.display or label,
                text=keyword_match.group(0),
                start=keyword_match.start(),
                end=keyword_match.end(),
                keyword=keyword_match.group(0),
            )
        )
    return out


# --------------------------------------------------------------------------------------
# 主抽取
# --------------------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """抽取器的原始产物（未做引用绑定前的形态，供测试与调试观察）。"""

    source: SourceText
    time_fields: List[TimeField] = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)
    materials: List[MaterialRequirement] = field(default_factory=list)
    questions: List[QuestionCandidate] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: 没有被任何标签解释的裸日期（必须追问，不得当事实）
    unaccounted: List[TimeExpression] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "time_fields": [item.to_dict() for item in self.time_fields],
            "todos": [item.to_dict() for item in self.todos],
            "materials": [item.to_dict() for item in self.materials],
            "questions": [item.to_dict() for item in self.questions],
            "warnings": list(self.warnings),
            "unaccounted": [item.text for item in self.unaccounted],
        }


def _citation_for(
    *,
    source_object_id: str,
    path: str,
    content_hash: str,
    source_version: Optional[int],
    source_time: Optional[str],
    source: SourceText,
    needle: str,
    supports: str,
) -> CitationRef:
    """在**原始全文**里定位 `needle`，生成行号区间引用（找不到就退化为整篇区间）。"""
    raw = source.raw_text
    position = raw.find(needle)
    if position < 0:
        collapsed = " ".join(raw.split())
        collapsed_needle = " ".join(str(needle).split())
        position = collapsed.find(collapsed_needle)
        if position >= 0:
            # 折叠后命中：用一个不依赖折叠映射的粗定位（宁可粗，不能假）
            start_line = 1
            end_line = max(1, raw.count("\n") + 1)
            return CitationRef.require(
                Citation(
                    source_object_id=source_object_id,
                    source_object_type="SourceDocument",
                    path=path,
                    locator="line:%d-%d" % (start_line, end_line),
                    source_time=source_time,
                    source_version=source_version,
                    content_hash=content_hash,
                    supports=supports,
                    excerpt=_excerpt(collapsed_needle),
                )
            )
        start_line = 1
        end_line = max(1, raw.count("\n") + 1)
        excerpt = ""
    else:
        start_line = raw.count("\n", 0, position) + 1
        end_line = raw.count("\n", 0, position + max(1, len(needle))) + 1
        excerpt = _excerpt(raw[position : position + max(1, len(needle))])
    return CitationRef.require(
        Citation(
            source_object_id=source_object_id,
            source_object_type="SourceDocument",
            path=path,
            locator="line:%d-%d" % (start_line, end_line),
            source_time=source_time,
            source_version=source_version,
            content_hash=content_hash,
            supports=supports,
            excerpt=excerpt,
        )
    )


def _excerpt(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _sentence_bounds(body: str, index: int) -> Tuple[int, int]:
    start = 0
    for boundary in _CLAUSE_BOUNDARY:
        position = body.rfind(boundary, 0, index)
        if position + 1 > start:
            start = position + 1
    end = len(body)
    for boundary in _CLAUSE_BOUNDARY:
        position = body.find(boundary, index)
        if position >= 0 and position < end:
            end = position
    return start, end


def _clean_clause(text: str, *, limit: int = 60) -> str:
    cleaned = " ".join(str(text).split()).strip(" \t　,，、:：")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned


def _normalize_material_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip(" \t　，,。.、：:；;")
    for prefix in ("缺少", "缺", "未提交", "尚未提交", "未提供", "需提交", "需要提交", "请提交", "请提供", "补交", "需补"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.strip(" \t　，,。.、：:；;")
    # 剥离纯计数前缀（"两份身份证复印件" → "身份证复印件"）
    return text


def _material_count_hint(name: str) -> str:
    match = re.match(r"^([一二三四五六七八九十两\d]+)\s*(?:份|张|个|套|件)", name.strip())
    return match.group(1) if match else ""


def extract(
    source: SourceText,
    *,
    source_object_id: str,
    path: str,
    content_hash: str,
    source_version: Optional[int] = None,
    source_time: Optional[str] = None,
) -> ExtractionResult:
    """对一封邮件/一份资料做确定性抽取。**纯函数**：同输入同输出。"""
    body = source.body_text
    anchor_utc = source.anchor_utc or _fallback_anchor(source.date_header)
    tz, tz_name = _tz_for_source(source)
    anchor = _anchor_datetime(anchor_utc, tz, source.anchor_original)

    result = ExtractionResult(source=source)
    expressions = extract_time_expressions(body, anchor=anchor, tz=tz, time_zone_name=tz_name)

    consumed: List[bool] = [False] * len(expressions)
    fields: List[TimeField] = []

    def citation_for(needle: str, supports: str) -> CitationRef:
        return _citation_for(
            source_object_id=source_object_id,
            path=path,
            content_hash=content_hash,
            source_version=source_version,
            source_time=source_time,
            source=source,
            needle=needle,
            supports=supports,
        )

    # ---- 1. 带标签的时间字段
    for index, expression in enumerate(expressions):
        found = label_for(body, (expression.start, expression.end))
        if found is None:
            continue
        rule, label, _label_start = found
        consumed[index] = True
        semantic = rule.semantic
        label_text = rule.display or label
        citation = citation_for(
            expression.text,
            "支持「%s」这一时间字段（原文标签：%s）" % (label_text, label),
        )
        time_field = expression.time_field(
            semantic=semantic,
            label=label,
            display=label_text,
            citations=[citation],
        )
        # 语义与置信度以"表达式本身解析出来的结果"为准（含"日期确定、时刻未定" 0.8 的情况）
        time_field.temporal.source_time_zone = tz_name
        time_field.temporal.confidence = expression.confidence
        time_field.temporal.normalized_utc = expression.normalized_utc
        fields.append(time_field)
        if expression.range_end_utc and rule.semantic == "event_start":
            end_field = expression.time_field(
                semantic="event_end",
                label="活动结束时间",
                display="活动结束时间",
                citations=[citation],
            )
            end_field.temporal.original_text = safe_text(expression.text, limit=64)
            end_field.temporal.normalized_utc = expression.range_end_utc
            end_field.temporal.source_time_zone = tz_name
            end_field.temporal.confidence = 0.8
            fields.append(end_field)

    # ---- 2. 未被标签解释的日期 → 必须追问（不得当事实）
    for index, expression in enumerate(expressions):
        if consumed[index]:
            continue
        if expression.normalized_utc is None:
            continue
        if _same_day_as_anchor(expression.normalized_utc, anchor_utc, tz):
            # 落款日期（"2026年10月5日"）与邮件 Date 头是同一天：这是署名信息，
            # 不是"语义不明的时间字段"。同一输入同一输出、不猜、也不噪声化追问。
            continue
        result.unaccounted.append(expression)

    # ---- 2b. 「时间待定」：语义已知、值缺失 → 记字段 + 追问
    for tag in find_pending_time_tags(body):
        citation = citation_for(
            tag.keyword,
            "支持「%s」这一时间字段目前没有值（原文：%s）" % (tag.display, tag.keyword),
        )
        temporal = TemporalField(
            original_label=safe_text(tag.label, limit=64),
            semantic=tag.semantic,
            original_text=safe_text(tag.text, limit=64),
            source_time_zone=tz_name,
            normalized_utc=None,
            confidence=0.0,
            evidence=[citation.citation.source_object_id],
        )
        field = TimeField(temporal=temporal, citations=[citation], label_text=tag.display)
        fields.append(field)
        result.questions.append(
            QuestionCandidate(
                question="「%s」目前是「%s」——具体时间是什么时候？" % (tag.display, tag.keyword),
                kind="missing_time",
                background="原文标签：%s；原文措辞：%s" % (tag.label, tag.keyword),
                affects=["时间字段:%s" % tag.semantic],
                citations=[citation],
            )
        )

    result.time_fields = merge_time_fields(fields)

    # ---- 3. 语义未定 / 值与标签对不上 → 追问
    for field in result.time_fields:
        if field.semantic == "unknown":
            result.questions.append(
                QuestionCandidate(
                    question="「%s」这一项具体属于哪一类时间（报名截止？活动开始？还是别的）？"
                    % (field.label_text or field.original_label),
                    kind="ambiguity",
                    background="原文标签：%s；原文时间文本：%r" % (field.original_label, field.original_text),
                    affects=["时间字段:unknown"],
                    citations=list(field.citations),
                )
            )
    for expression in result.unaccounted:
        citation = citation_for(
            expression.text,
            "支持「这个日期属于哪一类时间」的追问：原文只有裸日期，没有语义标签",
        )
        result.questions.append(
            QuestionCandidate(
                question="原文出现时间「%s」，但没说它属于哪一类（报名截止？活动开始？还是别的）。请指明它的语义。" % expression.text,
                kind="ambiguity",
                background="没有语义标签的时间不得当事实使用（SPEC §7.4、§21.1）",
                affects=["时间字段:unknown"],
                citations=[citation],
            )
        )

    # ---- 4. 待办（谁要做什么）
    result.todos = _extract_todos(
        body=body,
        source=source,
        source_object_id=source_object_id,
        path=path,
        content_hash=content_hash,
        source_version=source_version,
        source_time=source_time,
        fields=result.time_fields,
        expressions=expressions,
    )

    # ---- 5. 待补材料
    result.materials = _extract_materials(
        body=body,
        source=source,
        source_object_id=source_object_id,
        path=path,
        content_hash=content_hash,
        source_version=source_version,
        source_time=source_time,
    )
    for material in result.materials:
        citation = material.citations[0]
        result.questions.append(
            QuestionCandidate(
                question="材料/资料「%s」我在原文里只看到要求，没有看到已提交或已具备的证据。是否已经准备好？" % material.name,
                kind="missing_material",
                background="原文提到该材料，但缺少「已提供」的依据（SPEC §7.4）。",
                affects=["材料:%s" % material.name],
                citations=[citation],
            )
        )
    return result


def _tz_for_source(source: SourceText) -> Tuple[Any, str]:
    if source.source_time_zone and source.source_time_zone != DISPLAY_TZ_NAME:
        # `+08:00` 形式的偏移
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", source.source_time_zone)
        if match is not None:
            sign = 1 if match.group(1) == "+" else -1
            minutes = sign * (int(match.group(2)) * 60 + int(match.group(3)))
            return timezone(timedelta(minutes=minutes)), source.source_time_zone
        try:  # pragma: no cover - 依赖 tzdata 里的具名时区
            from zoneinfo import ZoneInfo

            return ZoneInfo(source.source_time_zone), source.source_time_zone
        except Exception:  # pragma: no cover
            pass
    return _display_tz(), DISPLAY_TZ_NAME


def _extract_todos(
    *,
    body: str,
    source: SourceText,
    source_object_id: str,
    path: str,
    content_hash: str,
    source_version: Optional[int],
    source_time: Optional[str],
    fields: Sequence[TimeField],
    expressions: Sequence[TimeExpression],
) -> List[TodoItem]:
    """抽取待办：要求标记 + 同句里的时间字段。没有引用的一律不产出。"""
    todos: List[TodoItem] = []
    seen = set()
    for marker in REQUIREMENT_MARKERS:
        position = body.find(marker)
        while position >= 0:
            start, end = _sentence_bounds(body, position)
            clause = _clean_clause(body[start:end])
            if clause and clause not in seen:
                seen.add(clause)
                needle = clause.split("…")[0][:24] or marker
                citation = _citation_for(
                    source_object_id=source_object_id,
                    path=path,
                    content_hash=content_hash,
                    source_version=source_version,
                    source_time=source_time,
                    source=source,
                    needle=needle,
                    supports="支持这条待办：原文要求「%s」" % marker,
                )
                due, due_semantic = _nearest_time_field(
                    body=body, start=start, end=end, fields=fields, clause=clause
                )
                todos.append(
                    TodoItem(
                        title=_todo_title(clause, marker),
                        citations=[citation],
                        due=due,
                        assignee="user",
                        detail=clause,
                        due_semantic=due_semantic,
                    )
                )
            position = body.find(marker, position + len(marker))
    return _dedupe_todos(todos)


def _todo_title(clause: str, marker: str) -> str:
    text = clause.strip()
    for prefix in ("请", "务必", "烦请", "敬请", "需", "需要"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if len(text) > 40:
        text = text[:39] + "…"
    return text or marker


def _dedupe_todos(todos: Iterable[TodoItem]) -> List[TodoItem]:
    """去掉"被同一条更完整的待办包含"的碎片（例如同时命中「请于」与「请回复」）。"""
    kept: List[str] = []
    out: List[TodoItem] = []
    ordered = list(todos)
    for item in ordered:
        key = " ".join(item.title.split())
        if any(key and key in other for other in kept):
            continue
        kept = [other for other in kept if not (other and other in key)]
        kept.append(key)
        out.append(item)
    return out


def _nearest_time_field(
    *,
    body: str,
    start: int,
    end: int,
    fields: Sequence[TimeField],
    clause: str,
) -> Tuple[Optional[TimeField], Optional[str]]:
    """给一条待办选定 `due`（截止时间）。

    两级、都不猜：

    1. 待办所在**句子/分句**里原文出现过的时间字段（`10月20日前提交` 这种把时间写全的）；
    2. 句子里**只有语义、没有数值**时（`于报名截止前回复`），用同句中出现的语义词
       （报名截止 / 提交截止 / 活动开始 …）去匹配时间字段的**语义槽位**。
       两个候选命中不同槽位 → 视为不明确，返回 `None`（宁缺勿错）。
    """
    explicit: List[TimeField] = []
    for field in fields:
        if field.normalized_utc is None:
            continue
        if field.original_text and field.original_text in clause:
            explicit.append(field)
    if explicit:
        explicit.sort(key=lambda item: (item.normalized_utc or "", item.original_text))
        chosen = explicit[0]
        return chosen, chosen.semantic

    by_semantic: Dict[str, TimeField] = {}
    for rule in LABEL_RULES:
        if not rule.semantic:
            continue
        if rule.semantic in by_semantic:
            continue
        for match in rule.regex().finditer(clause):
            key = match.group(0)
            if key in clause:
                by_semantic[rule.semantic] = match  # type: ignore[assignment]
    matched: List[TimeField] = []
    for field in fields:
        if field.semantic in by_semantic:
            matched.append(field)
    if len({field.semantic for field in matched}) == 1:
        chosen = matched[0]
        return chosen, chosen.semantic
    return None, None


def _material_names_after_marker(
    sentence: str,
    marker: str,
    noun_pattern: "re.Pattern",
) -> List[str]:
    """在"关键词 + 清单引导语"之后的名词清单里取材料名。

    只在**清单语境**里取词：
    - 冒号清单：`尚未提交的材料包括：A、B。`；
    - 引导语清单：`缺少的材料是 A 和 B。`（"A" 必须真的出现在引导语之后）。

    这样 `说明补交计划` / `请尽快` 这类动宾短语不会被当成材料名 ——
    抽不出清单就**不产出材料项**（宁缺勿假）。
    """
    index = sentence.find(marker)
    if index < 0:
        return []
    # 从关键词**之后**开始取词：否则「缺知情同意书」会把残余的「少」并进名词（实测踩过）
    after_marker = index + len(marker)
    tail = sentence[after_marker:]
    colon_match = re.match(r"\s*[：:]", tail)
    if colon_match is not None:
        start = after_marker + colon_match.end()
    else:
        lead = re.search(r"(包括|如下|为|是|有)\s*", tail)
        if lead is None:
            return []
        start = after_marker + lead.end()
    window = sentence[start : start + 120]
    names: List[str] = []
    for match in noun_pattern.finditer(window):
        candidate = _normalize_material_name(match.group(1))
        if candidate and len(candidate) >= 2 and candidate not in names:
            names.append(candidate)
    return names


def _extract_materials(
    *,
    body: str,
    source: SourceText,
    source_object_id: str,
    path: str,
    content_hash: str,
    source_version: Optional[int],
    source_time: Optional[str],
) -> List[MaterialRequirement]:
    """抽取"待补材料"：只在命中材料关键词时产出，绝不凭上下文猜。

    名词只从**清单位置**取：关键词以 `：` / `:` / 空白 收尾，后接一串「名词 + 、，和」，
    直到句读。这样 `请于提交截止前回复本邮件说明补交计划` 不会被当成"材料=计划"，
    而 `尚未提交的材料包括：A、B。` 会稳定抽出 `A`、`B`。
    """
    #: 材料名词：以材料类后缀结尾的名词短语
    noun_pattern = re.compile(
        r"([^。；;\n，,、：:]{1,16}?(?:材料|资料|文件|证明|复印件|扫描件|照片|表格|同意书|申请表|身份证|成绩单|证书|回执))"
    )
    out: List[MaterialRequirement] = []
    seen = set()
    for marker in MATERIAL_MARKERS:
        position = body.find(marker)
        while position >= 0:
            start, end = _sentence_bounds(body, position)
            sentence = body[start:end]
            names: List[str] = _material_names_after_marker(sentence, marker, noun_pattern)
            for name in names:
                status = "missing" if marker in ("缺少", "缺", "未提交", "尚未提交", "未提供", "没有提供") else "to_submit"
                key = (name, status)
                if key in seen or len(name) < 2:
                    continue
                seen.add(key)
                citation = _citation_for(
                    source_object_id=source_object_id,
                    path=path,
                    content_hash=content_hash,
                    source_version=source_version,
                    source_time=source_time,
                    source=source,
                    needle=(name if name in sentence else marker),
                    supports="支持这条待补材料：原文表述「%s」" % marker,
                )
                out.append(
                    MaterialRequirement(
                        name=name,
                        citations=[citation],
                        status=key[1],
                        semantic_hint=marker,
                        note=_clean_clause(sentence, limit=120),
                    )
                )
            position = body.find(marker, position + len(marker))
    return out
