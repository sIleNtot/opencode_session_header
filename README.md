# OpenCode 会话请求头注入

本插件为 AstrBot 提供 OpenCode 会话请求头注入能力：发往 OpenCode（Zen / Go）端点的 LLM 请求会被自动补上 `x-opencode-session` 会话头，从而正常调用 OpenCode Go 的全部模型；Anthropic、OpenAI、OpenAI Responses、Gemini 四类 Provider 与 `zen` / `inference` 全部端点默认全覆盖。

> 本仓库为 Fork，重构自上游 [AnzioNiko/astrbot_plugin_opencode_session_header](https://github.com/AnzioNiko/astrbot_plugin_opencode_session_header)，当前实现与上游已完全不同（差异见文末「与上游差异」）。本仓库地址：[sIleNtot/opencode_session_header](https://github.com/sIleNtot/opencode_session_header)。

## 功能

OpenCode 网关要求每个请求携带稳定且可见的 `x-opencode-session` 会话头，缺失时直接返回 `400 MissingSessionID`，而 AstrBot 自身不会发送该头。本插件在 httpx / httpx2 传输层统一拦截请求：只要最终 URL 指向 OpenCode 端点，无论请求来自哪种 Provider、经由哪种底层 SDK，都会被自动补上会话头。

| Provider 类型 | 是否支持 | 底层 SDK |
| --- | --- | --- |
| Anthropic Compatible | ✅ 全部支持 | `anthropic` |
| OpenAI Compatible | ✅ 全部支持 | `openai` |
| OpenAI Responses | ✅ 全部支持 | `openai` |
| Gemini Compatible | ✅ 全部支持 | `google-genai` |

匹配依据是**最终请求 URL**（主机 + 路径前缀），而不是 Provider 类型，因此四类 Provider 对应的四种 API 形态由同一套逻辑覆盖：

- Anthropic Messages：`/v1/messages`；
- OpenAI Chat Completions：`/v1/chat/completions`；
- OpenAI Responses：`/v1/responses`；
- Gemini `generateContent`：`/v1beta/models/<模型>:generateContent`。

不变式（任何情况下都成立）：

- **已有非空会话头绝不覆盖**：请求若已携带（例如你自己在 Provider 的 `custom_headers` 中配过），原值优先，插件填充的镜像头也会与之一致（你已自设的非空镜像头除外，见「会话 ID 设计」）；
- **非目标端点零影响**：OpenAI、DeepSeek、Ollama 等其它 API 的请求原样抵达，不产生任何副作用；
- **UA 不改写**：保留 AstrBot 自身的 `astrbot/<版本>`，不伪造 OpenCode 客户端身份；
- **全量容错**：注入路径中的任何异常只记日志，绝不抛出，绝不影响请求本身。

## 安装

1. **插件市场**：在 AstrBot 插件市场搜索「OpenCode 会话请求头注入」安装；
2. **手动安装**：将本仓库整个目录放入 AstrBot 的 `data/plugins/astrbot_plugin_opencode_session_header`；
3. 重载插件或重启 AstrBot。

运行时无需额外安装第三方 Python 依赖：`httpx`、`httpx2`、`openai`、`anthropic`、`google-genai` 均由 AstrBot 环境自带。

## 快速配置

在 AstrBot 中新建或修改 Provider 时，按模型对应的 API 形态填写：

| Provider 类型 | 示例 API 地址（填写值） | 最终请求路径 |
|---|---|---|
| Anthropic Compatible | `https://opencode.ai/zen/go` | `/zen/go/v1/messages` |
| OpenAI Compatible | `https://opencode.ai/zen/go/v1` | `/zen/go/v1/chat/completions` |
| OpenAI Responses | `https://opencode.ai/zen/go/v1` | `/zen/go/v1/responses` |
| Gemini Compatible | `https://opencode.ai/inference/go/google` | `/inference/go/google/v1beta/models/...` |
| Anthropic（直连端点） | `https://opencode.ai/inference/go/anthropic` | `/inference/go/anthropic/v1/messages` |
| OpenAI（直连端点） | `https://opencode.ai/inference/go/openai/v1` | `/inference/go/openai/v1/...` |

表中的示例地址全部落在默认匹配规则（主机 `opencode.ai` 及其点后缀子域，路径前缀 `/zen`、`/inference`）之内，装好插件后无需任何额外配置即可工作。

## 配置表

| 键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关；关闭后插件不修改任何请求，等同于未安装。 |
| `target_hosts` | list | `["opencode.ai"]` | 目标主机列表；命中主机或其点后缀子域才会注入，也可整段填写 URL（其路径会一并作为匹配前缀）。 |
| `target_path_prefixes` | list | `["/zen", "/inference"]` | 目标路径前缀；按段匹配（`/zen` 不匹配 `/zenx`），空列表 = 任意路径。 |
| `header_name` | string | `x-opencode-session` | 注入的主请求头名；留空或含不可见字符时回退默认值。 |
| `mirror_opencode_headers` | bool | `true` | 是否附加 OpenCode Desktop 同款镜像头（`x-opencode-session-id`、`x-session-affinity`、`X-Session-Id`、`x-opencode-client: astrbot`）；已存在的非空头不覆盖。 |
| `missing_session_mode` | string | `random` | 缺少会话上下文时的兜底方式：`random` 或 `fixed`；取值非法自动降级 `random`。 |
| `fixed_session_id` | string | `""` | `fixed` 模式使用的固定会话值，仅允许可见 ASCII；留空或非法自动降级 `random`。 |
| `log_injection` | bool | `true` | 注入成功时记录一条 INFO 日志。 |
| `log_skip` | bool | `true` | 仅当请求命中目标主机（`target_hosts`）但未注入时记录一条 INFO 日志说明原因（禁用或不匹配）；其它主机的请求不产生日志。 |

关于兜底语义：只要存在对话上下文（如 `req.conversation.cid`），会话 ID 永远按上下文确定性推导，兜底配置不参与。`random` 会在插件加载时生成一个 `ses_` 随机会话，并在本次进程内复用（重启或重载后更换）；`fixed` 则使用 `fixed_session_id` 的值，适合希望兜底会话跨重启保持不变的场景。修改配置后重载插件即可生效。

## 会话 ID 设计

- **格式**：`ses_` + 32 位小写十六进制，对齐 OpenCode Desktop 的会话 ID 约定；每个 AstrBot 对话恒定、跨重启稳定，对话重置（新 cid）即自动换新。
- **推导**：优先取 `req.conversation.cid`（AstrBot 对话 UUID），取不到时依次回退 `unified_msg_origin` 与 `req.session_id`。来源是合法 UUID 时直接取其去连字符的十六进制（`550e8400-e29b-41d4-a716-446655440000` → `ses_550e8400e29b41d4a716446655440000`）；其它来源字符串取 sha256 前 32 位十六进制，同样稳定可复现。
- **与 OpenCode Desktop 的对应关系**：Desktop 同样以 `ses_` 开头的会话 ID 标识会话；本插件生成的值与其同格式、同语义（对每个会话稳定、仅可见 ASCII），网关据此完成路由与提示缓存。
- **不覆盖用户自配值**：请求已带非空 `x-opencode-session` 时以原值为准，插件填充的镜像头与该值保持一致；但已由调用方设置的非空镜像头同样被保留 —— 若你把某个镜像头设成了与主头不同的值，两者会按你设置的值保持不同，插件不会强行对齐。
- **镜像头集**（`mirror_opencode_headers = true` 时）：`x-opencode-session-id`、`x-session-affinity`、`X-Session-Id` 取值与主头一致；`x-opencode-client` 固定为 `astrbot`；已由调用方设置的非空镜像头不覆盖。
- **缓存**：来源 → 会话 ID 的纯函数结果在进程内缓存（上限 4096 条），仅内存、不落盘。

## 命令

`/oc-session`（仅管理员）：查看当前会话的注入状态 —— 会话来源与来源值、将使用的主头值、匹配规则、启用状态与已挂载的传输钩子。

```text
会话来源：对话上下文
来源值：550e8400-e29b-41d4-a716-446655440000
x-opencode-session: ses_550e8400e29b41d4a716446655440000
匹配规则：opencode.ai | /zen, /inference
启用状态：已启用；传输钩子：httpx.AsyncClient.send, openai._base_client.httpx2.AsyncClient.send, ...
```

## FAQ

**Q：使用 OpenCode Go 时一直报 `400 MissingSessionID`？**

该错误表示请求缺少网关强制要求的会话头。排查顺序：

1. 用 `/oc-session` 查看「启用状态」与「匹配规则」，确认插件已加载、URL 确实命中；
2. 开启 `log_injection` / `log_skip`，从日志里确认真实请求 URL —— 只要它位于 `opencode.ai` 下的 `/zen` 或 `/inference` 路径，就应能看到注入日志；
3. 若你曾在 Provider 的 `custom_headers` 中配过非空的 `x-opencode-session`（例如从别处复制的、与当前会话不符的值），它会优先于插件推导值；空值或纯空白会被视为未设置，插件照常注入 —— 若自配值不正确，清空它即可让插件接管。

**Q：为什么必须装这个插件？**

AstrBot 各类型 Provider 都不会发送 `x-opencode-session`，网关缺失即 400，不装插件无法使用；而只 hook OpenAI SDK 的旧方案对 Anthropic / Gemini 类型 Provider 与 `inference` 直连端点无能为力，本插件对这些场景同样生效。

**Q：低版本 AstrBot 会被拒绝或降级不可用吗？**

不会拒绝：本插件不声明最低版本，所有可选属性（如 `req.conversation`、`unified_msg_origin`、内部调度器）均按存在性探测，缺失时逐级降级 —— 例如读不到对话 cid 就回退 umo；调度器兜底钩子装不上只记一条警告，不影响加载；传输钩子按运行时模块发现，缺哪个模块就跳过哪个。旧版 AstrBot 上同样能工作，只是可用的兜底路径更少。

## 隐私

- 不发起任何额外网络请求；真实的模型请求仍由 AstrBot 与所配置的 Provider 发出。
- 不收集、不上传、不落盘任何用户数据；聊天内容与模型参数完全不经本插件处理。
- 仅有的内存状态：来源 → 会话 ID 的映射缓存（上限 4096 条）与钩子安装记录；插件卸载 / 禁用时钩子全部还原并清空记录。
- 会话 ID 只是会话标识，不是密钥、令牌或认证凭据；日志中的会话值可通过 `log_injection` / `log_skip` 关闭输出。

## 开发

代码结构：`core.py`（端点匹配、会话推导、注入引擎、传输钩子，零 AstrBot 依赖）+ `main.py`（AstrBot 胶水层）。提交前请运行静态检查：

```bash
ruff check .
```

CI（GitHub Actions）在 Python 3.12 上执行同样的 `ruff check .`。

## 与上游差异

- **拦截方式**：上游 hook OpenAI SDK 内部实现（`_prepare_request` 等），本插件改在 httpx / httpx2 传输层统一拦截，覆盖全部 SDK 与四类 Provider；
- **端点范围**：上游只匹配单一端点 `https://opencode.ai/zen/go/v1`；本插件改为「主机 + 路径前缀」匹配，默认覆盖 `opencode.ai` 下 `/zen`、`/inference` 的全部端点及子域；
- **会话 ID**：上游为 UUID v4 形态；本插件改为对齐 OpenCode Desktop 的 `ses_` + 32 位十六进制格式；
- **镜像头**：上游只写一个会话头；本插件默认附加 Desktop 同款镜像头集；
- **代码结构**：新增零 AstrBot 依赖的 `core.py`（端点匹配、会话推导、注入引擎、传输钩子），`main.py` 收敛为薄胶水层；
- **配置项**：移除 `target_endpoint`、`ignore_trailing_slash`、`fixed_session_uuid`，改为 `target_hosts`、`target_path_prefixes`、`fixed_session_id` 等九项（见配置表）；
- **CI**：新增 GitHub Actions 工作流（ruff 静态检查）。

## 从 1.x 升级

1.x 的配置键 `target_endpoint`、`ignore_trailing_slash`、`fixed_session_uuid` 在 2.x 中不再读取（旧键会留在配置文件中但被忽略），请按「配置表」更新：`target_endpoint` + `ignore_trailing_slash` 改用 `target_hosts` + `target_path_prefixes` 表达（默认值即覆盖原单端点，通常无需额外配置）；`fixed_session_uuid` 请把固定值迁移到 `fixed_session_id`。

只配置过 `fixed_session_uuid` 的用户在 2.x 下 `fixed_session_id` 为空，固定兜底会话不再生效：若 `missing_session_mode` 仍为 `fixed`，插件加载时会输出「fixed_session_id 为空或含不可见字符，已降级为 random」警告并回退随机兜底（每次重启更换会话值）；请务必更新配置。

致谢：本插件基于上游 [AnzioNiko/astrbot_plugin_opencode_session_header](https://github.com/AnzioNiko/astrbot_plugin_opencode_session_header) 重构而来，感谢原作者的开拓工作。
