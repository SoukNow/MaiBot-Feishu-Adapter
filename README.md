# 飞书适配器（MaiBot-Feishu-Adapter）

把 [MaiBot](https://github.com/Mai-with-u/MaiBot) 接入飞书（Lark）开放平台的官方机器人。

使用 **长连接（WebSocket）** 模式，不需要公网 IP、域名或回调地址，服务器放在内网也能收消息。

![飞书适配器在 WebUI 中的连接状态](docs/screenshot.png)

## 特性

- 群聊与私聊双向收发
- `@` 提及识别（群内 @ 机器人可触发回复）
- 引用回复（回复某条消息）
- **图片收发**：用户发的图会下载后交给多模态模型识别，机器人也能主动发图
- 富文本（图文混排）消息完整解析
- 断线自动重连、消息去重
- WebUI 插件页可直接查看连接状态、手动重连

## 飞书应用准备

1. 打开 [飞书开放平台](https://open.feishu.cn/) → 开发者后台 → 创建企业自建应用。
2. **应用能力 → 机器人**：启用机器人，填写机器人名称与描述。
3. **权限管理** 添加以下应用权限（tenant 权限）：

   | 权限 | 用途 | 是否必需 |
   |---|---|---|
   | `im:message` | 获取与发送单聊、群组消息 | 必需 |
   | `im:message:send_as_bot` | 以机器人身份发消息 | 必需 |
   | `im:message.p2p_msg:readonly` | 接收用户发给机器人的单聊消息 | 必需 |
   | `im:message.group_at_msg:readonly` | 接收群里 @ 机器人的消息 | 必需 |
   | `im:resource` | 下载用户发的图片、上传机器人要发的图片 | 图片收发时必需 |
   | `im:chat:readonly` | 查询群名称 | 可选，不开则群名显示为 ID |

   需要接收群里**全部**消息时，额外申请敏感权限 `im:message.group_msg`，
   并把插件配置里的 `receive_group_message` 设为 `true`。

   > 权限改动后必须**重新发布版本**才会生效。

4. **事件与回调 → 事件配置**：订阅方式选「使用长连接接收事件」，添加事件 `im.message.receive_v1`。
5. **版本管理与发布**：创建版本并发布（企业租户需要管理员审批）。
6. 从「凭证与基础信息」复制 **App ID** 与 **App Secret**。

> 校验权限是否生效：
> `GET /open-apis/application/v6/scopes?app_id=<APP_ID>&user_id_type=open_id`
> （请求头必须带 `tenant_access_token`，否则返回空列表）。

## 安装

### 通过插件市场

在 MaiBot 的 WebUI 里：**插件扩展 → 插件市场**，搜索「飞书」或 `feishu-adapter`，
安装并启用。依赖 `lark-oapi` 与 `httpx` 会在插件加载时自动安装。

### 手动安装

把本仓库克隆到 MaiBot 的 `plugins/` 目录下，重启后由插件系统生成 `config.toml`：

```bash
git clone https://github.com/SoukNow/MaiBot-Feishu-Adapter.git \
    <MaiBot目录>/plugins/MaiBot-Feishu-Adapter
```

### Docker 部署

MaiBot v1.4 是多 Bot 宿主，第三方插件要装在**实例目录**下（不是 `/MaiMBot/plugins`）：

```
/opt/mai-bot/data/MaiMBot-bots/<bot_id>/plugins/MaiBot-Feishu-Adapter/
```

## 配置

在 WebUI 的 **插件管理 → 飞书适配器 → 设置** 中填写：

| 配置项 | 说明 |
|---|---|
| `app_id` | 飞书应用 App ID（`cli_xxx`） |
| `app_secret` | 飞书应用 App Secret |
| `domain` | 国内版 `https://open.feishu.cn`；国际版 Lark 用 `https://open.larksuite.com` |
| `proxy_url` | 可选代理地址，留空直连 |
| `receive_group_message` | 是否接收群内全部消息（默认只接收 @ 机器人） |
| `group_name_enabled` | 是否查询群名称（需 `im:chat:readonly`） |
| `max_download_mb` | 单张图片下载上限（默认 10 MB） |
| `request_timeout_sec` | 接口请求超时（秒） |
| `reconnect_delay_sec` | 连接失败后重试间隔（秒） |

保存配置后适配器会自动重启连接，状态可在插件页查看。

## 使用

- **私聊**：在飞书搜索机器人名称直接发消息。
- **群聊**：把机器人加入群，@ 机器人发消息。
- 机器人回复时会自动带上「回复」引用关系。
- **发图**：机器人主动发图走 `POST /open-apis/im/v1/images` 上传后发送，
  需要 `im:resource` 权限。

## 常见问题

| 现象 | 检查 |
|---|---|
| 插件页显示「请先填写飞书 App ID 与 App Secret」 | 配置未保存或字段为空 |
| 日志「飞书鉴权失败」 | App ID/Secret 错误、应用未发布、机器人能力未启用 |
| 收到连接但群里不回复 | 应用缺 `im:message.group_at_msg` 权限；权限变更后需重新发布版本；机器人不在群里 |
| 单聊无响应 | 缺 `im:message.p2p_msg` 权限；或应用可用范围未包含当前用户 |
| 长连接反复重连 | 检查服务器出网（需能访问 `open.feishu.cn` 与 `msg-frontier.feishu.cn` 的 443 端口） |
| 图片发不出 | 缺 `im:resource` 权限；图片超过 `max_download_mb` |
| 机器人说「图片没加载出来」 | 见下面「图片为什么看不到」 |

### 图片为什么看不到

飞书的图文混排消息（输入框里「带图发字」）类型是 **post 富文本**，图片藏在
`content[][]` 里的 `{"tag": "img", "image_key": "..."}`，而不是独立的 image 消息。
适配器必须把这些 `image_key` 下载成二进制再交给主程序，只传一段 `[图片]` 占位文本
是看不到图的。

另外两个容易踩的坑：

- **下载用户发的图不能用「下载图片」接口**。`GET /im/v1/images/{image_key}`
  只能下载**本应用自己上传过**的图片，用户发来的图用它会返回 `234001 Invalid request param`。
  用户消息里的图要用
  `GET /im/v1/messages/{message_id}/resources/{file_key}?type=image`，
  且 `file_key` 必须与 `message_id` 匹配，机器人也要在该会话内。
- **multipart 上传不能写死默认 `Content-Type`**。给 HTTP 客户端设了
  `Content-Type: application/json` 会覆盖掉 `multipart/form-data; boundary=...`，
  飞书收不到文件字段并报同样的 234001。

日志位置：MaiBot 的 `logs/` 目录，或 WebUI 的实时日志。
搜 `保存图片` / `理解图片` 可以确认图片是否已落盘并生成描述。

## 开发者自检

仓库自带离线自检脚本，只依赖标准库（`test_sdk_dispatch.py` 需要 `lark-oapi`）：

```bash
python tests/test_codec.py         # 消息转换（文本 / 富文本 / 图片段）
python tests/test_api_paths.py      # 断言每个 REST 接口的真实 URL
python tests/test_outbound.py       # 出站目标解析与载荷编码
python tests/test_sdk_dispatch.py   # 真实调用 lark-oapi 的事件分发
python tests/test_deploy_config.py  # 配置写入自检
```

## License

[MIT](LICENSE)
