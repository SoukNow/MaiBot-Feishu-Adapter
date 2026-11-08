# Changelog

## 0.1.3

修复「在飞书发图，机器人看不见图」的问题：

- **图文混排的富文本消息丢图**：飞书在输入框里「带图发字」时，消息类型不是
  `image` 而是 `post`（富文本）。原实现只在 `message_type == "image"` 时下载图片，
  `post` 分支更是把 `img` 标签直接降级成纯文本 `[图片]`，图片二进制从未进入
  主程序。结果是主程序只收到一句「[图片]」占位文本，没有任何可识别的图像。
  现在 `codec.collect_post_image_keys()` 会取出富文本里的全部 `image_key`，
  `plugin.py` 统一下载后以标准二进制图片段透传，交给主程序落盘、识别并生成描述。
- **富文本里的 @ 提及**：原先被当成纯文本 `@名字`，现在还原成标准 `at` 段，
  群聊里 @ 机器人能正确触发。
- **图片下载失败不再吞掉整条消息**：下载不到二进制时降级为 `[图片无法读取]`
  占位文本，用户说的话仍能进入对话；此前纯图片消息会整条被丢弃。
- `is_picture` 改为按实际消息段判定，图文混排的消息也能被识别为图片消息。
- **入站图片下载接口用错**：`GET /im/v1/images/{image_key}` 按飞书官方文档
  「**只能下载由当前机器人上传的图片**」，用户发来的图用它一律返回
  `234001 Invalid request param`。用户消息里的图必须走
  `GET /im/v1/messages/{message_id}/resources/{file_key}?type=image`
  （file_key 要与 message_id 匹配，机器人要在该会话内）。
  现在入站优先用消息资源接口，失败再退回图片下载接口。
- **出站图片上传报 `Invalid request param`**：客户端给 httpx 设了默认
  `Content-Type: application/json`，multipart 上传时 httpx 自己生成的
  `multipart/form-data; boundary=...` 头被覆盖，请求体是 multipart 而头写着 json，
  飞书收不到 `image` 字段直接报 code 234001。现在不再设置默认 Content-Type，
  交给 httpx 按请求体类型自动生成。出站图片此前从未真正跑通。

## 0.1.2

修复「长连接已连接、网关已就绪，但一条消息都收不到」的问题：

- **SDK 方法名版本自适应**：lark-oapi 1.5.x 的 ws client 调用
  `do_without_validation`，1.7.x 改成了 `_do_without_validation`。
  原先写死新名字，在旧版 SDK 上会误判成「处理器无效」并把长连接线程打死。
  现在改为探测 SDK 源码确定实际方法名，并在混用版本时补别名兜底。
- `ws_handler.ensure_handler_method()` 拆出别名逻辑，可单独测试。

## 0.1.1

修复两个会导致「在飞书发了消息，机器人完全没反应」的问题：

- **入站**: `EventDispatcherHandler.builder(...).register_p2_im_message_receive_v1(...)`
  返回的是 *builder*，而 `lark_oapi/ws/client.py` 收到事件时直接调用
  `event_handler._do_without_validation(...)` —— 该方法只存在于 `.build()`
  出来的 `EventDispatcherHandler` 上。少写 `.build()` 时消息能到达服务器，
  但分发时抛 `AttributeError`，表现为「长连接已连接、网关已就绪，但收不到任何消息」。
  现在构造逻辑抽到 `ws_handler.build_event_handler()`，并在启动时校验处理器可用。
- **出站**: 发送消息接口应为 `/open-apis/im/v1/messages`（飞书没有 v2）。
  写成 v2 会得到纯文本 `404 page not found`。

其他：

- `api.py` 不再使用 `raise_for_status()`，改为把 HTTP 状态码、请求路径与响应体
  一起放进 `FeishuAPIError`，避免再遇到只显示 “404 Not Found” 无法定位的情况。
- `api.py` 兼容 httpx 0.25 的 `proxies=` 与 0.28+ 的 `proxy=` 两种写法。
- 新增两个跑真实路径的回归测试：
  - `tests/test_sdk_dispatch.py` —— 真实调用 lark-oapi 的事件分发，验证回调被触发
  - `tests/test_api_paths.py` —— 用 `httpx.MockTransport` 断言每个接口的真实 URL

## 0.1.0

- 首个版本：飞书开放平台官方机器人适配器
- 长连接模式收发群聊与私聊消息
- 支持 `@` 提及、引用回复与图片收发
- 断线自动重连与消息去重
- WebUI 插件页显示连接状态并支持手动重连
