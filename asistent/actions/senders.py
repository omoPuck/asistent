"""可插拔外发适配器（SPEC §5.7、§21.2、§21.3；实现契约 `asistent.contracts.mail.MailSender`）。

职责边界（**很重要**）：

- 通道只负责"把请求交出去"和"如实报告结论"，**不负责**判断能不能发。
  "能不能发"由 `engine.ActionEngine` 通过 `store.claim_action` 的原子领取决定（§21.2）。
- 通道**不**负责去重：核心分配的 `idempotency_key` 是唯一去重依据，通道自己"顺手去重"
  不能当作安全保证（§21.2 第 1002 行：承诺的边界只是"不因自身重复轮询/并发/重启/重试
  而再次主动提交"，不是在不可控邮件服务上宣称端到端 exactly-once）。

结论必须**保守**：只有三种合法结论

| 结论 | 含义 | 依据 |
|---|---|---|
| `SUCCEEDED` | 有外部接受证据 | 通道拿到了接受凭证（例如远端 message id / SMTP 250） |
| `FAILED_SAFE` | 有确切证据未发生副作用 | 前置校验失败、连接未建立、通道明确拒绝未提交 |
| `UNKNOWN` | 其余一切（超时、连接中断、响应不可解析、崩溃） | **不许猜**；宁可 UNKNOWN 也不要谎报"没发出去" |

真实邮箱/真实外部服务的写操作**不在本轮范围**：本模块只提供内存记录型与文件记录型假通道。
文件记录型（`MailCallLog`）的存在理由是可跨进程核对"到底提交了几次"——崩溃注入测试
（子进程 `os._exit`）需要在父进程里数出提交次数。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.enums import FailureClass
from ..contracts.errors import AsistentError, PermissionDenied, ValidationError
from ..contracts.mail import MailSendRequest, MailSendResult, MailSender

__all__ = [
    "MailSender",
    "MailSendRequest",
    "MailSendResult",
    "OutboundError",
    "OutboundRejected",
    "OutboundTimeout",
    "OutboundUnavailable",
    "OutboundProtocolError",
    "MailCallLog",
    "SendScriptStep",
    "ScriptedMailSender",
    "classify_sender_exception",
    "RESULT_CLASSES",
    "UNSAFE_RESULT_CLASSES",
]

#: 唯一合法的三种结论
RESULT_CLASSES: Tuple[str, ...] = ("SUCCEEDED", "FAILED_SAFE", "UNKNOWN")
#: 需要人工裁决、禁止自动重试的结论
UNSAFE_RESULT_CLASSES: Tuple[str, ...] = ("UNKNOWN",)


class OutboundError(AsistentError):
    """外发过程中的通道错误。

    `provably_unsent` 是**通道自己的断言**：只有当通道能证明"请求根本没提交出去"
    时才可以置 True；核心据此把异常映射为 `FAILED_SAFE`，否则一律 `UNKNOWN`。
    核心不做通用错误码臆断（§21.2 第 1000 行）。
    """

    code = "OUTBOUND_ERROR"
    failure_class = FailureClass.TOOL_FAILURE
    provably_unsent = False


class OutboundRejected(OutboundError):
    """通道明确拒绝，且可证明尚未提交（例如收件人被服务端前置拒绝）。"""

    code = "OUTBOUND_REJECTED"
    provably_unsent = True


class OutboundUnavailable(OutboundError):
    """连接未能建立：请求从未离开本进程，可判定未发生副作用。"""

    code = "OUTBOUND_UNAVAILABLE"
    failure_class = FailureClass.NETWORK_FAILURE
    provably_unsent = True


class OutboundTimeout(OutboundError):
    """有界超时：**不确定**是否已被接受，禁止自动重试。"""

    code = "OUTBOUND_TIMEOUT"
    failure_class = FailureClass.NETWORK_FAILURE
    provably_unsent = False


class OutboundProtocolError(OutboundError):
    """响应无法解析（半包、非预期格式）：不得推断为"未发送"。"""

    code = "OUTBOUND_PROTOCOL_ERROR"
    failure_class = FailureClass.TOOL_FAILURE
    provably_unsent = False


def classify_sender_exception(exc: BaseException) -> Tuple[str, str]:
    """把外发异常映射为 `(result_class, failure_class)`。

    规则（保守优先，SPEC §13.1、§21.2）：

    1. `OutboundError.provably_unsent is True` → `FAILED_SAFE`（通道证明未提交）；
    2. 请求本身非法（`ValidationError`）/ 权限被拒（`PermissionDenied`）→ `FAILED_SAFE`
       （请求根本没离开本进程）；
    3. 其余一切异常 → `UNKNOWN` / `EXTERNAL_UNKNOWN`。

    崩溃类异常（`BaseException` 但非 `Exception`，例如 `KeyboardInterrupt`）**不**在这里处理：
    由引擎原样抛出，让 attempt 停留在 `IN_FLIGHT`，交给重启恢复路径转 `UNKNOWN`。
    """
    if isinstance(exc, OutboundError):
        if exc.provably_unsent:
            return "FAILED_SAFE", str(exc.failure_class)
        return "UNKNOWN", str(exc.failure_class)
    if isinstance(exc, (ValidationError, PermissionDenied)):
        return "FAILED_SAFE", str(exc.failure_class)
    return "UNKNOWN", str(FailureClass.EXTERNAL_UNKNOWN)


class MailCallLog:
    """外发调用账本：内存 + 可选追加落盘（带 `fsync`），用于跨进程核对提交次数。

    只记"发生了什么"，不参与任何决策。落盘格式为**一行一个 JSON**，便于用
    纯标准库在父进程里逐行数（`submitted_count` 只数 `event == "submit"` 的行）。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(path) if path else None
        self._entries: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 记录
    def record(self, request: MailSendRequest, *, event: str, note: str = "", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """`event` ∈ {"submit", "refused"}：submit = 请求已交给外部；refused = 未提交。"""
        entry: Dict[str, Any] = {
            "event": event,
            "idempotency_key": request.idempotency_key,
            "action_id": request.action_id,
            "from_account": request.from_account,
            "to": list(request.to),
            "subject": request.subject,
            "note": note,
        }
        if extra:
            entry.update(extra)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._entries.append(entry)
            if self.path:
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        return entry

    # ------------------------------------------------------------------ 观测
    def entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._entries]

    def in_memory_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def path_exists(self) -> bool:
        return bool(self.path) and os.path.exists(str(self.path))

    def submitted_count(self) -> int:
        """本进程内记账的 submit 次数。"""
        with self._lock:
            return sum(1 for item in self._entries if item.get("event") == "submit")

    @staticmethod
    def read_entries(path: str) -> List[Dict[str, Any]]:
        """从落盘账本读回全部条目（进程重启后核对用）。"""
        if not os.path.exists(path):
            return []
        rows: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    @classmethod
    def submitted_count_on_disk(cls, path: str) -> int:
        """落盘账本里的 submit 次数：崩溃重启后仍能数出来的**唯一**硬证据。"""
        return sum(1 for item in cls.read_entries(path) if item.get("event") == "submit")


@dataclass
class SendScriptStep:
    """一次 `send` 调用的剧本：返回什么、抛什么、算不算"已提交"。"""

    result_class: str = "SUCCEEDED"
    exception: Optional[BaseException] = None
    #: "submit"（请求已交给外部）/ "refused"（未提交）
    phase: str = "submit"
    external_ref: Optional[str] = None
    note: str = ""
    #: 调用 `crash_hook()`（模拟"已提交但结果未落库就被杀"）；默认钩子抛 `SimulatedCrash`
    crash: bool = False

    # ---- 常用剧本 ----
    @staticmethod
    def succeed(external_ref: Optional[str] = None) -> "SendScriptStep":
        return SendScriptStep(result_class="SUCCEEDED", external_ref=external_ref, note="远端接受")

    @staticmethod
    def fail_safe(note: str = "通道拒绝，未提交") -> "SendScriptStep":
        return SendScriptStep(result_class="FAILED_SAFE", phase="refused", note=note)

    @staticmethod
    def unknown(note: str = "响应不可解析") -> "SendScriptStep":
        return SendScriptStep(result_class="UNKNOWN", note=note)

    @staticmethod
    def timeout(note: str = "超时，无法确认是否被接受") -> "SendScriptStep":
        return SendScriptStep(exception=OutboundTimeout(note), note=note)

    @staticmethod
    def rejected(note: str = "服务端前置拒绝") -> "SendScriptStep":
        return SendScriptStep(exception=OutboundRejected(note), phase="refused", note=note)

    @staticmethod
    def unavailable(note: str = "连接未建立") -> "SendScriptStep":
        return SendScriptStep(exception=OutboundUnavailable(note), phase="refused", note=note)

    @staticmethod
    def crash_after_submit(note: str = "已提交，结果未落库即被强杀") -> "SendScriptStep":
        return SendScriptStep(phase="submit", crash=True, note=note)


class SimulatedCrash(BaseException):
    """模拟"被强杀"：继承 `BaseException`（与 `KeyboardInterrupt` 同类），引擎不得吞掉它。"""


class ScriptedMailSender:
    """剧本型假通道：记录每一次调用，按剧本给出结论，可注入故障/超时/崩溃。

    刻意**不**做真实网络写操作。它实现 `asistent.contracts.mail.MailSender` 协议，
    因此可以直接替换成真实通道而不改引擎。
    """

    name = "scripted"

    def __init__(
        self,
        *,
        name: str = "scripted",
        log: Optional[MailCallLog] = None,
        script: Optional[Sequence[SendScriptStep]] = None,
        default_step: Optional[SendScriptStep] = None,
        crash_hook: Optional[Any] = None,
        refuse_reused_key: bool = True,
    ) -> None:
        self.name = name
        self.log = log or MailCallLog()
        self._script: List[SendScriptStep] = list(script or [])
        self.default_step = default_step or SendScriptStep.succeed()
        self.crash_hook = crash_hook
        #: 是否在"同一幂等键第二次提交"时拒绝（**通道侧**的兜底，不是核心保证）
        self.refuse_reused_key = refuse_reused_key
        self._submitted_keys: List[str] = []
        self._lock = threading.Lock()
        self._calls = 0

    # ------------------------------------------------------------------ 观测量
    @property
    def call_count(self) -> int:
        """`send` 被调用的次数（含被拒绝的调用）。"""
        with self._lock:
            return self._calls

    def submitted_keys(self) -> List[str]:
        with self._lock:
            return list(self._submitted_keys)

    def log_entries(self) -> List[Dict[str, Any]]:
        """每次调用发生了什么（含被拒绝的调用）——"调用次数断言"的唯一依据。"""
        return self.log.entries()

    # ------------------------------------------------------------------ 通道
    def _next_step(self) -> SendScriptStep:
        with self._lock:
            if self._script:
                return self._script.pop(0)
        return self.default_step

    def send(self, request: MailSendRequest) -> MailSendResult:
        with self._lock:
            self._calls += 1
        request.validate()  # 请求非法 → ValidationError（未提交，引擎归类 FAILED_SAFE）
        step = self._next_step()

        if self.refuse_reused_key and request.idempotency_key in self.submitted_keys():
            self.log.record(request, event="refused", note="同一幂等键已提交过：通道侧兜底拒绝")
            return MailSendResult.failed_safe(
                "幂等键已被提交过：拒绝第二次提交（通道侧兜底，核心幂等才是保证）",
                raw={"idempotency_key": request.idempotency_key},
            )

        self.log.record(request, event=step.phase, note=step.note)
        if step.phase == "submit":
            with self._lock:
                if request.idempotency_key not in self._submitted_keys:
                    self._submitted_keys.append(request.idempotency_key)

        if step.crash:
            hook = self.crash_hook
            if hook is not None:
                hook()
            raise SimulatedCrash(step.note or "模拟崩溃：已提交，结果未落库")

        if step.exception is not None:
            raise step.exception

        result_class = step.result_class if step.result_class in RESULT_CLASSES else "UNKNOWN"
        if result_class == "SUCCEEDED":
            ref = step.external_ref or "fake-message-id@local"
            return MailSendResult.succeeded(ref, raw={"channel": self.name, "note": step.note})
        if result_class == "FAILED_SAFE":
            return MailSendResult.failed_safe(step.note or "确定未发送", raw={"channel": self.name})
        return MailSendResult(
            result_class="UNKNOWN",
            accepted=False,
            error=step.note or "结果未知",
            raw={"channel": self.name},
        )


def protocol_names(sender: Any) -> Tuple[str, bool]:  # pragma: no cover - 便捷探针
    """探测适配器是否符合 `MailSender` 协议（能力探测，不抛错）。"""
    return (getattr(sender, "name", ""), isinstance(sender, MailSender))
