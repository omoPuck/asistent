"""只读探针：用真实的 `mcp-email-server` 校验 MCP 适配器（不读邮箱、不写任何数据）。

它只做三件事中的前两件，且第三件仅限只读工具：

1. `initialize`（握手，确认 protocolVersion / serverInfo）
2. `tools/list`（确认必需工具名与只读标注）
3. 可选 `--call-tools`：调用 `list_available_accounts`（`readOnlyHint=true`）验证
   真实 `tools/call` 的返回结构；**绝不**调用任何写工具（send/delete/move/mark）。

输出为逐行 JSON，便于原样贴进文档。用法::

    /usr/bin/python3 tests/mail/real_mcp_probe.py \\
        --command ~/asistent-spike/venv/bin/mcp-email-server \\
        --env XDG_CONFIG_HOME=$HOME/asistent-var/config/xdg

凭据：探针本身不读凭据文件；需要凭据的调用不属于本探针的职责。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asistent.mail.mcp_stdio import (  # noqa: E402
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    StdioJsonRpcClient,
)

READ_ONLY_TOOLS_TO_TRY = ("list_available_accounts", "list_mailboxes")


def _make_stdout_safe() -> None:
    """证据输出与代码页无关：cp936/ascii 控制台下退化成 `?`，不抛 UnicodeEncodeError。"""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - 非 TextIOWrapper 时忽略
        pass


def emit(payload: dict) -> None:
    _make_stdout_safe()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def mask(value):
    """探针输出里的凭据脱敏（账号名保留，密码/令牌形态一律掩掉）。"""
    if isinstance(value, dict):
        return {key: mask(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(item) for item in value]
    text = value if isinstance(value, str) else None
    if text is None:
        return value
    for marker in ("password", "token", "secret"):
        if marker in text.lower() and "=" in text:
            return "***REDACTED***"
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读 MCP 适配器探针")
    parser.add_argument("--command", required=True, help="mcp-email-server 可执行文件路径")
    parser.add_argument("--args", default="stdio", help="子命令参数（默认 stdio）")
    parser.add_argument("--env", action="append", default=[], help="NAME=VALUE，可重复")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--call-tools", action="store_true", help="额外调用只读工具（仍需账号配置）")
    opts = parser.parse_args(argv)

    env = {}
    for item in opts.env:
        name, _, value = item.partition("=")
        env[name] = value

    command = [opts.command] + [part for part in opts.args.split() if part]
    client = StdioJsonRpcClient(command, env=env or None, timeout=opts.timeout)
    exit_code = 0
    called: list = []
    try:
        handshake = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
            "clientInfo": {"name": "asistent-mvp2-probe", "version": "0.1.0"},
        })
        emit({"step": "initialize", "result": handshake})
        client.notify("notifications/initialized", {})

        listed = client.request("tools/list", {})
        tools = listed.get("tools") or []
        names = sorted(str(tool.get("name")) for tool in tools)
        emit({"step": "tools/list", "count": len(tools), "names": names})

        classified = {"adapter_read_only": [], "server_read_only_hint": [], "adapter_refuses_in_read_only": []}
        for tool in tools:
            name = str(tool.get("name"))
            hint = (tool.get("annotations") or {}).get("readOnlyHint")
            if hint is True:
                classified["server_read_only_hint"].append(name)
            if name in WRITE_TOOLS:
                classified["adapter_refuses_in_read_only"].append(name)
            elif name in READ_ONLY_TOOLS:
                classified["adapter_read_only"].append(name)
        emit({"step": "classification",
              "adapter_read_only": sorted(classified["adapter_read_only"]),
              "adapter_refuses_in_read_only": sorted(classified["adapter_refuses_in_read_only"]),
              "server_read_only_hint": sorted(classified["server_read_only_hint"]),
              "note": "get_emails_content 的服务端 readOnlyHint=false（mark_as_read=true 会改远端），"
                      "适配器恒发 mark_as_read=false 因此按只读处理"})

        if opts.call_tools:
            for tool in READ_ONLY_TOOLS_TO_TRY:
                if tool not in names:
                    emit({"step": "tools/call", "tool": tool, "skipped": "服务器未暴露该工具"})
                    continue
                called.append(tool)
                try:
                    result = mask(client.call_tool(tool, {}, read_only=True))
                    emit({"step": "tools/call", "tool": tool, "ok": True, "result": result})
                except Exception as exc:  # noqa: BLE001 - 探针要把失败原样记录
                    emit({"step": "tools/call", "tool": tool, "ok": False,
                          "error": type(exc).__name__, "message": str(exc)})

        emit({"step": "stdio", "stderr_tail": client.stderr_tail(10),
              "protocol_noise": client.protocol_noise, "tools_called": called})
    except Exception as exc:  # noqa: BLE001 - 探针要把失败原样记录并给出非零退出码
        emit({"step": "error", "error": type(exc).__name__, "message": str(exc)})
        exit_code = 2
    finally:
        try:
            client.close(timeout=5.0)
        except Exception:  # pragma: no cover
            pass
    for tool in called:
        if tool in WRITE_TOOLS:  # 双保险：探针绝不允许写工具出现在调用记录里
            emit({"step": "guard", "error": "探针调用了写工具：%s" % tool})
            exit_code = 3
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
