"""飞书事件与宿主标准消息段之间的转换；不依赖主程序内部模块。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

PLATFORM = "feishu"
GATEWAY_NAME = "feishu_gateway"

# 飞书单条文本消息长度上限远大于此，按 4000 切分可安全容纳中英文混排。
MAX_TEXT_LENGTH = 4000

# 图片以二进制消息段透传给宿主，由主程序统一做识别与描述；
# 文本里只保留占位说明，拿不到二进制时用更明确的失败占位。
IMAGE_PLACEHOLDER = "[图片]"
IMAGE_UNAVAILABLE = "[图片无法读取]"


def parse_content(raw: Any) -> Dict[str, Any]:
    """飞书事件里的 content 是 JSON 字符串；解析失败时按纯文本处理。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}
    return data if isinstance(data, dict) else {"text": text}


def binary_segment(kind: str, binary: bytes, label: str = "") -> Dict[str, Any]:
    """构造宿主标准的二进制媒体段。"""
    if not binary:
        raise ValueError("媒体内容为空")
    return {
        "type": kind,
        "data": label,
        "hash": hashlib.sha256(binary).hexdigest(),
        "binary_data_base64": base64.b64encode(binary).decode("ascii"),
    }


def decode_binary(segment: Mapping[str, Any]) -> bytes:
    value = segment.get("binary_data_base64")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{segment.get('type')} 消息段缺少二进制内容")
    try:
        binary = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("媒体 Base64 无效") from exc
    if not binary:
        raise ValueError("媒体内容为空")
    return binary


def extract_post_items(content: Mapping[str, Any]) -> List[List[Dict[str, str]]]:
    """把富文本消息拆成有序的行，每行是若干 ``text`` / ``at`` / ``img`` 条目。

    ``img`` 条目的 ``value`` 是飞书 image_key，``at`` 条目额外带 ``name``。
    """
    lines: List[List[Dict[str, str]]] = []
    title = str(content.get("title") or "").strip()
    if title:
        lines.append([{"kind": "text", "value": title}])
    body = content.get("content")
    if not isinstance(body, list):
        return lines
    for line in body:
        if not isinstance(line, list):
            continue
        items: List[Dict[str, str]] = []
        for item in line:
            if not isinstance(item, Mapping):
                continue
            tag = str(item.get("tag") or "")
            if tag == "text":
                items.append({"kind": "text", "value": str(item.get("text") or "")})
            elif tag == "a":
                items.append(
                    {"kind": "text", "value": f"{item.get('text') or ''}({item.get('href') or ''})"}
                )
            elif tag == "at":
                items.append(
                    {
                        "kind": "at",
                        "value": str(item.get("user_id") or ""),
                        "name": str(item.get("user_name") or ""),
                    }
                )
            elif tag == "img":
                items.append({"kind": "img", "value": str(item.get("image_key") or "")})
        if items:
            lines.append(items)
    return lines


def collect_post_image_keys(content: Mapping[str, Any]) -> List[str]:
    """收集富文本消息里的全部 image_key，保持出现顺序且去重。"""
    keys: List[str] = []
    for line in extract_post_items(content):
        for item in line:
            if item["kind"] != "img":
                continue
            key = item["value"].strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def extract_post_text(content: Mapping[str, Any]) -> str:
    """富文本消息只提取可读文字，保留标题与正文层级。"""
    parts: List[str] = []
    for line in extract_post_items(content):
        chunk = "".join(item["value"] for item in line if item["kind"] == "text").strip()
        if chunk:
            parts.append(chunk)
    return "\n".join(parts).strip()


def decode_text(
    text: str,
    mentions: List[Mapping[str, Any]],
    bot_open_id: str = "",
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """把飞书的 ``@_user_N`` 占位符还原成标准 at 段。

    返回标准消息段、可读正文与是否 @ 到机器人。
    """
    lookup: Dict[str, Tuple[str, str]] = {}
    for mention in mentions:
        if not isinstance(mention, Mapping):
            continue
        key = str(mention.get("key") or "").strip()
        if not key:
            continue
        identifier = mention.get("id") or {}
        open_id = str(identifier.get("open_id") or "").strip() if isinstance(identifier, Mapping) else ""
        name = str(mention.get("name") or "").strip()
        lookup[key] = (open_id, name)

    segments: List[Dict[str, Any]] = []
    display: List[str] = []
    mentioned = False
    position = 0
    while position < len(text):
        hit: Optional[Tuple[int, str, str, str]] = None
        for key, (open_id, name) in lookup.items():
            index = text.find(key, position)
            if index >= 0 and (hit is None or index < hit[0]):
                hit = (index, key, open_id, name)
        if hit is None:
            break
        index, key, open_id, name = hit
        if index > position:
            chunk = text[position:index]
            segments.append({"type": "text", "data": chunk})
            display.append(chunk)
        label = f"@{name}" if name else "@用户"
        segments.append(
            {
                "type": "at",
                "data": {"target_user_id": open_id, "target_user_nickname": name or open_id},
            }
        )
        display.append(label)
        if bot_open_id and open_id == bot_open_id:
            mentioned = True
        position = index + len(key)
    if position < len(text):
        chunk = text[position:]
        segments.append({"type": "text", "data": chunk})
        display.append(chunk)
    return segments, "".join(display).strip(), mentioned


def decode_post(
    content: Mapping[str, Any],
    image_binaries: Optional[Mapping[str, bytes]] = None,
    bot_open_id: str = "",
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """把飞书富文本消息转换为标准消息段。

    富文本里的图片会以二进制图片段透传给宿主，由主程序统一落盘、识别并生成描述；
    拿不到二进制时降级为占位文字，避免上下文里留下无法解释的 "[图片]" 噪声。
    """
    binaries = image_binaries or {}
    segments: List[Dict[str, Any]] = []
    text_lines: List[str] = []
    mentioned = False

    for line in extract_post_items(content):
        line_parts: List[str] = []
        for item in line:
            kind = item["kind"]
            if kind == "text":
                value = item["value"]
                if not value:
                    continue
                segments.append({"type": "text", "data": value})
                line_parts.append(value)
            elif kind == "at":
                target = item["value"].strip()
                name = item.get("name") or target
                if not target:
                    continue
                segments.append(
                    {"type": "at", "data": {"target_user_id": target, "target_user_nickname": name}}
                )
                line_parts.append(f"@{name}")
                if bot_open_id and target == bot_open_id:
                    mentioned = True
            else:
                key = item["value"].strip()
                binary = binaries.get(key) if key else None
                if binary:
                    segments.append(binary_segment("image", binary))
                    line_parts.append(IMAGE_PLACEHOLDER)
                else:
                    segments.append({"type": "text", "data": IMAGE_UNAVAILABLE})
                    line_parts.append(IMAGE_UNAVAILABLE)
        line_text = "".join(line_parts).strip()
        if line_text:
            text_lines.append(line_text)

    return segments, "\n".join(text_lines).strip(), mentioned


def decode_event(
    event: Mapping[str, Any],
    account_id: str,
    bot_open_id: str = "",
    group_name: str = "",
    image_binaries: Optional[Mapping[str, bytes]] = None,
) -> Dict[str, Any]:
    """把飞书 ``im.message.receive_v1`` 事件转换为宿主标准消息字典。"""
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    if not isinstance(message, Mapping) or not isinstance(sender, Mapping):
        raise ValueError("飞书事件结构不完整")

    message_id = str(message.get("message_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    chat_type = str(message.get("chat_type") or "").strip()
    user_id = str(sender_id.get("open_id") or "").strip() if isinstance(sender_id, Mapping) else ""
    if not message_id or not chat_id or not user_id:
        raise ValueError("飞书事件缺少 message_id / chat_id / 用户 open_id")

    is_group = chat_type == "group"
    message_type = str(message.get("message_type") or "text").strip()
    content = parse_content(message.get("content"))
    mentions = message.get("mentions")
    mention_list = [item for item in mentions if isinstance(item, Mapping)] if isinstance(mentions, list) else []

    segments: List[Dict[str, Any]] = []
    plain_text = ""
    mentioned = False
    binaries = dict(image_binaries or {})
    if message_type == "text":
        segments, plain_text, mentioned = decode_text(
            str(content.get("text") or ""), mention_list, bot_open_id
        )
    elif message_type == "post":
        segments, plain_text, mentioned = decode_post(content, binaries, bot_open_id)
        if not mentioned:
            mentioned = any(
                str((item.get("id") or {}).get("open_id") or "") == bot_open_id
                for item in mention_list
                if isinstance(item, Mapping)
            )
    elif message_type == "image":
        image_key = str(content.get("image_key") or "").strip()
        binary = binaries.get(image_key) if image_key else None
        if binary:
            segments.append(binary_segment("image", binary))
            plain_text = IMAGE_PLACEHOLDER
        else:
            # 图片拿不到时也要让消息进入对话，否则用户的话会被整条丢掉。
            segments.append({"type": "text", "data": IMAGE_UNAVAILABLE})
            plain_text = IMAGE_UNAVAILABLE
    else:
        # 文件、语音、表情等类型暂不进入对话，避免用占位文本污染上下文。
        plain_text = ""
        segments = []

    if not segments:
        raise ValueError(f"飞书消息不包含可处理的正文（类型 {message_type}）")

    additional_config: Dict[str, Any] = {
        "feishu_chat_id": chat_id,
        "feishu_message_id": message_id,
        "feishu_chat_type": chat_type,
        "feishu_message_type": message_type,
    }
    if is_group:
        additional_config["platform_io_target_group_id"] = chat_id
    else:
        additional_config["platform_io_target_user_id"] = user_id
    root_id = str(message.get("root_id") or "").strip()
    parent_id = str(message.get("parent_id") or "").strip()
    thread_id = str(message.get("thread_id") or "").strip()
    if root_id:
        additional_config["feishu_root_id"] = root_id
    if thread_id:
        additional_config["feishu_thread_id"] = thread_id

    reply_to = parent_id or root_id
    if reply_to:
        segments.insert(
            0,
            {
                "type": "reply",
                "data": {
                    "target_message_id": reply_to,
                    "target_message_content": "",
                    "target_message_sender_id": None,
                    "target_message_sender_nickname": None,
                },
            },
        )

    message_info: Dict[str, Any] = {
        "user_info": {"user_id": user_id, "user_nickname": str(user_id)},
        "additional_config": additional_config,
    }
    if is_group:
        message_info["group_info"] = {"group_id": chat_id, "group_name": group_name or ""}

    result: Dict[str, Any] = {
        "message_id": message_id,
        "timestamp": str(_timestamp(message.get("create_time"))),
        "platform": PLATFORM,
        "account_id": account_id,
        "scope": None,
        "message_info": message_info,
        "raw_message": segments,
        "processed_plain_text": plain_text,
        "is_at": mentioned,
        "is_mentioned": mentioned,
        "is_emoji": False,
        "is_picture": any(str(seg.get("type") or "") == "image" for seg in segments),
        "is_command": plain_text.startswith("/"),
        "session_id": "",
    }
    if reply_to:
        result["reply_to"] = reply_to
    return result


def _timestamp(create_time: Any) -> float:
    """飞书 create_time 是毫秒字符串，统一换算为秒级时间戳。"""
    try:
        value = float(str(create_time))
    except (TypeError, ValueError):
        return 0.0
    if value > 1e11:
        return value / 1000.0
    return value


def split_text(text: str, limit: int = MAX_TEXT_LENGTH) -> List[str]:
    """按上限切分长文本，尽量在换行处断开。"""
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


def encode_outbound(
    segments: List[Mapping[str, Any]],
) -> Tuple[str, List[bytes], str]:
    """把标准消息段转换为飞书发送载荷。

    返回正文文本、图片二进制列表与被引用的消息 ID。
    """
    text_parts: List[str] = []
    images: List[bytes] = []
    reply_to = ""
    for segment in segments:
        kind = str(segment.get("type") or "")
        data = segment.get("data")
        if kind == "text":
            text_parts.append(str(data if data is not None else ""))
        elif kind == "at":
            mapping = data if isinstance(data, Mapping) else {}
            target = str(mapping.get("target_user_id") or "").strip()
            name = str(
                mapping.get("target_user_nickname") or mapping.get("target_user_cardname") or target
            ).strip()
            if target:
                text_parts.append(f'<at user_id="{target}">{name}</at> ')
            else:
                text_parts.append(f"@{name} ")
        elif kind == "reply":
            mapping = data if isinstance(data, Mapping) else {}
            reply_to = str(mapping.get("target_message_id") or "").strip()
        elif kind in {"image", "emoji"}:
            images.append(decode_binary(segment))
        elif kind == "forward":
            # 飞书没有等价的合并转发载荷，转发节点内容由调用方拆成普通消息。
            continue
        else:
            raise ValueError(f"不支持的出站消息段: {kind}")
    return "".join(text_parts).strip(), images, reply_to
