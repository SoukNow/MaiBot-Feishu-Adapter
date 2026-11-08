"""飞书适配器配置模型。"""

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase


class PluginSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(default=False, description="启用飞书机器人连接")
    config_version: str = Field(default="0.1.0", description="配置版本")


class FeishuSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "飞书"
    __ui_order__: ClassVar[int] = 1

    app_id: str = Field(
        default="",
        description="飞书应用 App ID（形如 cli_xxxxxxxxxx）",
        json_schema_extra={"x-widget": "password"},
    )
    app_secret: str = Field(
        default="",
        repr=False,
        description="飞书应用 App Secret",
        json_schema_extra={"x-widget": "password"},
    )
    domain: str = Field(
        default="https://open.feishu.cn",
        description="飞书开放平台域名；国际版 Lark 使用 https://open.larksuite.com",
    )
    proxy_url: str = Field(
        default="",
        repr=False,
        description="HTTP/SOCKS5 代理地址；留空直连，不读取环境代理",
        json_schema_extra={"x-widget": "password"},
    )
    receive_group_message: bool = Field(
        default=False,
        description="是否接收群内全部消息（需 im:message.group_msg 敏感权限，默认只接收 @ 机器人）",
    )
    group_name_enabled: bool = Field(
        default=True,
        description="是否查询群名称（需 im:chat:readonly 权限）",
    )
    max_download_mb: int = Field(
        default=10, ge=1, le=50, description="单个图片下载上限（MB）"
    )
    request_timeout_sec: int = Field(default=30, ge=5, le=300, description="接口请求超时（秒）")
    reconnect_delay_sec: float = Field(default=5.0, ge=1, le=300, description="连接失败后重试间隔（秒）")
    log_level: str = Field(
        default="info",
        description="长连接日志级别：info 可看到 SDK 的连接建立日志；warning 只留告警",
    )


class AdapterSettings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
