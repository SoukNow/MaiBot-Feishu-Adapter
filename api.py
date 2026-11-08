"""飞书开放平台 REST 客户端：令牌、消息发送、图片上传下载与群信息查询。"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any, Dict, Optional, Tuple

import httpx

# httpx 0.28 起把 proxies= 改名成 proxy=, 老版本只认 proxies=。
# 插件 manifest 要求 httpx>=0.28, 但本地开发/离线自检可能装的是老版本,
# 这里按签名探测, 两边都能跑。
_PROXY_KWARG = (
    "proxy"
    if "proxy" in inspect.signature(httpx.AsyncClient.__init__).parameters
    else "proxies"
)


class FeishuAPIError(RuntimeError):
    """飞书接口返回了业务错误。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"飞书接口错误 code={code}: {message}")
        self.code = code
        self.message = message


class FeishuAPI:
    """飞书开放平台接口封装，令牌自动续期。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        domain: str = "https://open.feishu.cn",
        proxy_url: str = "",
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._domain = domain.strip().rstrip("/") or "https://open.feishu.cn"
        self._token = ""
        self._token_expire_at = 0.0
        client_kwargs: Dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "transport": transport,
        }
        # 刻意不设置默认 Content-Type。httpx 会按请求体类型自动生成:
        #   json=  -> application/json
        #   files= -> multipart/form-data; boundary=...
        # 一旦在这里写死 application/json, multipart 上传时 httpx 自己生成的
        # boundary 头会被覆盖掉 —— 请求体是 multipart 而头写着 json, 飞书收不到
        # image 字段, 直接报 "Invalid request param"(code 234001)。
        proxy = proxy_url.strip()
        if proxy:
            client_kwargs[_PROXY_KWARG] = proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    @property
    def domain(self) -> str:
        return self._domain

    async def aclose(self) -> None:
        await self._client.aclose()

    async def tenant_access_token(self, force: bool = False) -> str:
        """获取 tenant_access_token，提前 5 分钟续期。"""
        now = time.monotonic()
        if not force and self._token and now < self._token_expire_at:
            return self._token
        response = await self._client.post(
            f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        payload = self._parse(response, "/open-apis/auth/v3/tenant_access_token/internal")
        if payload.get("code") != 0:
            raise FeishuAPIError(int(payload.get("code", -1)), str(payload.get("msg", "未知错误")))
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise FeishuAPIError(-1, "未返回 tenant_access_token")
        expire = int(payload.get("expire") or 7200)
        self._token = token
        self._token_expire_at = now + max(60, expire - 300)
        return token

    async def _headers(self) -> Dict[str, str]:
        token = await self.tenant_access_token()
        return {"Authorization": f"Bearer {token}"}

    async def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        response = await self._client.post(
            f"{self._domain}{path}", json=body, headers=await self._headers()
        )
        payload = self._parse(response, path)
        if payload.get("code") != 0:
            raise FeishuAPIError(int(payload.get("code", -1)), str(payload.get("msg", "未知错误")))
        return payload.get("data") or {}

    @staticmethod
    def _parse(response: Any, path: str) -> Dict[str, Any]:
        """解析飞书响应。

        飞书的错误分两种形态:
          1. HTTP 200 + body 里 code != 0  (最常见)
          2. HTTP 4xx/5xx, body 可能是 JSON, 也可能是纯文本 (如 "404 page not found")
        统一在这里处理, 并把状态码与响应体带进异常信息 —— 否则只看到
        httpx 的 "Client error '404 Not Found'" 根本无从判断是路径写错还是权限问题。
        """
        text = ""
        try:
            text = response.text or ""
        except Exception:  # noqa: BLE001 - 响应体读不出来时不能影响主流程
            text = ""
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应 (纯文本 404 等)
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        status = getattr(response, "status_code", 200)
        if status >= 400:
            detail = payload.get("msg") or text.strip() or "无响应体"
            code = int(payload.get("code", status)) if payload.get("code") is not None else status
            raise FeishuAPIError(code, f"HTTP {status} {path}: {detail}")
        return payload

    async def bot_info(self) -> Dict[str, Any]:
        """查询机器人自身信息，含 bot 的 open_id 与名称。"""
        response = await self._client.get(
            f"{self._domain}/open-apis/bot/v3/info", headers=await self._headers()
        )
        payload = self._parse(response, "/open-apis/bot/v3/info")
        if payload.get("code") != 0:
            raise FeishuAPIError(int(payload.get("code", -1)), str(payload.get("msg", "未知错误")))
        bot = payload.get("bot") or {}
        return {
            "open_id": str(bot.get("open_id") or ""),
            "app_name": str(bot.get("app_name") or ""),
            "avatar_url": str(bot.get("avatar_url") or ""),
        }

    async def send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: Dict[str, Any],
        reply_to: str = "",
    ) -> str:
        """发送消息，返回平台消息 ID。"""
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        # 注意: 飞书"发送消息"接口是 /open-apis/im/v1/messages (v1), 没有 v2。
        # 写成 /open-apis/im/v2/messages 会得到纯文本 "404 page not found"。
        if reply_to:
            data = await self._post_json(f"/open-apis/im/v1/messages/{reply_to}/reply", body)
        else:
            data = await self._post_json(
                f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}", body
            )
        message_id = str((data.get("message_id") or "")).strip()
        if not message_id:
            raise FeishuAPIError(-1, "发送结果缺少 message_id")
        return message_id

    async def upload_image(self, binary: bytes, filename: str = "image.png") -> str:
        """上传图片，返回 image_key。"""
        token = await self.tenant_access_token()
        response = await self._client.post(
            f"{self._domain}/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (filename, binary)},
        )
        payload = self._parse(response, "/open-apis/im/v1/images")
        if payload.get("code") != 0:
            raise FeishuAPIError(int(payload.get("code", -1)), str(payload.get("msg", "未知错误")))
        image_key = str(((payload.get("data") or {}).get("image_key")) or "")
        if not image_key:
            raise FeishuAPIError(-1, "上传图片未返回 image_key")
        return image_key

    @staticmethod
    def _read_binary(response: Any, path: str) -> bytes:
        """把响应读成二进制；JSON 响应说明接口报了业务错误。"""
        if getattr(response, "status_code", 200) >= 400:
            FeishuAPI._parse(response, path)  # 必定抛异常
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = response.json()
            raise FeishuAPIError(
                int(payload.get("code", -1)), str(payload.get("msg", "下载资源失败"))
            )
        return response.content

    async def download_image(self, image_key: str) -> bytes:
        """下载**本应用自己上传**的图片二进制内容。

        注意: 飞书这个接口只认应用自己上传过的图片(image_type=message),
        用户发来的图片用它会返回 234001 —— 那种情况要用
        :meth:`download_message_resource`。
        """
        token = await self.tenant_access_token()
        path = f"/open-apis/im/v1/images/{image_key}"
        response = await self._client.get(
            f"{self._domain}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        return self._read_binary(response, path)

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str = "image",
    ) -> bytes:
        """下载消息中的资源文件(用户发来的图片必须走这个接口)。

        ``GET /im/v1/images/{image_key}`` 只能下载本应用自己上传的图片,
        用户消息里的图用它一律返回 234001。用户发来的图要用
        ``GET /im/v1/messages/{message_id}/resources/{file_key}?type=image``,
        且 file_key 必须与 message_id 匹配, 机器人也要在该会话内。
        """
        token = await self.tenant_access_token()
        path = f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        response = await self._client.get(
            f"{self._domain}{path}",
            params={"type": resource_type},
            headers={
                "Authorization": f"Bearer {token}",
                # 飞书文档标注该接口的 Content-Type 必填, 固定这个值。
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return self._read_binary(response, path)

    async def chat_info(self, chat_id: str) -> Tuple[str, str]:
        """查询群信息，返回 (群名称, 群 ID)。失败时返回空名称。"""
        try:
            response = await self._client.get(
                f"{self._domain}/open-apis/im/v1/chats/{chat_id}",
                headers=await self._headers(),
            )
            payload = self._parse(response, f"/open-apis/im/v1/chats/{chat_id}")
            if payload.get("code") != 0:
                return "", chat_id
            data = payload.get("data") or {}
            return str(data.get("name") or ""), chat_id
        except (httpx.HTTPError, FeishuAPIError):
            return "", chat_id
