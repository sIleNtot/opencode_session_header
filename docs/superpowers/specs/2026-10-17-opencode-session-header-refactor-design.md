# OpenCode 会话请求头注入插件 —— Fork 重构设计

- 日期：2026-10-17
- 仓库：`sIleNtot/opencode_session_header`（fork 自 `AnzioNiko/astrbot_plugin_opencode_session_header`）
- 插件 ID：`astrbot_plugin_opencode_session_header`（保持不变，避免已装用户升级断档）

## 1. 背景与意图

本插件为 AstrBot 指向 OpenCode 端点（Zen / Go）的 LLM 请求自动注入 `x-opencode-session` 会话请求头，使 AstrBot 能作为客户端正常使用 OpenCode Go 的全部模型（网关自 09/05 起强制要求会话头，缺失直接返回 `400 MissingSessionID`）。

现有实现（上游 v1.0.0）只 hook OpenAI SDK 的 `_prepare_request` 与 `chat.completions.create` / `responses.create`，且只匹配单一端点 `https://opencode.ai/zen/go/v1`，因此：

- AstrBot 使用 **Anthropic 类型 Provider**（底层 `anthropic` SDK）指向 `https://opencode.ai/zen/go` 时完全不被覆盖 → `400 MissingSessionID`（用户实测）。
- AstrBot 使用 **Gemini 类型 Provider**（底层 `google-genai` SDK）同样不被覆盖。
- `https://opencode.ai/inference/go/...` 系列直连端点未被适配。

本次目标：**完全重构**，使插件覆盖 OpenCode Go 全部模型（Anthropic Messages / Chat Completions / OpenAI Responses / Gemini 四种 API 形态），行为与 OpenCode Desktop 一致，仓库全部文件重写为 Fork 作者自己的内容，提交记录中说明相对原仓库的修改。

## 2. 关键事实（已通过源码/二进制/文档侦察验证）

### 2.1 OpenCode Desktop 的真实行为（v2.0.32，CLI 与 Desktop 同一内核二进制）

桌面端所有模型请求经中央构造路径 `SessionModelRequest.prepare` 统一附加以下请求头：

| 请求头 | 值 | 说明 |
|---|---|---|
| `x-opencode-session` | 亲和会话 ID | **核心头**，网关路由与提示缓存依据 |
| `x-opencode-session-id` | 实际会话 ID | |
| `x-session-affinity` | 亲和会话 ID | |
| `X-Session-Id` | 亲和会话 ID | |
| `x-opencode-parent-session-id` / `x-parent-session-id` | 父会话 ID | 仅子会话（subagent）携带 |
| `x-opencode-project` | 项目 ID | 40 位十六进制 |
| `x-opencode-client` | 应用名 | Desktop 默认 `opencode` |
| `User-Agent` | `opencode/<channel>/<version>/<name>` | 如 `opencode/latest/2.0.32/opencode` |

其中「亲和会话 ID」= `parentID ?? fork?.sessionID ?? id`（子会话的请求归属到父会话，保证路由与缓存一致）。

会话 ID 格式（本地 `opencode.db` 实测）：`ses_` + 12 位十六进制 + 14 位随机串（例：`ses_ec05bf214ffdx5ouBvgIwZ64Q8`），共 `ses_` + 26 字符。社区网关客户端（kRouter）使用 `ses_` + 32 位小写十六进制，已被网关验证兼容——**网关只要求值对每个会话稳定且为可见 ASCII，不解析内容**。

### 2.2 OpenCode Go 端点（官方文档 console/go）

- OpenCode 客户端自身（`opencode-go` provider，models.json）：单一 OpenAI 兼容端点 `https://opencode.ai/zen/go/v1`，36 个模型（GPT/Grok/Claude/Qwen/MiniMax/GLM/DeepSeek/Kimi/…）。用户 AstrBot 实测亦使用 `https://opencode.ai/zen/go`，经 Anthropic SDK 补全为 `https://opencode.ai/zen/go/v1/messages`——说明该聚合端点同时提供 Messages 形态。
- 官方直连端点（按模型 API 形态）：
  - `https://opencode.ai/inference/go/openai/v1/chat/completions`（多数模型）
  - `https://opencode.ai/inference/go/openai/v1/responses`（Grok 4.7、GPT 6 Luna、GPT 5.6 Luna、Muse Spark 等）
  - `https://opencode.ai/inference/go/anthropic/v1/messages`（Claude Haiku 5.5、MiniMax M3、Qwen3.x 等）
  - `https://opencode.ai/inference/go/google/v1beta/models/<model>:generateContent`（Gemini 4 Argon Contributor）
- 官方对客户端的要求：① 典型 coding agent 流量；② 使用自己的 User-Agent 标识（如 `my-agent/1.0`）；③ 每个会话发送稳定的 `x-opencode-session`。

### 2.3 AstrBot 侧架构（master，4.29.0）

四种 LLM Provider 实现，底层全部经由 httpx 生态发包：

| Provider 类型 | 源码 | 底层 SDK | 客户端构造 |
|---|---|---|---|
| OpenAI Compatible | `sources/openai_source.py` | `openai` | `AsyncOpenAI(base_url=api_base, default_headers=request_headers, http_client=...)` |
| OpenAI Responses | `sources/openai_responses_source.py` | `openai` | 继承前者，调 `client.responses.create` |
| Anthropic Compatible | `sources/anthropic_source.py` | `anthropic` | `AsyncAnthropic(base_url=..., default_headers=...)`，base 会先去尾 `/v1` |
| Gemini Compatible | `sources/gemini_source.py` | `google-genai` | `genai.Client(http_options=HttpOptions(base_url=..., httpx_async_client=...))` |

**传输模块并不统一**（PyPI 实测，2026-10-17；AstrBot 源码注释亦确认）：

| SDK | 传输依赖 |
|---|---|
| `openai` 3.33.0 | `httpx2>=2.12`（httpx 分支包） |
| `anthropic` 1.15.0 | `httpx2>=2`（anthropic 1.0.0+ 将内置 httpx 更名为 `_base_client.httpx2`） |
| `google-genai` 2.31.0 | `httpx>=0.28`（原版 httpx） |

AstrBot 核心既有能力（插件应与之协同、不重复造轮子）：

- `astrbot/core/provider/headers.py`：默认 `User-Agent: astrbot/<version>`；每请求附 `x-astrbot-conversation-id: <cid>`（cid 为对话 UUID，属 AstrBot 自己的会话概念）。
- `on_llm_request` 钩子：`ProviderRequest` 含 `conversation`（可空），`req.conversation.cid` 即当前对话 UUID；`session_id` 字段已废弃。
- 属性均需按存在性探测，兼容旧版 AstrBot（未见即降级）。

## 3. 目标 / 非目标 / 反目标

**目标**

1. 只要请求最终发往 OpenCode 端点（`opencode.ai` 下 `/zen/*` 或 `/inference/*` 路径），无论经由哪种 Provider 类型 / SDK，均注入会话头。
2. 会话 ID 语义与 OpenCode Desktop 对齐：`ses_` 前缀、每个 AstrBot 对话稳定、对话重置（新 cid）即新会话、重启后不变。
3. 仓库所有文件（`main.py`、`README.md`、`metadata.yaml`、`_conf_schema.json` 等）重写为 Fork 作者内容，无上游文案残留；提交信息说明相对原仓库的修改。
4. 提供测试与 CI，保证注入逻辑可靠性。

**非目标**

- 不伪造 OpenCode 的 User-Agent（保持 AstrBot 自己的 `astrbot/<版本>`，符合官方要求）。`x-opencode-project` 不注入（AstrBot 无对应概念）。
- 不修改 AstrBot 任何核心文件；不要求上游合入。
- 不处理非 OpenCode 端点（OpenAI / DeepSeek / Ollama 等请求零影响）。
- 不做跨进程持久化会话存储（纯确定性推导，无状态）。
- 不改插件 ID 与目录名。

**反目标（出现即为失败）**

- 插件任何异常影响请求流程（注入逻辑必须全量容错）。
- 覆盖已有 `x-opencode-session` 值（如用户通过 Provider `custom_headers` 自配）。
- 配置复杂化：保持少量、可理解的开关。

## 4. 设计

### 4.1 总架构：httpx 生态多模块统一拦截

不再 hook 各 SDK 内部实现（`_prepare_request` 等，版本脆弱且覆盖不全），改为在 **httpx 传输入口**统一拦截。

安装策略为**运行时多模块发现**（去重、缺省跳过、逐模块幂等打标）：

1. 顶层 `httpx`：`httpx.Client.send` / `httpx.AsyncClient.send`
2. 顶层 `httpx2`：`httpx2.Client.send` / `httpx2.AsyncClient.send`
3. `openai._base_client` 暴露的 `httpx` / `httpx2` 属性模块
4. `anthropic._base_client` 暴露的 `httpx` / `httpx2` 属性模块

对每个发现的模块，包装其 `Client.send` 与 `AsyncClient.send`（类级、按类对象去重、带标记；`terminate` 时全部还原）。SDK 内部的客户端包装类（如 stainless 的 `AsyncHttpxClientWrapper`）继承自这些基类，方法查找在调用时经 MRO 命中我们的包装器；`request()` 内部亦调用 `send`。

- 包装器内：检查总开关 → 按**最终 URL**匹配目标 → 读取当前会话（ContextVar）→ 按保留策略写入主头与镜像头。
- 所有逻辑置于 `try/except`，任何异常仅记日志，绝不抛出。
- README 记录兼容性说明：覆盖 `httpx` / `httpx2` 两种传输模块；若未来出现第三种分支，按同一发现机制扩展。

### 4.2 端点匹配

按最终 URL 的 `host` + `path` 匹配：

- 主机：`urlsplit(url).hostname`（不含端口）与 `target_hosts` 列表比较，规则：完全相等或为其点后缀子域。默认 `["opencode.ai"]`。
- 路径：`path` 以 `target_path_prefixes` 中任一项开头，且前缀后为 `/` 或结束（避免 `/zenx` 误配）。默认 `["/zen", "/inference"]`。空列表 = 任意路径。
- 覆盖矩阵（全部命中默认配置）：
  - `https://opencode.ai/zen/go/v1/messages`（Anthropic 类型 → 用户当前配置）
  - `https://opencode.ai/zen/go/v1/chat/completions`、`.../responses`
  - `https://opencode.ai/zen/v1/...`（Zen 同样受益）
  - `https://opencode.ai/inference/go/anthropic/v1/messages`
  - `https://opencode.ai/inference/go/openai/v1/chat/completions`、`.../responses`
  - `https://opencode.ai/inference/go/google/v1beta/models/<model>:generateContent`
- 反例必须不匹配：`https://api.openai.com/...`、`https://opencode.ai/console/...`、`https://example.com/zen/...`、`https://opencode.ai/zenx/...`。

### 4.3 会话 ID 推导（对齐 OpenCode Desktop）

- 输入优先 `req.conversation.cid`（AstrBot 对话 UUID）；无 conversation 时回退 `event.unified_msg_origin`（umo）。
- 推导规则（确定性、跨重启稳定）：
  - cid/umo 为 UUID：`ses_` + UUID 去连字符的小写十六进制（32 位）。例：cid `550e8400-e29b-41d4-a716-446655440000` → `ses_550e8400e29b41d4a716446655440000`。
  - 其它输入：`ses_` + `sha256(input)` 前 32 位十六进制。
- 结果行为与 Desktop 等价：同一对话恒定、新对话新 ID、值仅含 `[0-9a-f_]`（可见 ASCII 安全）；调试可复现（`/oc-session` 同时展示 cid 与推导值）。
- 缓存：`cid → session` 纯函数结果，进程内缓存上限 4096。
- 无任何会话上下文时：按配置兜底 `random`（插件加载时生成一次，进程内复用）或 `fixed`（`fixed_session_id`；校验仅可见 ASCII `^[\x21-\x7e]+$`，非法则告警并降级 random）。

### 4.4 注入的请求头

| 头 | 默认 | 说明 |
|---|---|---|
| `x-opencode-session` | 会话 ID | 必需；头名可配置（`header_name`） |
| `x-opencode-session-id` | 会话 ID | 镜像 Desktop 头集（`mirror_opencode_headers=true` 时） |
| `x-session-affinity` | 会话 ID | 同上 |
| `X-Session-Id` | 会话 ID | 同上 |
| `x-opencode-client` | `astrbot` | 同上，标识客户端身份 |

**保留与一致性策略**：

1. 计算「有效会话值」：若请求已携带非空的主头（`header_name`，兼容用户自配 Provider `custom_headers`）→ 取既有值；否则取推导值。
2. 主头：缺省或为空时写入有效值；非空既有值原样保留。
3. 镜像头（镜像开关开启时）：缺省或为空时写入**同一个有效值**（保证与主头一致，等同 Desktop 的相等语义）；个别镜像头若用户显式设置过，则保留其值。
4. **不动** `User-Agent`（AstrBot 已设 `astrbot/<版本>`，符合官方"用自己的 UA"要求）与 `x-astrbot-conversation-id`（AstrBot 核心行为）。
5. AstrBot 无子会话概念，亲和头与主头取值一致。

### 4.5 会话上下文传递（ContextVar）

- 模块级 `ContextVar[str|None]` 存当前最终会话 ID；`ContextVar` 随任务链传播，天然隔离并发对话。
- 两条写入路径：
  1. `on_llm_request` 钩子：解析 `req.conversation.cid`（探测存在性）→ 推导 → `set()`（首选，与 AstrBot 对话概念一致）。
  2. 包装 `PipelineScheduler.execute`（运行时探测导入；失败仅警告）：事件入口按 `unified_msg_origin` 设置兜底值，`finally` 中 `reset()` 防串线——与上游已验证机制一致的保守兜底。
- `llm_generate` 等无事件路径：读取不到会话 → 走兜底模式。

### 4.6 配置项（重写 `_conf_schema.json`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | true | 总开关 |
| `target_hosts` | list | `["opencode.ai"]` | 目标主机列表 |
| `target_path_prefixes` | list | `["/zen", "/inference"]` | 目标路径前缀；空列表 = 任意路径 |
| `header_name` | string | `x-opencode-session` | 主头名 |
| `mirror_opencode_headers` | bool | true | 是否附加 Desktop 镜像头集 |
| `missing_session_mode` | string | `random` | 无会话上下文兜底：`random` / `fixed` |
| `fixed_session_id` | string | `""` | fixed 模式取值（可见 ASCII 校验） |
| `log_injection` | bool | true | 注入成功日志 |
| `log_skip` | bool | true | 跳过原因日志 |

（移除上游的 `target_endpoint`、`ignore_trailing_slash`、`fixed_session_uuid`。）

### 4.7 命令与日志

- 保留管理员命令 `/oc-session`：展示 umo、当前 cid、推导会话 ID、启用状态、匹配规则（hosts + prefixes）、已挂载的传输模块清单。仅管理员可用（不向普通成员暴露会话标识）。
- 日志前缀统一 `[opencode_session_header]`；级别过滤交由 AstrBot 全局 `log_level`。

### 4.8 容错与安全

- 不发起网络请求、不收集/上传数据、不读写聊天内容；仅内存状态（会话缓存 + 钩子记录）。
- 钩子安装幂等（模块级活动实例 + 标记位），`terminate` 完整还原并清空。
- 固定值校验可见 ASCII，防 CRLF 注入导致头部异常。

## 5. 验证计划

**可测试性架构**：逻辑拆分两个文件——

- `core.py`：纯逻辑，**零 AstrBot 依赖**（可独立 import）——端点匹配、会话推导、缓存、保留策略、头注入函数、httpx 多模块发现与包装安装/卸载。
- `main.py`：薄胶水层（约百行）——`Star` 子类、配置读取、`on_llm_request` 钩子、`PipelineScheduler` 包装、`/oc-session` 命令、生命周期；调用 `core.py`。

单元测试（pytest，`tests/`，直接 import `core.py`，无需安装 AstrBot）：

1. **端点匹配**：2.2 节全部正例 + 4.2 节反例；主机点后缀子域规则；路径边界（`/zenx` 不匹配 `/zen`、`/zen` 全等匹配、`/inference/go/...` 匹配）。
2. **会话推导**：UUID → `ses_`+32 hex 确定性向量（含文档示例向量）；非 UUID 回退哈希；固定值可见 ASCII 校验与降级；缓存上限。
3. **注入行为（真实传输模块 + MockTransport 往返）**：对 `httpx` 与 `httpx2`（`pytest.importorskip` 优雅降级；CI 两者都装）分别验证——写入头齐全、已有主头时镜像头与之一致、个别镜像头已存在时保留、`enable=false` 不注入、镜像开关关闭时只写主头、端点不匹配零副作用、`terminate` 后类方法完全还原。
4. **容错**：注入函数人为抛错时包装器吞异常并记日志，不影响请求返回。
5. **发现机制**：伪造缺失模块/重复模块时去重与跳过正确。

CI（GitHub Actions）：`pip install httpx httpx2 pytest ruff` → `ruff check` → `pytest`。**不安装 AstrBot**（核心逻辑无依赖）。

手动端到端（用户环境）：Anthropic 类型 Provider（`https://opencode.ai/zen/go`）从 400 恢复；OpenAI 兼容类型回归无感；`/oc-session` 展示正确。

静态检查：`ruff check` 通过（遵循 AstrBot 开发规范）。

## 6. 文件与提交计划

交付文件：

| 文件 | 动作 |
|---|---|
| `core.py` | 新增：纯逻辑模块（零 AstrBot 依赖，可独立测试） |
| `main.py` | 完全重写：薄胶水层（插件类、钩子、命令、生命周期） |
| `_conf_schema.json` | 完全重写 |
| `README.md` | 完全重写（安装、四类 Provider 配置指南、配置表、设计说明、FAQ、与上游差异） |
| `metadata.yaml` | 重写（署名、fork 仓库地址、版本 2.0.0、描述更新） |
| `tests/test_core.py` 等 | 新增 |
| `.github/workflows/ci.yml` | 新增（ruff + pytest） |
| `docs/superpowers/specs/2026-10-17-opencode-session-header-refactor-design.md` | 本设计文档 |

提交拆分（信息中文，标注相对原仓库的修改）：

1. `docs: 新增重构设计文档` —— 说明新方案相对上游（仅 OpenAI SDK hook、单一端点）的差异。
2. `refactor!: 重写插件实现为 httpx 多模块统一会话头注入` —— 覆盖 Anthropic/OpenAI/Responses/Gemini 四类 Provider 与 zen/inference 全部端点；`_conf_schema.json` 同步重写。
3. `test: 新增端点匹配、会话推导、注入行为单元测试`。
4. `ci: 添加 GitHub Actions（ruff + pytest）`。
5. `docs: 重写 README 与 metadata 为 Fork 版内容` —— 含与上游差异说明。

（粒度可合并/拆分，以最终计划为准。）

## 7. 构建者决定（默认值，可被用户否决）

- 版本号：`2.0.0`（破坏性重构）。
- `metadata.yaml`：`author: sIleNtot`、`repo:` 指向 fork 地址。
- README 保留一行致谢，说明基于上游 AnzioNiko 原仓库重构（AstrBot 开发规范要求借鉴说明）。
- 不声明 `astrbot_version` 约束（属性探测降级，兼容尽可能广）。
- 无运行时第三方依赖（`httpx` / `httpx2` / `openai` / `anthropic` / `google-genai` 均为 AstrBot 环境自带）；测试依赖 pytest、httpx、httpx2 仅 CI/开发使用，不建 `requirements.txt`。
- 保留命令名 `/oc-session`、日志前缀、插件 ID 不变。

## 8. 用户确认结果（2026-10-17）

用户已确认全部按第 7 节默认执行：署名 `sIleNtot` + fork 地址 + 一行致谢；版本号 2.0.0；添加 GitHub Actions CI。设计文档整体获批，进入实施计划阶段。
