"""MaiBot 飞书适配器：基于飞书长连接的官方平台适配器。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from collections import OrderedDict
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple, Type

from maibot_sdk import ConfigWidget, ConfigWidgetState, MaiBotPlugin, MessageGateway, PluginConfigBase

from .api import FeishuAPI
from .codec import (
    GATEWAY_NAME,
    PLATFORM,
    collect_post_image_keys,
    decode_event,
    encode_outbound,
    parse_content,
    split_text,
)
from .settings import AdapterSettings
from .ws_handler import build_event_handler

MAX_SEEN_MESSAGES = 2000


def _to_plain(value: Any) -> Any:
    """把飞书 SDK 的模型对象转换成普通字典，屏蔽 SDK 版本差异。"""
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            return json.loads(to_json())
        except Exception:  # noqa: BLE001 - SDK 序列化失败时退回属性遍历
            pass
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_plain(model_dump())
        except Exception:  # noqa: BLE001
            pass
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {str(key): _to_plain(item) for key, item in data.items() if not str(key).startswith("_")}
    return value


class FeishuAdapterPlugin(MaiBotPlugin):
    """飞书官方平台适配器。"""

    config_model: ClassVar[Optional[Type[PluginConfigBase]]] = AdapterSettings

    config_widgets: ClassVar[List[ConfigWidget]] = [
        ConfigWidget(id="connection", kind="status", label="飞书连接状态"),
        ConfigWidget(id="reconnect", kind="action", label="重新连接"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._api: Optional[FeishuAPI] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws_client: Any = None
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self._ready = False
        self._account_id = ""
        self._bot_open_id = ""
        self._bot_name = ""
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._group_names: Dict[str, str] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._api = self._build_api()
        if self.config.plugin.enabled:
            await self._start()

    async def on_unload(self) -> None:
        async with self._lifecycle_lock:
            await self._stop()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del version
        if scope != "self":
            return
        async with self._lifecycle_lock:
            await self._stop()
            self.set_plugin_config(config_data)
            self._api = self._build_api()
            if self.config.plugin.enabled:
                await self._start()

    async def get_config_widget_state(self) -> Dict[str, ConfigWidgetState]:
        configured = bool(self.config.feishu.app_id.strip() and self.config.feishu.app_secret.strip())
        if self._ready:
            text = f"已连接：{self._bot_name or '飞书机器人'}" if self._bot_name else "已连接：飞书机器人"
            tone = "success"
        elif self._thread is not None and self._thread.is_alive():
            text = "连接中 / 等待重连"
            tone = "neutral"
        elif configured:
            text = "尚未连接"
            tone = "neutral"
        else:
            text = "请先填写飞书 App ID 与 App Secret"
            tone = "neutral"
        return {
            "connection": ConfigWidgetState(text=text, tone=tone),
            "reconnect": ConfigWidgetState(disabled=not configured),
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id != "reconnect":
            raise ValueError(f"未知配置页操作: {action_id}")
        async with self._lifecycle_lock:
            await self._stop()
            if self.config.plugin.enabled:
                await self._start()

    # ------------------------------------------------------------------ 连接管理

    def _build_api(self) -> FeishuAPI:
        settings = self.config.feishu
        return FeishuAPI(
            app_id=settings.app_id,
            app_secret=settings.app_secret,
            domain=settings.domain,
            proxy_url=settings.proxy_url,
            timeout=float(settings.request_timeout_sec),
        )

    async def _start(self) -> None:
        settings = self.config.feishu
        if not settings.app_id.strip() or not settings.app_secret.strip():
            self.ctx.logger.error("飞书适配器缺少 App ID 或 App Secret，请在插件配置中填写")
            return
        self._stop_event.clear()
        # _stop() 会关闭旧的 HTTP 客户端, 这里必须重建, 否则重连时客户端已关闭。
        if self._api is not None:
            with contextlib.suppress(Exception):
                await self._api.aclose()
        self._api = self._build_api()
        await self._verify_credentials()
        self._thread = threading.Thread(
            target=self._run_ws_client, name="feishu-ws-client", daemon=True
        )
        self._thread.start()

    async def _stop(self) -> None:
        self._stop_event.set()
        self._stop_ws_client()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 5.0)
        await self._set_ready(False)
        if self._api is not None:
            with contextlib.suppress(Exception):
                await self._api.aclose()

    def _stop_ws_client(self) -> None:
        """停止长连接。

        lark-oapi 的 ``Client.start()`` 绑定模块级事件循环且没有 ``stop()``，
        这里关闭自动重连后停止该循环，让 ``start()`` 正常退出。
        """
        client = self._ws_client
        self._ws_client = None
        loop = self._ws_loop
        if loop is None:
            module = sys.modules.get("lark_oapi.ws.client")
            loop = getattr(module, "loop", None)
        if client is not None:
            with contextlib.suppress(Exception):
                client._auto_reconnect = False
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)

    async def _verify_credentials(self) -> None:
        """用 bot/v3/info 验证凭证，同时拿到机器人自身的 open_id。"""
        if self._api is None:
            return
        try:
            info = await self._api.bot_info()
        except Exception as exc:  # noqa: BLE001 - 统一按鉴权失败处理
            self.ctx.logger.error(
                "飞书鉴权失败，请检查 App ID/Secret、机器人能力与应用发布状态: %s", exc
            )
            await self._set_ready(False)
            return
        self._bot_open_id = str(info.get("open_id") or "")
        self._bot_name = str(info.get("app_name") or "")
        self._account_id = self._bot_open_id or self.config.feishu.app_id.strip()
        await self._set_ready(True)
        self.ctx.logger.info(
            "飞书网关已就绪: %s (open_id=%s)", self._bot_name or "bot", self._bot_open_id or "-"
        )

    def _run_ws_client(self) -> None:
        """长连接线程：SDK 内部负责心跳与重连，退出后按间隔重新拉起。"""
        # lark-oapi 的 ws.Client.start() 固定使用模块级事件循环。
        # 若该模块先在别的主线程被导入, 模块级 loop 会绑定到正在运行的 loop,
        # 在本线程 run_until_complete 就会抛 "loop is already running"。
        # 因此在这里先建独立 loop 并回填给 SDK, 保证长连接跑在本线程自己的 loop 上。
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        try:
            # 显式导入子模块, 不依赖 lark_oapi 顶层的 re-export 是否存在。
            from lark_oapi.core.enum import LogLevel
            from lark_oapi.ws import Client as LarkWSClient
            import lark_oapi.ws.client as lark_ws_client
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("缺少 lark-oapi 依赖，飞书适配器无法启动: %s", exc)
            return
        lark_ws_client.loop = loop

        settings = self.config.feishu
        handler = build_event_handler(self._on_message_event)
        log_level = getattr(LogLevel, str(settings.log_level or "info").upper(), LogLevel.INFO)
        client = LarkWSClient(
            settings.app_id.strip(),
            settings.app_secret.strip(),
            event_handler=handler,
            log_level=log_level,
        )
        self._ws_client = client
        while not self._stop_event.is_set():
            try:
                client.start()
                if self._stop_event.is_set():
                    break
                self.ctx.logger.warning("飞书长连接已结束，准备重连")
            except Exception as exc:  # noqa: BLE001 - 网络异常统一进入重连循环
                if self._stop_event.is_set():
                    break
                self.ctx.logger.warning("飞书长连接异常，准备重连: %s", exc)
            self._stop_event.wait(float(settings.reconnect_delay_sec))
        self._ws_client = None
        with contextlib.suppress(Exception):
            loop.close()

    # ------------------------------------------------------------------ 入站处理

    def _on_message_event(self, data: Any) -> None:
        """飞书 SDK 回调线程：把事件转交给事件循环处理。"""
        try:
            payload = _to_plain(data)
        except Exception:  # noqa: BLE001
            return
        event = payload.get("event") if isinstance(payload, Mapping) else None
        if not isinstance(event, Mapping):
            return
        loop = self._loop
        if loop is None or self._stop_event.is_set():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._handle_event(dict(event)), loop)
        except RuntimeError:
            # 事件循环已关闭，插件正在卸载。
            return

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        message = event.get("message") or {}
        if not isinstance(message, Mapping):
            return
        message_id = str(message.get("message_id") or "").strip()
        if message_id and not self._mark_seen(message_id):
            return

        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        sender_open_id = str(sender_id.get("open_id") or "") if isinstance(sender_id, Mapping) else ""
        if self._bot_open_id and sender_open_id == self._bot_open_id:
            return

        chat_type = str(message.get("chat_type") or "").strip()
        if chat_type == "group" and not self.config.feishu.receive_group_message:
            if not self._mentions_bot(message):
                return

        # 飞书的图片有两种投递方式：纯图片消息，以及图文混排的富文本(post)消息。
        # 两种都要把二进制取回来，否则主程序只会收到一段 "[图片]" 占位文本。
        message_type = str(message.get("message_type") or "").strip()
        content = parse_content(message.get("content"))
        image_keys: List[str] = []
        if message_type == "image":
            image_key = str(content.get("image_key") or "").strip()
            if image_key:
                image_keys.append(image_key)
        elif message_type == "post":
            image_keys = collect_post_image_keys(content)

        image_binaries: Dict[str, bytes] = {}
        if image_keys and self._api is not None:
            limit = int(self.config.feishu.max_download_mb) * 1024 * 1024
            for image_key in image_keys:
                binary = await self._download_inbound_image(message_id, image_key)
                if not binary:
                    continue
                if len(binary) > limit:
                    self.ctx.logger.warning(
                        "飞书图片超过 %s MB，已忽略", self.config.feishu.max_download_mb
                    )
                    continue
                image_binaries[image_key] = binary

        group_name = ""
        if chat_type == "group":
            group_name = await self._resolve_group_name(str(message.get("chat_id") or ""))

        try:
            decoded = decode_event(
                event,
                account_id=self._account_id or self.config.feishu.app_id.strip(),
                bot_open_id=self._bot_open_id,
                group_name=group_name,
                image_binaries=image_binaries,
            )
        except ValueError as exc:
            self.ctx.logger.info("跳过无法处理的飞书消息: %s", exc)
            return

        accepted = await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message=decoded,
            external_message_id=decoded["message_id"],
            dedupe_key=decoded["message_id"],
        )
        preview = decoded.get("processed_plain_text", "").replace("\n", " ")[:60]
        self.ctx.logger.info(
            "收到飞书消息: chat=%s user=%s msg=%s at=%s 内容=%s",
            decoded["message_info"].get("group_info", {}).get("group_id")
            or decoded["message_info"]["additional_config"].get("platform_io_target_user_id", ""),
            decoded["message_info"]["user_info"]["user_id"],
            decoded["message_id"],
            decoded.get("is_at", False),
            preview,
        )
        if not accepted:
            self.ctx.logger.info("MaiBot 未接收飞书消息: %s", decoded["message_id"])

    async def _download_inbound_image(self, message_id: str, image_key: str) -> bytes:
        """下载用户消息里的图片。

        飞书的「下载图片」接口 (``GET /im/v1/images/{image_key}``) 只能下载
        **本应用自己上传**的图片, 用户发来的图用它一律返回 234001。
        用户消息里的图必须走「获取消息中的资源文件」接口, 且 file_key 要与
        message_id 匹配。这里先试消息资源接口, 再退回图片下载接口。
        """
        if self._api is None:
            return b""
        errors: List[str] = []
        try:
            return await self._api.download_message_resource(message_id, image_key, "image")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"消息资源接口: {exc}")
        try:
            return await self._api.download_image(image_key)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"图片下载接口: {exc}")
        self.ctx.logger.warning("飞书图片下载失败: key=%s (%s)", image_key, "; ".join(errors))
        return b""

    def _mentions_bot(self, message: Mapping[str, Any]) -> bool:
        mentions = message.get("mentions")
        if not isinstance(mentions, list) or not self._bot_open_id:
            return False
        for mention in mentions:
            if not isinstance(mention, Mapping):
                continue
            identifier = mention.get("id") or {}
            if isinstance(identifier, Mapping) and str(identifier.get("open_id") or "") == self._bot_open_id:
                return True
        return False
    async def _resolve_group_name(self, chat_id: str) -> str:
        if not chat_id or not self.config.feishu.group_name_enabled or self._api is None:
            return ""
        if chat_id in self._group_names:
            return self._group_names[chat_id]
        name, _ = await self._api.chat_info(chat_id)
        # 取不到也要记住: 否则每条群消息都会再打一次注定失败的接口。
        self._group_names[chat_id] = name
        return name

    def _mark_seen(self, message_id: str) -> bool:
        """消息去重：断线重连后飞书可能重投事件。"""
        if message_id in self._seen:
            return False
        self._seen[message_id] = 0.0
        while len(self._seen) > MAX_SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return True

    async def _set_ready(self, ready: bool) -> None:
        accepted = await self.ctx.gateway.update_state(
            gateway_name=GATEWAY_NAME,
            ready=ready,
            platform=PLATFORM,
            account_id=self._account_id,
            metadata={"connection": "feishu_long_connection"},
        )
        self._ready = ready and accepted

    # ------------------------------------------------------------------ 出站发送

    def _resolve_target(self, message: Mapping[str, Any]) -> Tuple[str, str]:
        info = message.get("message_info") or {}
        group = info.get("group_info") if isinstance(info, Mapping) else None
        config = info.get("additional_config") or {} if isinstance(info, Mapping) else {}
        if isinstance(group, Mapping):
            group_id = str(group.get("group_id") or "").strip()
            if group_id:
                return group_id, "chat_id"
        user_id = str(config.get("platform_io_target_user_id") or "").strip()
        if user_id:
            return user_id, "open_id"
        raise ValueError("出站消息缺少目标聊天 ID")

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform=PLATFORM,
        protocol="feishu_open_platform",
        description="飞书文本、@ 提及、图片与引用回复双向消息网关",
        timeout_ms=180000,
    )
    async def send_to_feishu(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del route, metadata, kwargs
        async with self._send_lock:
            if not self._ready or self._api is None:
                return {"success": False, "error": "飞书网关尚未就绪"}
            try:
                receive_id, receive_id_type = self._resolve_target(message)
                text, images, reply_to = encode_outbound(message.get("raw_message") or [])
                sent: List[str] = []
                for index, chunk in enumerate(split_text(text)):
                    message_id = await self._api.send_message(
                        receive_id,
                        receive_id_type,
                        "text",
                        {"text": chunk},
                        reply_to if index == 0 else "",
                    )
                    sent.append(message_id)
                for binary in images:
                    image_key = await self._api.upload_image(binary)
                    message_id = await self._api.send_message(
                        receive_id, receive_id_type, "image", {"image_key": image_key}
                    )
                    sent.append(message_id)
                if not sent:
                    return {"success": False, "error": "出站消息为空"}
                return {
                    "success": True,
                    "external_message_id": sent[0],
                    "metadata": {"feishu_message_ids": sent},
                }
            except Exception as exc:  # noqa: BLE001 - 出站失败以返回值反馈给宿主
                self.ctx.logger.warning("飞书出站失败: %s", exc)
                return {"success": False, "error": str(exc)}


def create_plugin() -> FeishuAdapterPlugin:
    return FeishuAdapterPlugin()
