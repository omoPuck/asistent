"""执行后端适配接口（SPEC §2.4、§15.1、§21.1）。

dsh / pi / 模型服务 / 脚本都只是**可替换的执行后端**：
- 后端不得成为主数据、任务状态、审批记录和审计记录的唯一承载者；
- 更换后端不能导致历史事务、规则和资料无法读取；
- 后端拿不到发送邮件/网盘凭据，副作用必须走受控连接器（§21.3）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .enums import FailureClass
from .ids import digest_of


@dataclass
class ExecutionRequest:
    """一次执行请求。`inputs` 必须是可序列化的纯数据（不携带凭据）。"""

    purpose: str
    case_id: Optional[str] = None
    task_id: Optional[str] = None
    execution_id: Optional[str] = None
    instructions: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    #: 允许的工具白名单；空表示不允许任何工具（只读推理）
    allowed_tools: List[str] = field(default_factory=list)
    #: 是否只读（默认只读；写操作必须显式开启并经权限裁决）
    read_only: bool = True

    def digest(self) -> str:
        return digest_of(
            {
                "purpose": self.purpose,
                "case_id": self.case_id,
                "task_id": self.task_id,
                "instructions": self.instructions,
                "inputs": self.inputs,
                "allowed_tools": sorted(self.allowed_tools),
                "read_only": self.read_only,
            }
        )


@dataclass
class BackendToolCall:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    note: str = ""

    def digest(self) -> str:
        return digest_of({"tool": self.tool, "args": self.args})


@dataclass
class ExecutionResult:
    ok: bool = True
    output: str = ""
    structured: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[BackendToolCall] = field(default_factory=list)
    model: Optional[str] = None
    token_usage: Dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    failure_class: Optional[str] = None
    error_text: Optional[str] = None

    @staticmethod
    def failure(failure_class: FailureClass, message: str) -> "ExecutionResult":
        return ExecutionResult(ok=False, failure_class=str(failure_class), error_text=message)


@runtime_checkable
class ExecutionBackend(Protocol):
    """执行后端协议：任何后端（dsh/pi/模型/脚本/测试替身）都实现它。"""

    name: str

    def execute(self, request: ExecutionRequest) -> ExecutionResult:  # pragma: no cover - 协议
        ...


class EchoBackend:
    """确定性测试后端：不联网、不调用模型，回显输入摘要。

    契约测试与离线运行使用它，保证「后端可替换」（AT-02）。
    """

    name = "echo"

    def __init__(self, responses: Optional[Dict[str, Any]] = None) -> None:
        self.responses = responses or {}

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.purpose in self.responses:
            payload = self.responses[request.purpose]
            return ExecutionResult(output=str(payload), structured={"echo": True, "purpose": request.purpose})
        return ExecutionResult(
            output=f"echo:{request.purpose}",
            structured={
                "echo": True,
                "purpose": request.purpose,
                "read_only": request.read_only,
                "input_keys": sorted(request.inputs.keys()),
                "inputs_digest": request.digest(),
            },
            model="echo-1",
            token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            cost=0.0,
        )


class ScriptedBackend:
    """剧本后端：按调用顺序返回预设结果，用于回归与失败注入。"""

    name = "scripted"

    def __init__(self, script: List[ExecutionResult], default: Optional[ExecutionResult] = None) -> None:
        self._script = list(script)
        self._default = default or ExecutionResult(output="scripted:default")
        self.calls: List[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        if self._script:
            return self._script.pop(0)
        return self._default


class BackendBudgetExceeded(Exception):
    """预算耗尽：任务暂停，但邮箱接收、手机查看与恢复不能被模型失败拖垮（§21.7）。"""


class BudgetedBackend:
    """给任意后端加"每日预算"护栏：未配置预算时禁止无人值守无限调用。"""

    def __init__(self, inner: ExecutionBackend, *, daily_cost_limit: Optional[float] = None, daily_call_limit: Optional[int] = None) -> None:
        self.inner = inner
        self.name = f"budgeted({inner.name})"
        self.daily_cost_limit = daily_cost_limit
        self.daily_call_limit = daily_call_limit
        self.spent = 0.0
        self.calls = 0

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self.daily_call_limit is not None and self.calls >= self.daily_call_limit:
            raise BackendBudgetExceeded(f"每日调用次数已达上限 {self.daily_call_limit}")
        if self.daily_cost_limit is not None and self.spent >= self.daily_cost_limit:
            raise BackendBudgetExceeded(f"每日费用已达上限 {self.daily_cost_limit}")
        result = self.inner.execute(request)
        self.calls += 1
        self.spent += result.cost
        return result


class HttpChatBackend:
    """OpenAI 兼容 HTTP 后端（标准库实现，可选启用）。

    凭据只从参数或受保护的本地配置读取，绝不写入 Git/日志（§14.4）。
    """

    name = "http_chat"

    def __init__(self, *, base_url: str, model: str, api_key: str, timeout: float = 60.0) -> None:
        if not base_url or not model:
            raise ValueError("HttpChatBackend 需要 base_url 与 model")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.timeout = timeout

    def execute(self, request: ExecutionRequest) -> ExecutionResult:  # pragma: no cover - 需要真实网络
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.instructions or "你是个人事务助手的推理后端。"},
                {"role": "user", "content": json.dumps(request.inputs, ensure_ascii=False, sort_keys=True)},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            return ExecutionResult.failure(FailureClass.NETWORK_FAILURE, f"网络失败: {exc}")
        except Exception as exc:  # pragma: no cover
            return ExecutionResult.failure(FailureClass.MODEL_FAILURE, f"模型调用失败: {exc}")

        choice = (body.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content")) or ""
        usage = body.get("usage") or {}
        return ExecutionResult(
            output=text,
            structured={"raw": body},
            model=body.get("model", self.model),
            token_usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            },
        )


class DshBackendStub:
    """dsh 后端占位（可选接入，SPEC §2.4）。

    真实接入前必须证明：切换后端后旧事务/资料/规则仍可读取，新任务仍按相同审批边界执行（AT-02）。
    """

    name = "dsh_stub"

    def __init__(self, *, command: Optional[List[str]] = None) -> None:
        self.command = command or []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult.failure(
            FailureClass.TOOL_FAILURE,
            "dsh 后端尚未接入：请先完成适配与契约测试（AT-02），不要把它当作已交付能力",
        )
