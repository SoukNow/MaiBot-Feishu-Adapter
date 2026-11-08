"""回归测试: 真跑 lark-oapi 的事件分发路径。

背景: 之前的离线测试全是 mock, 漏掉了一个致命 bug ——
``EventDispatcherHandler.builder(...).register_xxx(...)`` 返回的是 **builder**,
而 ``lark_oapi/ws/client.py`` 收到事件时直接调用
``event_handler._do_without_validation(...)``; 该方法只在 ``.build()`` 出来的
``EventDispatcherHandler`` 上。少写 ``.build()`` 的后果是:

    消息能到达服务器, 但分发时抛 AttributeError -> 机器人完全没反应

本测试用真实 SDK 构造处理器, 并喂一条真实结构的 v2 事件, 断言回调真的被触发。

用法:
    python plugins/maibot-feishu-adapter/tests/test_sdk_dispatch.py

本机没装 lark-oapi 时会跳过; 在服务器容器里跑可以真正验证:

    sudo docker exec maim-bot-core /MaiMBot/.venv/bin/python \\
        /MaiMBot/bots/<bot_id>/plugins/maibot-feishu-adapter/tests/test_sdk_dispatch.py
"""

from __future__ import annotations

import json
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def sample_event_payload() -> bytes:
    """一条 im.message.receive_v1 的 v2 事件 (私聊文本)。"""
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_001",
            "event_type": "im.message.receive_v1",
            "create_time": "1762000000000",
            "token": "verification-token",
            "app_id": "cli_test",
            "tenant_key": "tenant_test",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_alice", "union_id": "on_alice", "user_id": "u_alice"},
                "sender_type": "user",
                "tenant_key": "tenant_test",
            },
            "message": {
                "message_id": "om_test_001",
                "root_id": "",
                "parent_id": "",
                "create_time": "1762000000000",
                "chat_id": "oc_test_1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}, ensure_ascii=False),
                "mentions": [],
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def main() -> int:
    try:
        from lark_oapi import EventDispatcherHandler
        from lark_oapi.ws.client import Client as LarkWSClient
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP  未安装 lark-oapi ({exc}); 在容器内运行可真正验证")
        return 0

    from ws_handler import build_event_handler, ensure_handler_method, required_handler_method

    # 方法名在不同 SDK 版本间变过 (1.5.x: do_without_validation,
    # 1.7.x: _do_without_validation), 所以测试必须用探测出来的名字, 不能写死。
    required = required_handler_method()
    print(f"  (当前 SDK 要求的方法: {required})")

    print("[1] 处理器类型必须是 EventDispatcherHandler, 不能是 builder")
    received: list[object] = []

    def callback(event):  # noqa: ANN001 - SDK 传入的模型对象
        received.append(event)

    handler = build_event_handler(callback)
    check("handler 类型正确", type(handler).__name__ == "EventDispatcherHandler", type(handler).__name__)
    check("不是 builder", type(handler).__name__ != "EventDispatcherHandlerBuilder", type(handler).__name__)
    check("具备 SDK 要求的方法", hasattr(handler, required), required)

    print("[2] ws.Client 收到的 event_handler 必须能分发事件")
    # 复刻 ws/client.py 的调用方式: 直接对 _event_handler 调它要的那个方法
    ws_client = LarkWSClient("cli_test", "secret_test", event_handler=handler)
    check("Client 保存了 handler", ws_client._event_handler is handler)
    check("Client 的 handler 可分发", hasattr(ws_client._event_handler, required))

    print("[3] 真实事件分发: 回调应被触发")
    try:
        getattr(ws_client._event_handler, required)(sample_event_payload())
        dispatched = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        dispatched = False
        err = f"{type(exc).__name__}: {exc}"
    check("分发未抛异常", dispatched, err)
    check("回调被调用 1 次", len(received) == 1, f"实际 {len(received)} 次")

    print("[4] 回调收到的内容正确")
    if received:
        event = received[0]
        body = getattr(event, "event", None)
        message = getattr(body, "message", None)
        chat_id = getattr(message, "chat_id", None)
        msg_id = getattr(message, "message_id", None)
        check("chat_id 正确", chat_id == "oc_test_1", str(chat_id))
        check("message_id 正确", msg_id == "om_test_001", str(msg_id))
        content = getattr(message, "content", None)
        check("content 是 JSON 字符串", isinstance(content, str), str(type(content)))
        if isinstance(content, str):
            try:
                check("文本内容正确", json.loads(content).get("text") == "你好", content)
            except Exception as exc:  # noqa: BLE001
                check("文本内容正确", False, str(exc))
    else:
        check("有收到事件", False, "回调未触发")

    print("[5] 版本自适应: 方法名探测 + 别名兜底")
    import lark_oapi.ws.client as ws_client_module

    source = inspect.getsource(ws_client_module)
    check("探测结果与 ws client 源码一致",
          ("self._event_handler.%s(" % required) in source,
          "%s 不在源码里" % required)
    check("探测结果属于合法候选",
          required in ("_do_without_validation", "do_without_validation"), required)

    # 别名兜底: 造一个只有另一个名字的对象, 验证能补出目标方法
    other = "do_without_validation" if required == "_do_without_validation" else "_do_without_validation"

    class _OnlyOther:
        def __init__(self):
            self.called = False

        def _do_without_validation(self, payload):  # noqa: ANN001
            self.called = True
            return "ok"

    dummy = _OnlyOther()
    if not hasattr(dummy, required):
        try:
            ensure_handler_method(dummy, required)
            check("别名兜底可用", hasattr(dummy, required), required)
            check("别名兜底可调用", dummy._do_without_validation(b"x") == "ok")
        except Exception as exc:  # noqa: BLE001
            check("别名兜底可用", False, str(exc))
    else:
        check("别名兜底可用", True, "无需别名")

    # 缺少两个名字时必须明确报错, 不能静默返回一个坏 handler
    class _Neither:
        pass

    try:
        ensure_handler_method(_Neither(), required)
        check("两方法都缺时抛错", False, "没有抛异常")
    except RuntimeError:
        check("两方法都缺时抛错", True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} 项 -> {failures}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
