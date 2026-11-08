"""构造飞书长连接使用的事件处理器。

单独成模块的原因: 这里必须调用 ``.build()``。

lark-oapi 的 ``EventDispatcherHandler.builder(...).register_xxx(...)`` 返回的是
**builder**, 而 ``lark_oapi/ws/client.py`` 在收到事件时直接调用::

    result = self._event_handler._do_without_validation(pl)   # 1.7.x
    result = self._event_handler.do_without_validation(pl)     # 1.5.x

少写 ``.build()`` 时消息依然能到达服务器, 但分发时抛::

    'EventDispatcherHandlerBuilder' object has no attribute '_do_without_validation'

表现为"在飞书发了消息, 机器人完全没反应"。

**注意方法名在 SDK 版本间变过**: 1.5.x 是 ``do_without_validation`` (无下划线),
1.7.x 改成了 ``_do_without_validation``。所以这里不能写死方法名, 必须探测
SDK 的 ws client 实际调用的是哪个 —— 否则在旧版 SDK 上会误判成"处理器无效"
并把长连接线程直接打死。
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable

# ws client 可能调用的两个方法名 (按版本不同)
_METHOD_CANDIDATES = ("_do_without_validation", "do_without_validation")


def required_handler_method() -> str:
    """探测当前 lark-oapi 的 ws client 实际调用的事件处理方法名。

    直接读 ``lark_oapi/ws/client.py`` 的源码, 从 ``self._event_handler.xxx(``
    里取出方法名 —— 这比写死版本号可靠, 也比 try/except 试错可靠。
    """
    try:
        import lark_oapi.ws.client as ws_client

        source = inspect.getsource(ws_client)
    except Exception:  # noqa: BLE001 - 探测失败时退回新版的约定名字
        return "_do_without_validation"

    match = re.search(r"self\._event_handler\.(\w+)\(", source)
    if match and match.group(1) in _METHOD_CANDIDATES:
        return match.group(1)
    return "_do_without_validation"


def ensure_handler_method(handler: Any, required: str) -> Any:
    """保证 handler 上存在 ws client 要调用的方法。

    正常情况下 handler 和 client 来自同一个 SDK 包, 方法名必然一致; 这个函数
    处理的是混用版本时 handler 缺少目标方法的情况 —— 从另一个名字别名过来。

    Raises:
        RuntimeError: 两个名字都没有, SDK 版本完全不认识。
    """
    if hasattr(handler, required):
        return handler
    for alias in _METHOD_CANDIDATES:
        if hasattr(handler, alias):
            setattr(handler, required, getattr(handler, alias))
            return handler
    raise RuntimeError(
        "lark-oapi 事件处理器无效: 缺少 %s (SDK 版本不兼容)" % required
    )


def build_event_handler(callback: Callable[[Any], None]) -> Any:
    """构造处理 ``im.message.receive_v1`` 的事件处理器。

    Args:
        callback: 收到消息事件时调用的同步回调, 入参是 SDK 的
            ``P2ImMessageReceiveV1`` 模型对象。

    Returns:
        已 build 的 ``EventDispatcherHandler`` 实例。

    Raises:
        RuntimeError: SDK 版本不兼容, 处理器缺少 ws client 需要的方法。
    """
    from lark_oapi import EventDispatcherHandler

    handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    return ensure_handler_method(handler, required_handler_method())
