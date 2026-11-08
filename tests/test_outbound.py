"""出站路径自检: 目标解析 + 载荷编码 + 发送顺序 (不触网)。

用法:
    python plugins/maibot-feishu-adapter/tests/test_outbound.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codec import encode_outbound, split_text  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def resolve_target(message: dict) -> tuple[str, str]:
    """与 plugin.FeishuAdapterPlugin._resolve_target 保持一致的目标解析。"""
    info = message.get("message_info") or {}
    group = info.get("group_info")
    config = info.get("additional_config") or {}
    if isinstance(group, dict):
        group_id = str(group.get("group_id") or "").strip()
        if group_id:
            return group_id, "chat_id"
    user_id = str(config.get("platform_io_target_user_id") or "").strip()
    if user_id:
        return user_id, "open_id"
    raise ValueError("出站消息缺少目标聊天 ID")


print("群聊出站目标")
group_message = {
    "message_info": {
        "group_info": {"group_id": "oc_group_1", "group_name": "测试群"},
        "additional_config": {
            "platform_io_target_group_id": "oc_group_1",
            "platform_io_target_user_id": "ou_alice",
        },
    }
}
check("群走 chat_id", resolve_target(group_message) == ("oc_group_1", "chat_id"), resolve_target(group_message))

print("私聊出站目标")
private_message = {
    "message_info": {
        "additional_config": {"platform_io_target_user_id": "ou_alice"},
    }
}
check("私聊走 open_id", resolve_target(private_message) == ("ou_alice", "open_id"), resolve_target(private_message))

print("缺失目标应报错")
try:
    resolve_target({"message_info": {}})
    check("缺目标时抛错", False, "没有抛异常")
except ValueError:
    check("缺目标时抛错", True)

print("出站载荷编码")
segments = [
    {"type": "text", "data": "好的"},
    {"type": "at", "data": {"target_user_id": "ou_alice", "target_user_nickname": "Alice"}},
    {"type": "text", "data": "明天见"},
]
text, images, reply_to = encode_outbound(segments)
check("文本与 @ 合并", text == '好的<at user_id="ou_alice">Alice</at> 明天见', text)
check("无图片", images == [])
check("无引用", reply_to == "")

reply_text, reply_images, reply_id = encode_outbound(
    [
        {"type": "reply", "data": {"target_message_id": "om_parent"}},
        {"type": "text", "data": "收到"},
    ]
)
check("引用 ID 解析", reply_id == "om_parent")
check("引用与正文并存", reply_text == "收到")

image_text, image_bin, _ = encode_outbound(
    [{"type": "image", "data": "", "hash": "x", "binary_data_base64": "aGVsbG8="}]
)
check("图片解出二进制", image_bin == [b"hello"], image_bin)
check("纯图片无正文", image_text == "")

print("长文本切分")
chunks = split_text("字" * 9500)
check("切出 3 段", len(chunks) == 3, len(chunks))
check("每段不超 4000", all(len(c) <= 4000 for c in chunks))
check("内容完整", "".join(chunks) == "字" * 9500)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    raise SystemExit(1)
print("全部通过")
