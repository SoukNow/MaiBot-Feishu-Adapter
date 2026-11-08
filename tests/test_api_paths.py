"""回归测试: 断言飞书 REST 接口的**真实请求路径**。

背景: 之前把发送消息写成了 ``/open-apis/im/v2/messages``, 但飞书只有
``/open-apis/im/v1/messages``。因为纯文本 404 响应体是 "404 page not found"
而不是飞书的 JSON 错误, 排查起来极费劲; 而老的出站测试只测纯函数
(目标解析 / 载荷编码), 完全没碰 HTTP, 所以没能发现。

本测试用 httpx.MockTransport 拦截请求, 断言每个接口的真实 URL。

用法:
    python plugins/maibot-feishu-adapter/tests/test_api_paths.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from api import FeishuAPI, FeishuAPIError  # noqa: E402

failures: list[str] = []
requests: list[httpx.Request] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def handler(request: httpx.Request) -> httpx.Response:
    requests.append(request)
    path = request.url.path
    if path.endswith("/tenant_access_token/internal"):
        return httpx.Response(
            200, json={"code": 0, "msg": "ok", "tenant_access_token": "t-abc", "expire": 7200}
        )
    if path.endswith("/bot/v3/info"):
        return httpx.Response(
            200, json={"code": 0, "msg": "ok", "bot": {"open_id": "ou_bot", "app_name": "小M"}}
        )
    if path.endswith("/reply") or path.endswith("/messages"):
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"message_id": "om_sent"}})
    if "/resources/" in path:
        return httpx.Response(
            200, content=b"binary-message-image", headers={"content-type": "image/png"}
        )
    if "/im/v1/images/" in path:
        return httpx.Response(200, content=b"binary-image", headers={"content-type": "image/png"})
    if path.endswith("/im/v1/images"):
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"image_key": "img_v2_x"}})
    if "/im/v1/chats/" in path:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"name": "测试群"}})
    return httpx.Response(404, text="404 page not found")


async def main() -> int:
    transport = httpx.MockTransport(handler)
    api = FeishuAPI("cli_test", "secret_test", transport=transport)

    print("[1] 发送消息: 必须是 /open-apis/im/v1/messages (飞书没有 v2)")
    requests.clear()
    mid = await api.send_message("ou_alice", "open_id", "text", {"text": "hi"})
    url = requests[-1].url
    print(f"      实际: {url.path}?{url.query}")
    check("路径是 im/v1/messages", url.path == "/open-apis/im/v1/messages", url.path)
    check("不是 im/v2/messages", "/v2/" not in url.path, url.path)
    check("带 receive_id_type 查询参数", "receive_id_type=open_id" in str(url.query), str(url.query))
    check("返回 message_id", mid == "om_sent", mid)
    body = json.loads(requests[-1].content)
    check("请求体含 receive_id", body.get("receive_id") == "ou_alice", str(body))
    check("请求体含 msg_type", body.get("msg_type") == "text", str(body))
    check("content 是 JSON 字符串", isinstance(body.get("content"), str), str(type(body.get("content"))))

    print("[2] 引用回复: /open-apis/im/v1/messages/{id}/reply")
    requests.clear()
    await api.send_message("ou_alice", "open_id", "text", {"text": "re"}, reply_to="om_parent")
    url = requests[-1].url
    print(f"      实际: {url.path}")
    check("路径正确", url.path == "/open-apis/im/v1/messages/om_parent/reply", url.path)
    check("不是 v2", "/v2/" not in url.path, url.path)

    print("[3] 上传图片: /open-apis/im/v1/images")
    requests.clear()
    key = await api.upload_image(b"fake", "a.png")
    url = requests[-1].url
    print(f"      实际: {url.path}")
    check("路径正确", url.path == "/open-apis/im/v1/images", url.path)
    check("返回 image_key", key == "img_v2_x", key)
    # 客户端一旦写死默认 Content-Type, httpx 生成的 multipart boundary 头会被
    # 覆盖掉, 飞书会收到「multipart 请求体 + json 头」并报 Invalid request param。
    content_type = requests[-1].headers.get("content-type", "")
    print(f"      Content-Type: {content_type}")
    check("是 multipart/form-data", content_type.startswith("multipart/form-data"), content_type)
    check("带 boundary", "boundary=" in content_type, content_type)
    check("不是 application/json", "application/json" not in content_type, content_type)
    check("请求体含图片二进制", b"fake" in requests[-1].content, str(requests[-1].content[:200]))
    check(
        "请求体含 image_type",
        b"message" in requests[-1].content,
        str(requests[-1].content[:200]),
    )

    print("[4] 下载图片: /open-apis/im/v1/images/{key}")
    requests.clear()
    data = await api.download_image("img_v2_x")
    url = requests[-1].url
    print(f"      实际: {url.path}")
    check("路径正确", url.path == "/open-apis/im/v1/images/img_v2_x", url.path)
    check("拿到二进制", data == b"binary-image", str(data))

    # 用户发来的图必须走「获取消息中的资源文件」:
    # /im/v1/images/{key} 只能下载本应用自己上传的图片, 用户消息里的图用它返回 234001。
    print("[4b] 下载消息中的资源文件: /open-apis/im/v1/messages/{id}/resources/{key}?type=image")
    requests.clear()
    data = await api.download_message_resource("om_msg_1", "img_v3_user", "image")
    url = requests[-1].url
    print(f"      实际: {url.path}?{url.query}")
    check(
        "路径正确",
        url.path == "/open-apis/im/v1/messages/om_msg_1/resources/img_v3_user",
        url.path,
    )
    check("带 type=image", "type=image" in str(url.query), str(url.query))
    check("拿到二进制", data == b"binary-message-image", str(data))
    check(
        "带 Content-Type",
        requests[-1].headers.get("content-type", "").startswith("application/json"),
        requests[-1].headers.get("content-type", ""),
    )

    print("[5] 群信息: /open-apis/im/v1/chats/{id}")
    requests.clear()
    name, cid = await api.chat_info("oc_group_1")
    url = requests[-1].url
    print(f"      实际: {url.path}")
    check("路径正确", url.path == "/open-apis/im/v1/chats/oc_group_1", url.path)
    check("拿到群名", name == "测试群", name)
    check("返回 chat_id", cid == "oc_group_1", cid)

    print("[6] 机器人信息: /open-apis/bot/v3/info")
    requests.clear()
    info = await api.bot_info()
    url = requests[-1].url
    print(f"      实际: {url.path}")
    check("路径正确", url.path == "/open-apis/bot/v3/info", url.path)
    check("拿到 open_id", info["open_id"] == "ou_bot", str(info))

    print("[7] 4xx 响应体必须带进异常信息 (不能再只看到 httpx 的 404)")

    def failing_handler(request: httpx.Request) -> httpx.Response:
        # 只有发送消息接口失败, token 正常返回, 这样异常才指向真正出错的路径
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "msg": "ok", "tenant_access_token": "t-abc", "expire": 7200}
            )
        return httpx.Response(404, text="404 page not found")

    err_transport = httpx.MockTransport(failing_handler)
    bad_api = FeishuAPI("cli_test", "secret_test", transport=err_transport)
    try:
        await bad_api.send_message("ou_x", "open_id", "text", {"text": "x"})
        check("4xx 抛异常", False, "没有抛异常")
    except FeishuAPIError as exc:
        detail = str(exc)
        print(f"      异常: {detail}")
        check("异常含 HTTP 状态码", "404" in detail, detail)
        check("异常含响应体", "404 page not found" in detail, detail)
        check("异常含请求路径", "/open-apis/im/v1/messages" in detail, detail)

    await api.aclose()
    await bad_api.aclose()

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
