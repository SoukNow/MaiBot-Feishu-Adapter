"""校验 deploy-feishu-adapter.sh 里的配置写入逻辑: 产出必须是合法 TOML, 且类型正确。"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

SAMPLE = """[plugin]
enabled = false
config_version = "0.1.0"

[feishu]
app_id = ""
app_secret = ""
domain = "https://open.feishu.cn"
receive_group_message = false
"""


def set_value(text: str, section: str, key: str, raw: str) -> str:
    pattern = re.compile(r"(?ms)^\[" + re.escape(section) + r"\]\s*\n(.*?)(?=^\[|\Z)")
    match = pattern.search(text)
    body = match.group(1) if match else ""
    line = f"{key} = {raw}"
    if re.search(r"(?m)^" + re.escape(key) + r"\s*=", body):
        body = re.sub(r"(?m)^" + re.escape(key) + r"\s*=.*$", line, body)
    else:
        body = body.rstrip("\n") + "\n" + line + "\n"
    if match:
        return text[: match.start(1)] + body + text[match.end(1):]
    return text.rstrip("\n") + f"\n\n[{section}]\n" + body


text = set_value(SAMPLE, "feishu", "app_id", '"cli_test"')
text = set_value(text, "feishu", "app_secret", '"secret_test"')
text = set_value(text, "plugin", "enabled", "true")

print(text)
print("-----")
data = tomllib.loads(text)
assert data["plugin"]["enabled"] is True, data["plugin"]["enabled"]
assert data["feishu"]["app_id"] == "cli_test"
assert data["feishu"]["app_secret"] == "secret_test"
assert data["feishu"]["receive_group_message"] is False
assert data["plugin"]["config_version"] == "0.1.0"
assert data["feishu"]["domain"] == "https://open.feishu.cn"
print("TOML 合法, 类型正确 (enabled 为布尔 true)")

# 幂等: 再写一次不应产生重复键
text2 = set_value(text, "feishu", "app_id", '"cli_test2"')
assert text2.count("app_id") == 1, text2
print("重复写入无副作用")
print("全部通过")
