"""飞书适配器的离线自检: 只依赖标准库, 不需要网络与凭证。

用法:
    python plugins/maibot-feishu-adapter/tests/test_codec.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codec import (  # noqa: E402
    collect_post_image_keys,
    decode_event,
    encode_outbound,
    extract_post_text,
    split_text,
)

BOT_OPEN_ID = "ou_bot_open_id"
ACCOUNT_ID = "ou_bot_open_id"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def group_event() -> dict:
    return {
        "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
        "message": {
            "message_id": "om_001",
            "root_id": "",
            "parent_id": "",
            "create_time": "1762000000000",
            "chat_id": "oc_group_1",
            "chat_type": "group",
            "message_type": "text",
            "content": json.dumps({"text": "@_user_1 今天天气怎么样"}),
            "mentions": [
                {"key": "@_user_1", "id": {"open_id": BOT_OPEN_ID}, "name": "麦麦"},
            ],
        },
    }


def private_event() -> dict:
    return {
        "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
        "message": {
            "message_id": "om_002",
            "create_time": "1762000001000",
            "chat_id": "oc_p2p_1",
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": "在吗"}),
            "mentions": [],
        },
    }


def image_event() -> dict:
    return {
        "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
        "message": {
            "message_id": "om_003",
            "create_time": "1762000002000",
            "chat_id": "oc_p2p_1",
            "chat_type": "p2p",
            "message_type": "image",
            "content": json.dumps({"image_key": "img_v2_xxx"}),
        },
    }


print("群聊 @ 消息")
message = decode_event(group_event(), ACCOUNT_ID, BOT_OPEN_ID, "测试群")
check("platform 正确", message["platform"] == "feishu", message["platform"])
check("account_id 正确", message["account_id"] == ACCOUNT_ID)
check("消息 ID 透传", message["message_id"] == "om_001")
check("识别为 @ 机器人", message["is_at"] is True)
check("群 ID 正确", message["message_info"]["group_info"]["group_id"] == "oc_group_1")
check("群名正确", message["message_info"]["group_info"]["group_name"] == "测试群")
check(
    "出站群目标已写入",
    message["message_info"]["additional_config"]["platform_io_target_group_id"] == "oc_group_1",
)
check("用户 ID 正确", message["message_info"]["user_info"]["user_id"] == "ou_alice")
check(
    "@ 段已还原",
    any(seg["type"] == "at" and seg["data"]["target_user_id"] == BOT_OPEN_ID
        for seg in message["raw_message"]),
    json.dumps(message["raw_message"], ensure_ascii=False),
)
check("正文已还原", message["processed_plain_text"] == "@麦麦 今天天气怎么样",
      message["processed_plain_text"])
check("时间戳为秒", abs(float(message["timestamp"]) - 1762000000.0) < 1)

print("私聊文本消息")
message = decode_event(private_event(), ACCOUNT_ID, BOT_OPEN_ID)
check("无群信息", "group_info" not in message["message_info"])
check(
    "出站私聊目标已写入",
    message["message_info"]["additional_config"]["platform_io_target_user_id"] == "ou_alice",
)
check("非 @ 消息", message["is_at"] is False)
check("正文正确", message["processed_plain_text"] == "在吗")

def post_image_event() -> dict:
    """图文混排的富文本消息：飞书在输入框里带图发字时走的就是这种格式。"""
    return {
        "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
        "message": {
            "message_id": "om_004",
            "create_time": "1762000003000",
            "chat_id": "oc_p2p_1",
            "chat_type": "p2p",
            "message_type": "post",
            "content": json.dumps(
                {
                    "content": [
                        [{"tag": "img", "image_key": "img_v3_post_1"}],
                        [{"tag": "text", "text": "这是啥"}],
                    ]
                }
            ),
            "mentions": [],
        },
    }


print("图片消息")
message = decode_event(
    image_event(), ACCOUNT_ID, BOT_OPEN_ID, image_binaries={"img_v2_xxx": b"\x89PNG fake"}
)
check("标记为图片", message["is_picture"] is True)
image_segments = [seg for seg in message["raw_message"] if seg["type"] == "image"]
check("含图片段", len(image_segments) == 1)
check("图片带 base64", image_segments and image_segments[0].get("binary_data_base64"))
check("图片带哈希", image_segments and len(image_segments[0].get("hash", "")) == 64)

print("富文本图片消息")
post_content = json.loads(post_image_event()["message"]["content"])
check(
    "能收集富文本图片 key",
    collect_post_image_keys(post_content) == ["img_v3_post_1"],
    collect_post_image_keys(post_content),
)
message = decode_event(
    post_image_event(),
    ACCOUNT_ID,
    BOT_OPEN_ID,
    image_binaries={"img_v3_post_1": b"\x89PNG post"},
)
check("富文本图片标记为图片", message["is_picture"] is True)
image_segments = [seg for seg in message["raw_message"] if seg["type"] == "image"]
check("富文本含图片段", len(image_segments) == 1)
check(
    "富文本图片带 base64",
    image_segments and image_segments[0].get("binary_data_base64"),
)
check(
    "富文本图片内容正确",
    image_segments
    and __import__("base64").b64decode(image_segments[0]["binary_data_base64"]) == b"\x89PNG post",
)
text_segments = [seg for seg in message["raw_message"] if seg["type"] == "text"]
check(
    "富文本文字保留",
    any(seg["data"] == "这是啥" for seg in text_segments),
    json.dumps(message["raw_message"], ensure_ascii=False),
)
check(
    "图片段排在文字段之前",
    [seg["type"] for seg in message["raw_message"]] == ["image", "text"],
    [seg["type"] for seg in message["raw_message"]],
)
check(
    "富文本正文保留",
    "这是啥" in message["processed_plain_text"],
    message["processed_plain_text"],
)

print("富文本图片下载失败")
message = decode_event(post_image_event(), ACCOUNT_ID, BOT_OPEN_ID, image_binaries={})
check("降级不丢消息", len(message["raw_message"]) >= 1)
check(
    "失败时给出明确占位",
    any(seg["type"] == "text" and "无法读取" in seg["data"] for seg in message["raw_message"]),
    json.dumps(message["raw_message"], ensure_ascii=False),
)
check("失败时不算图片", message["is_picture"] is False)

print("纯图片消息下载失败")
message = decode_event(image_event(), ACCOUNT_ID, BOT_OPEN_ID, image_binaries={})
check("纯图片失败不丢消息", len(message["raw_message"]) == 1)
check("纯图片失败不算图片", message["is_picture"] is False)

print("出站编码")
text, images, reply_to = encode_outbound(
    [
        {"type": "text", "data": "好的"},
        {"type": "at", "data": {"target_user_id": "ou_alice", "target_user_nickname": "Alice"}},
        {"type": "text", "data": "明天见"},
    ]
)
check("文本合并", text == "好的<at user_id=\"ou_alice\">Alice</at> 明天见", text)
check("无图片", images == [])
check("无引用", reply_to == "")

text, images, reply_to = encode_outbound(
    [
        {"type": "reply", "data": {"target_message_id": "om_001"}},
        {"type": "text", "data": "收到"},
    ]
)
check("引用目标解析", reply_to == "om_001")

print("富文本与长文本")
post = extract_post_text(
    {"title": "标题", "content": [[{"tag": "text", "text": "正文"}, {"tag": "a", "text": "链接", "href": "https://x"}]]}
)
check("富文本提取", "标题" in post and "正文" in post, post)
chunks = split_text("字" * 9000)
check("长文本切分", len(chunks) == 3 and all(len(c) <= 4000 for c in chunks), [len(c) for c in chunks])

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    raise SystemExit(1)
print("全部通过")
