# OpenCode 会话请求头注入插件重构——实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完全重构 AstrBot 插件 `astrbot_plugin_opencode_session_header`：在 httpx 生态层统一注入 `x-opencode-session`，覆盖 Anthropic / OpenAI Chat / OpenAI Responses / Gemini 四类 Provider 与 zen/inference 全部 OpenCode 端点。

**Architecture:** 纯逻辑拆入零依赖的 `core.py`（端点匹配、会话推导、注入引擎、httpx/httpx2 传输钩子），`main.py` 为薄胶水层（AstrBot Star 类、钩子、命令、生命周期）。运行时动态发现 httpx / httpx2 / SDK 内部模块并包装其 `Client.send` / `AsyncClient.send`。

**Tech Stack:** Python ≥3.12（AstrBot 环境）、httpx / httpx2、pytest、ruff、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-10-17-opencode-session-header-refactor-design.md`

## Global Constraints

- 所有注释、日志、提交信息使用中文；日志前缀统一 `[opencode_session_header]`。
- `core.py` 零 AstrBot 依赖、零第三方硬依赖（httpx / httpx2 均为可选导入、缺失跳过）。
- 运行时行为红线：注入路径（`inject_request` 及 `send` 包装器）**任何异常只记日志、绝不抛出**；**不覆盖**已存在的非空主头值；**不动** `User-Agent` 与 `x-astrbot-conversation-id`。
- 会话推导：UUID → `ses_` + 去连字符小写十六进制（32 位）；其它 → `ses_` + `sha256` 前 32 位十六进制；固定值仅允许可见 ASCII `^[\x21-\x7e]+$`（不含空格）。
- 默认配置：`target_hosts=["opencode.ai"]`、`target_path_prefixes=["/zen", "/inference"]`、`header_name="x-opencode-session"`、`mirror_opencode_headers=true`。
- 插件 ID / 目录名 `astrbot_plugin_opencode_session_header` 不变；版本 2.0.0；author `sIleNtot`；repo `https://github.com/sIleNtot/opencode_session_header`。
- `ruff check .` 必须通过（ruff 默认规则集）；`pytest -q` 必须全绿。
- 不新建 `requirements.txt`；测试依赖仅 pytest / httpx / httpx2。
- 不修改 AstrBot 任何文件；不引入持久化存储。

## Review Focus

1. **畸形 / 无 scheme / 相对 URL** —— 匹配器返回 False，注入引擎不抛异常（Task 1、Task 3 各自的测试）。
2. **请求头大小写不敏感** —— `httpx.Headers` 中已存在 `X-Opencode-Session` 时视为已设置，不重复注入（Task 3 测试）。
3. **并发会话不串线** —— 两个并发请求各自携带自己的会话 ID（Task 4 测试）。
4. **send 包装器签名透明** —— 经 `client.request(...)` 带 `follow_redirects` / `auth` 等 kwarg 调用不受影响（Task 4 测试）。
5. **用户配置错误不炸** —— `fixed_session_id` 含 CRLF/空格被拒并降级、`target_hosts` 填完整 URL 被归一化、路径前缀无前导 `/` 被归一化（Task 1、Task 2 测试）。

---

### Task 0: 提交设计文档

**Files:**
- Create（已存在，待提交）: `docs/superpowers/specs/2026-10-17-opencode-session-header-refactor-design.md`

**Interfaces:**
- Consumes: 无
- Produces: 其余所有任务引用的 Spec 文档

- [ ] **Step 1: 确认文档存在**

Run: `git status --short docs/`
Expected: 出现 `?? docs/` （未跟踪）

- [ ] **Step 2: 提交**

```bash
git add docs/superpowers/specs/2026-10-17-opencode-session-header-refactor-design.md
git commit -m "docs: 新增重构设计文档（httpx 多模块注入、Desktop 会话头对齐、四类 Provider 覆盖）"
```

---

### Task 1: core 常量与端点匹配

**Files:**
- Create: `core.py`
- Create: `tests/conftest.py`
- Test: `tests/test_matcher.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces（后续任务全部依赖，必须逐字一致）:
  - `DEFAULT_TARGET_HOSTS: tuple[str, ...] = ("opencode.ai",)`
  - `DEFAULT_TARGET_PATH_PREFIXES: tuple[str, ...] = ("/zen", "/inference")`
  - `DEFAULT_HEADER_NAME = "x-opencode-session"`
  - `SESSION_PREFIX = "ses_"`、`SESSION_CACHE_MAX = 4096`
  - `HOOK_MARKER = "_astrbot_opencode_session_hook"`
  - `MIRROR_HEADERS: tuple[tuple[str, str | None], ...]`（`None` 表示取有效会话值）
  - `CURRENT_SESSION: contextvars.ContextVar[str | None]`（名称 `"astrbot_opencode_session"`，default `None`）
  - `LoggerFn = Callable[[str, str], None]`（level 取值 `"info" | "warning" | "error" | "debug"`）
  - `coerce_bool(value: object, default: bool) -> bool`
  - `coerce_str_list(value: object, default: Iterable[str]) -> list[str]`
  - `class EndpointMatcher`：`__init__(self, hosts: Iterable[str] = DEFAULT_TARGET_HOSTS, path_prefixes: Iterable[str] = DEFAULT_TARGET_PATH_PREFIXES)`；`matches(self, url: str) -> bool`；`@property description -> str`

- [ ] **Step 0: 准备开发环境（一次性）**

```bash
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip httpx httpx2 pytest ruff
```

Expected: 全部安装成功（`.venv/` 已被 .gitignore 忽略）。

- [ ] **Step 1: 写失败的测试——节点头与路径匹配**

`tests/conftest.py`（所有测试用；`import core` 依赖此文件）：

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

`tests/test_matcher.py` 关键断言：

```python
import pytest
from core import DEFAULT_TARGET_HOSTS, DEFAULT_TARGET_PATH_PREFIXES, EndpointMatcher

OK_URLS = [
    "https://opencode.ai/zen/go/v1/messages",
    "https://opencode.ai/zen/go/v1/chat/completions",
    "https://opencode.ai/zen/go/v1/responses",
    "https://opencode.ai/zen/v1/models",
    "https://opencode.ai/inference/go/anthropic/v1/messages",
    "https://opencode.ai/inference/go/openai/v1/chat/completions",
    "https://opencode.ai/inference/go/google/v1beta/models/gemini-4-argon-contributor:generateContent",
    "https://opencode.ai/zen",           # 前缀全等
    "https://api.opencode.ai/zen/go",    # 子域
]
BAD_URLS = [
    "https://api.openai.com/v1/chat/completions",
    "https://opencode.ai/console/billing",   # 路径不在前缀内
    "https://opencode.ai/zenx/go",           # 前缀边界
    "https://example.com/zen/go/v1/messages",
    "", "not a url", "/relative/path",
]

@pytest.mark.parametrize("url", OK_URLS)
def test_matches_ok(url):
    assert EndpointMatcher(DEFAULT_TARGET_HOSTS, DEFAULT_TARGET_PATH_PREFIXES).matches(url) is True

@pytest.mark.parametrize("url", BAD_URLS)
def test_matches_bad(url):
    assert EndpointMatcher(DEFAULT_TARGET_HOSTS, DEFAULT_TARGET_PATH_PREFIXES).matches(url) is False

def test_empty_prefixes_match_any_path():
    assert EndpointMatcher(("opencode.ai",), ()).matches("https://opencode.ai/custom/path") is True

def test_user_may_paste_full_url_as_host():
    assert EndpointMatcher(("https://opencode.ai/zen/go",), ("/v1",)).matches(
        "https://opencode.ai/zen/go/v1/models"
    ) is True

def test_prefix_without_leading_slash_normalized():
    assert EndpointMatcher(("opencode.ai",), ("zen",)).matches("https://opencode.ai/zen/go") is True
```

- [ ] **Step 2: 写失败的测试——配置工具**

`tests/test_config.py` 关键断言：

```python
from core import coerce_bool, coerce_str_list

def test_coerce_bool_variants():
    assert coerce_bool(True, False) is True
    assert coerce_bool("true", False) is True
    assert coerce_bool("on", False) is True
    assert coerce_bool("0", True) is False
    assert coerce_bool(None, True) is True          # None 回退默认值
    assert coerce_bool("", False) is False

def test_coerce_str_list_variants():
    assert coerce_str_list(["a", " b "], ("d",)) == ["a", "b"]
    assert coerce_str_list("a,b", ("d",)) == ["a", "b"]
    assert coerce_str_list("", ("d",)) == ["d"]        # 空字符串回退默认
    assert coerce_str_list([], ("d",)) == []           # 显式空列表保持空（= 任意路径语义）
    assert coerce_str_list(None, ("d",)) == ["d"]
    assert coerce_str_list(123, ("d",)) == ["d"]
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_matcher.py tests/test_config.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'core'`）

- [ ] **Step 4: 实现 `core.py`（常量 + 工具 + 匹配器）**

创建 `core.py`，内容包括：模块 docstring（一句话：零 AstrBot 依赖的核心逻辑）、`from __future__ import annotations`、本任务 Interfaces 中全部常量、`coerce_bool` / `coerce_str_list`、`EndpointMatcher`。

实现要点（决定行为，非逐字代码）：
- `coerce_bool`：`None` → default；`bool` 原样；`str` → 小写去空白后 in `("1","true","yes","on")`；其它 → `bool(value)`。
- `coerce_str_list`：`None`/其它类型 → `list(default)`；`str` → 按 `,` 切分、strip、去空项，结果为空则 `list(default)`；list/tuple → 对每项 `str(x).strip()`、去空项（显式空列表保持 `[]`）。
- `EndpointMatcher.__init__`：主机归一化——strip、小写、去尾部 `.`；若条目含 `"://"` 或 `/`，取 `urlsplit(entry).hostname`；空条目丢弃。路径前缀归一化——strip；无前导 `/` 则补上；`rstrip("/")`；`"/"` 单独保留（代表全部路径）。
- `matches`：`urlsplit` 异常/无 hostname → False；主机匹配 `host == h or host.endswith("." + h)`；`path = parts.path or "/"`；前缀逐一判断 `path == p or path.startswith(p + "/")`；`p == "/"` 直接 True；无前缀 → 任意路径。
- `description`：返回如 `opencode.ai | /zen, /inference`（前缀为空时显示 `任意路径`）。

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_matcher.py tests/test_config.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add core.py tests/conftest.py tests/test_matcher.py tests/test_config.py
git commit -m "feat: core 端点匹配与配置工具（覆盖 zen/inference、子域与路径边界；相对上游仅单端点的扩展）"
```

---

### Task 2: 会话 ID 推导与兜底解析

**Files:**
- Modify: `core.py`（追加）
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: Task 1 的 `SESSION_PREFIX`、`SESSION_CACHE_MAX`、`CURRENT_SESSION`
- Produces:
  - `is_visible_ascii(value: str) -> bool`
  - `sanitize_session_value(value: object) -> str | None`
  - `derive_session_id(source: str) -> str`
  - `new_random_session_id() -> str`
  - `class SessionResolver`：`__init__(self, *, fixed_session: str | None = None, random_session: str | None = None, cache_max: int = SESSION_CACHE_MAX)`；`resolve(self, source: str | None) -> tuple[str, str]`（第二项为来源标签 `"conversation" | "fixed" | "random"`）

- [ ] **Step 1: 写失败的测试**

`tests/test_session.py` 关键断言：

```python
import re
from core import SessionResolver, derive_session_id, new_random_session_id, sanitize_session_value

def test_derive_from_uuid_is_deterministic_vector():
    assert derive_session_id("550e8400-e29b-41d4-a716-446655440000") == "ses_550e8400e29b41d4a716446655440000"
    # 去连字符 / 大写输入均归一到同一值
    assert derive_session_id("550E8400E29B41D4A716446655440000") == "ses_550e8400e29b41d4a716446655440000"

def test_derive_from_non_uuid_is_stable_hash():
    value = derive_session_id("aiocqhttp:group:12345")
    assert re.fullmatch(r"ses_[0-9a-f]{32}", value)
    assert derive_session_id("aiocqhttp:group:12345") == value

def test_sanitize_rejects_crlf_and_space():
    assert sanitize_session_value("abc-123") == "abc-123"
    assert sanitize_session_value(" a b ") is None      # 含空格
    assert sanitize_session_value("a\r\nX: y") is None  # CRLF
    assert sanitize_session_value("") is None
    assert sanitize_session_value(None) is None

def test_resolver_precedence_and_origins():
    r = SessionResolver(fixed_session="ses_" + "f" * 32, random_session="ses_" + "r" * 32)
    assert r.resolve("550e8400-e29b-41d4-a716-446655440000") == ("ses_550e8400e29b41d4a716446655440000", "conversation")
    assert r.resolve(None) == ("ses_" + "f" * 32, "fixed")
    plain = SessionResolver(random_session="ses_" + "r" * 32)
    assert plain.resolve(None) == ("ses_" + "r" * 32, "random")

def test_resolver_cache_cap():
    r = SessionResolver(random_session="ses_" + "r" * 32, cache_max=2)
    for i in range(5):
        r.resolve(f"source-{i}")
    assert len(r._cache) == 2

def test_random_session_format():
    assert re.fullmatch(r"ses_[0-9a-f]{32}", new_random_session_id())
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_session.py -q`
Expected: FAIL（`ImportError: cannot import name ...`）

- [ ] **Step 3: 实现**

`derive_session_id`：strip 输入；`uuid.UUID(value)` 成功 → `SESSION_PREFIX + parsed.hex`；否则 `SESSION_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]`。
`new_random_session_id`：`SESSION_PREFIX + uuid.uuid4().hex`。
`is_visible_ascii`：非空且所有字符 `0x21 <= ord(c) <= 0x7E`。
`sanitize_session_value`：`value is None` → None；`str(value).strip()`；通过 `is_visible_ascii` 则返回值，否则 None。
`SessionResolver.resolve`：`source` 非空 → 查 `_cache`（命中直接返回），未命中时 `derive_session_id` 并在未满 `cache_max` 时写入，标签 `"conversation"`；`source` 为空 → 有 `fixed_session`（构造时已 sanitize，非法则 None）返回 `("fixed")`，否则返回 random（构造时 `sanitize(random_session)`，None 则 `new_random_session_id()`），标签 `"random"`。

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_session.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add core.py tests/test_session.py
git commit -m "feat: ses_ 会话 ID 推导与兜底解析（对齐 OpenCode Desktop 格式，替换上游 UUID v4 方案）"
```

---

### Task 3: 注入引擎（头计划 + 容错注入）

**Files:**
- Modify: `core.py`（追加）
- Test: `tests/test_injection.py`

**Interfaces:**
- Consumes: Task 1（`EndpointMatcher`、`MIRROR_HEADERS`、`DEFAULT_HEADER_NAME`、`LoggerFn`）、Task 2（`SessionResolver`、`CURRENT_SESSION`）
- Produces:
  - `plan_header_injection(current: Mapping[str, str], *, header_name: str, session: str, mirror: bool) -> dict[str, str]`
  - `class InjectionEngine`：`__init__(self, *, enable: bool, matcher: EndpointMatcher, resolver: SessionResolver, header_name: str = DEFAULT_HEADER_NAME, mirror: bool = True, log_injection: bool = True, log_skip: bool = True, logger: LoggerFn | None = None)`；`inject_request(self, request: object) -> None`；公开属性 `enable` / `matcher` / `header_name`

- [ ] **Step 1: 写失败的测试（头计划）**

`tests/test_injection.py` 关键断言（假请求对象 `FakeRequest`：`url` 为 str、`headers` 为 dict）：

```python
import httpx
from core import CURRENT_SESSION, EndpointMatcher, InjectionEngine, SessionResolver, plan_header_injection

SESSION = "ses_" + "a" * 32
HOSTS, PREFIXES = ("opencode.ai",), ("/zen", "/inference")

def test_plan_writes_primary_and_mirrors():
    plan = plan_header_injection({}, header_name="x-opencode-session", session=SESSION, mirror=True)
    assert plan == {
        "x-opencode-session": SESSION,
        "x-opencode-session-id": SESSION,
        "x-session-affinity": SESSION,
        "X-Session-Id": SESSION,
        "x-opencode-client": "astrbot",
    }

def test_plan_keeps_existing_primary_and_mirrors_its_value():
    plan = plan_header_injection(
        {"x-opencode-session": "custom-value"}, header_name="x-opencode-session", session=SESSION, mirror=True)
    assert "x-opencode-session" not in plan
    assert plan["x-session-affinity"] == "custom-value"
    assert plan["x-opencode-client"] == "astrbot"

def test_plan_keeps_existing_individual_mirror():
    plan = plan_header_injection(
        {"X-Session-Id": "user-set"}, header_name="x-opencode-session", session=SESSION, mirror=True)
    assert "X-Session-Id" not in plan
    assert plan["x-opencode-session-id"] == SESSION

def test_plan_mirror_off_and_empty_primary():
    assert plan_header_injection({"x-opencode-session": ""}, header_name="x-opencode-session", session=SESSION, mirror=False) == {"x-opencode-session": SESSION}
    assert plan_header_injection({"x-opencode-session": SESSION}, header_name="x-opencode-session", session=SESSION, mirror=False) == {}
```

引擎行为断言：

```python
def test_engine_injects_on_match_and_sets_all_headers():
    # FakeRequest("https://opencode.ai/zen/go/v1/messages")；CURRENT_SESSION.set(SESSION)
    # 调用 engine.inject_request(req) 后：req.headers["x-opencode-session"] == SESSION（5 个键齐全）

def test_engine_skips_non_matching_endpoint():
    # FakeRequest("https://api.openai.com/v1/chat/completions") → headers 保持 {}

def test_engine_disabled_does_not_inject():
    # enable=False → headers 保持 {}

def test_engine_headers_are_case_insensitive_with_httpx_headers():
    # req.headers = httpx.Headers({"X-Opencode-Session": "abc"})
    # inject → req.headers["x-opencode-session"] == "abc"，且镜像头 == "abc"，未覆盖为 SESSION

def test_engine_swallows_injection_errors(caplog):
    # engine.resolver 替换为 resolve() 抛 RuntimeError 的对象 → inject_request 不抛，logger 收到 error

def test_engine_uses_fallback_when_no_context():
    # CURRENT_SESSION 未设置、resolver(random_session=...) → 注入 random 会话值
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_injection.py -q`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 实现**

`plan_header_injection`（纯函数，不修改入参）：
1. `existing = current.get(header_name)`；`existing_ok = isinstance(existing, str) and existing.strip() != ""`。
2. `effective = existing.strip() if existing_ok else session`（strip 仅用于取值）。
3. 主头缺失/为空 → `to_set[header_name] = session`。
4. `mirror=True` 时遍历 `MIRROR_HEADERS`：跳过与 `header_name` 同名（大小写不敏感）者；`current.get(name)` 非空字符串 → 跳过；否则 `to_set[name] = fixed if fixed is not None else effective`。
5. 返回 `to_set`。

`InjectionEngine.inject_request`（整个函数体包在 `try/except Exception`，异常仅 `_log("error", ...)`）：
1. `url = str(getattr(request, "url", "") or "")`。
2. `not self.enable` → `_log_skip("插件已禁用", url)` 返回。
3. `not self.matcher.matches(url)` → `_log_skip("端点不匹配", url)` 返回。
4. `session = CURRENT_SESSION.get()`；为空 → `session, origin = self.resolver.resolve(None)`，否则 `origin = "conversation"`。
5. `to_set = plan_header_injection(request.headers, header_name=self.header_name, session=session, mirror=self.mirror)`；逐项写入 `request.headers[name] = value`。
6. `log_injection` 时记录：`已注入 {header_name}={写入或保留的主头值} (来源={origin}, url={url})`。
7. `_log_skip` / `_log` 内部按 `log_skip` / `log_injection` 开关与 `logger` 可空处理。

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_injection.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add core.py tests/test_injection.py
git commit -m "feat: 注入引擎（保留策略与镜像头一致性、全量容错；相对上游新增 Desktop 同款镜像头）"
```

---

### Task 4: httpx/httpx2 传输钩子

**Files:**
- Modify: `core.py`（追加）
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: Task 3 的 `InjectionEngine.inject_request`；Task 1 的 `HOOK_MARKER`
- Produces:
  - `discover_transport_modules() -> list[tuple[str, types.ModuleType]]`（标签如 `"httpx"`、`"httpx2"`、`"openai._base_client.httpx2"`）
  - `InjectionEngine.install_hooks(self) -> list[str]`（返回已挂载标签，幂等）
  - `InjectionEngine.uninstall_hooks(self) -> None`（逆序还原）
  - `InjectionEngine.hooked_labels: list[str]`（property）

- [ ] **Step 1: 写失败的测试——发现机制**

`tests/test_hooks.py` 关键断言：

```python
import sys, types
import core

def test_discovery_finds_fake_httpx2(monkeypatch):
    fake = types.ModuleType("httpx2")
    fake.Client = type("Client", (), {})
    fake.AsyncClient = type("AsyncClient", (), {})
    monkeypatch.setitem(sys.modules, "httpx2", fake)
    found = dict(core.discover_transport_modules())
    assert found.get("httpx2") is fake

def test_discovery_skips_none_and_non_transport_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", None)  # 导入必然失败
    empty = types.ModuleType("openai._base_client")   # 无 Client/AsyncClient 属性
    monkeypatch.setitem(sys.modules, "openai._base_client", empty)
    labels = [label for label, _ in core.discover_transport_modules()]
    assert "httpx2" not in labels
    assert "openai._base_client.httpx" not in labels

def test_discovery_dedupes_same_module_object(monkeypatch):
    import httpx
    base = types.ModuleType("openai._base_client")
    base.httpx = httpx          # 与顶层 httpx 同一对象
    base.httpx2 = None
    monkeypatch.setitem(sys.modules, "openai._base_client", base)
    labels = [label for label, _ in core.discover_transport_modules()]
    assert labels.count("httpx") <= 1
    assert "openai._base_client.httpx" not in labels
```

- [ ] **Step 2: 写失败的测试——包装、往返与还原**

```python
import asyncio, pytest

SESSION = "ses_" + "b" * 32

def _make_engine(**kw):
    # 返回 InjectionEngine(enable=True, matcher=EndpointMatcher(("opencode.ai",), ("/zen", "/inference")),
    #   resolver=SessionResolver(random_session="ses_" + "r" * 32),
    #   log_injection=False, log_skip=False, **kw)

@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_send_roundtrip_injects_and_preserves_kwargs(module_name):
    mod = pytest.importorskip(module_name)
    mock_transport = getattr(mod, "MockTransport", None)
    if mock_transport is None:
        pytest.skip(f"{module_name} 无 MockTransport")
    engine = _make_engine()
    engine.install_hooks()
    try:
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return mod.Response(200, json={})

        async def main():
            core.CURRENT_SESSION.set(SESSION)
            async with mod.AsyncClient(transport=mock_transport(handler)) as client:
                # 经 client.request 携带 follow_redirects（send 透明转发 kwarg）
                await client.request("POST", "https://opencode.ai/zen/go/v1/chat/completions",
                                     json={}, follow_redirects=True)
        asyncio.run(main())
    finally:
        engine.uninstall_hooks()

    assert seen["x-opencode-session"] == SESSION
    assert seen["x-session-affinity"] == SESSION
    assert seen["x-opencode-client"] == "astrbot"

@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_send_roundtrip_ignores_other_hosts(module_name):
    # 同上，URL 换 https://api.openai.com/v1/chat/completions → "x-opencode-session" not in seen

def test_install_uninstall_restores_original_send():
    import httpx
    saved = httpx.AsyncClient.send
    engine = _make_engine()
    engine.install_hooks()
    assert httpx.AsyncClient.send is not saved
    engine.install_hooks()                      # 二次安装幂等
    assert httpx.AsyncClient.send is not saved  # 未叠加第二层
    engine.uninstall_hooks()
    assert httpx.AsyncClient.send is saved

def test_concurrent_sessions_do_not_leak():
    # 两个 asyncio.gather 任务各自 CURRENT_SESSION.set(不同会话) 后经 MockTransport 发请求
    # 断言 handler 收到的两个会话值分别正确（集合 == {SESSION_A, SESSION_B}）
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_hooks.py -q`
Expected: FAIL（`AttributeError: install_hooks`）

- [ ] **Step 4: 实现**

`discover_transport_modules`：候选顺序 —— 顶层 `httpx`、顶层 `httpx2`、`openai._base_client` 的 `httpx`/`httpx2` 属性、`anthropic._base_client` 的 `httpx`/`httpx2` 属性。用 `importlib.import_module` + `try/except Exception` 逐个尝试；模块为 `None`、非模块、或同时缺少 `Client` 与 `AsyncClient` 属性 → 跳过；按模块对象 `id` 去重（先到先得）；返回 `[(label, module)]`。

`install_hooks`：
- 对 `discover_transport_modules()` 中每个模块，遍历 `("AsyncClient", "Client")`；类对象按 `id` 去重（本方法内 `seen` 集合，跨模块共享）。
- `original = getattr(cls, "send", None)` 不可调用 → 跳过。
- 已被本插件标记（`getattr(original, HOOK_MARKER, False)`）→ 记录 `(cls, original, original)` 并计入标签（不再包装）。
- 否则按 `inspect.iscoroutinefunction(original)` 生成包装器（代码见下，**逐字实现**），`setattr(cls, "send", wrapped)`，记录 `(cls, original, wrapped)`并追加标签 `f"{label}.{cls_name}.send"`。
- 返回标签列表；同时保存到 `self._hooked` 与 `self._hooked_labels`。

包装器（关键代码，不得改动语义）：

```python
def _make_send_wrapper(self, original, *, is_async: bool):
    engine = self
    if is_async:
        @functools.wraps(original)
        async def wrapped(client_self, request, *args, **kwargs):
            engine.inject_request(request)
            return await original(client_self, request, *args, **kwargs)
    else:
        @functools.wraps(original)
        def wrapped(client_self, request, *args, **kwargs):
            engine.inject_request(request)
            return original(client_self, request, *args, **kwargs)
    setattr(wrapped, HOOK_MARKER, True)
    return wrapped
```

`uninstall_hooks`：逆序遍历 `self._hooked`，`getattr(cls, "send", None) is wrapped` 时 `setattr(cls, "send", original)`；清空记录与标签。

- [ ] **Step 5: 运行确认通过（含双传输模块往返）**

Run: `.venv\Scripts\python -m pytest tests/test_hooks.py -q`
Expected: PASS（本地未装 httpx2 时该参数化用例 skip）

- [ ] **Step 6: 提交**

```bash
git add core.py tests/test_hooks.py
git commit -m "feat: httpx/httpx2 传输钩子（多模块发现、幂等安装与还原；相对上游从 SDK 内部 hook 迁移到传输层）"
```

---

### Task 5: main.py 与 _conf_schema.json 重写

**Files:**
- Modify: `main.py`（整体替换）
- Modify: `_conf_schema.json`（整体替换）
- Test: `tests/test_main_smoke.py`

**Interfaces:**
- Consumes: core 全部公开接口（Tasks 1-4）
- Produces: 插件类 `OpenCodeSessionHeaderPlugin(Star)`；配置键 `enable` / `target_hosts` / `target_path_prefixes` / `header_name` / `mirror_opencode_headers` / `missing_session_mode` / `fixed_session_id` / `log_injection` / `log_skip`

- [ ] **Step 1: 写失败的测试——插件冒烟（stub AstrBot）**

`tests/test_main_smoke.py`：以 `types.ModuleType` 伪造 `astrbot.api` / `astrbot.api.event` / `astrbot.api.provider` / `astrbot.api.star` 四个模块（`logger` 用标准 logging；`AstrBotConfig` = `dict`；`filter.on_llm_request/command/permission_type` 均为返回恒等装饰器的静态方法；`PermissionType.ADMIN`；`Star` 基类 `__init__(self, context=None)`），再以包路径加载 main：

```python
import importlib.util
import sys

MAIN = ROOT / "main.py"
PKG = "data.plugins.astrbot_plugin_opencode_session_header"

def _load_main(tmp_path):  # 用 monkeypatch.setitem(sys.modules, ...) 注入 stub 后调用
    # 1) 伪造 astrbot 各模块并 monkeypatch.setitem
    # 2) 注册父包模块：sys.modules["data"]、sys.modules["data.plugins"]、sys.modules[PKG]
    #    （parents 用 types.ModuleType 且 PKG.__path__ = [str(ROOT)]）
    # 3) spec = importlib.util.spec_from_file_location(PKG + ".main", MAIN, submodule_search_locations=[str(ROOT)])
    #    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
    #    spec.loader.exec_module(module); return module

def test_plugin_smoke(monkeypatch):
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(context=None, config={})
    assert any(label.startswith("httpx") for label in plugin._engine.hooked_labels)
    asyncio.run(plugin.terminate())

def test_on_llm_request_prefers_conversation_cid(monkeypatch):
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(context=None, config={})
    try:
        class _Conv:  cid = "550e8400-e29b-41d4-a716-446655440000"
        class _Req:   conversation = _Conv(); session_id = "legacy"
        class _Event: unified_msg_origin = "platform:group:1"
        asyncio.run(plugin.on_llm_request(_Event(), _Req()))
        assert core.CURRENT_SESSION.get() == "ses_550e8400e29b41d4a716446655440000"
    finally:
        asyncio.run(plugin.terminate())
```

def test_fixed_mode_invalid_falls_back(monkeypatch):
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(
        context=None, config={"missing_session_mode": "fixed", "fixed_session_id": ""})
    try:
        session, origin = plugin._resolver.resolve(None)
        assert origin == "random"  # 无效固定值 → 降级随机
    finally:
        asyncio.run(plugin.terminate())

（`_load_main` 的 stub 细节由实现者补全；判断标准是测试能在无 AstrBot 的环境下真实执行 main.py 的类定义与实例化。）

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_main_smoke.py -q`
Expected: FAIL（现有 main.py 为上游实现，不导入 core，无 `_engine` 属性）

- [ ] **Step 3: 重写 `main.py`**

结构与实现要点：
- 顶部 `try: from . import core / except ImportError: import core`（兼容包导入与脚本式导入）。
- AstrBot 导入：`from astrbot.api import AstrBotConfig, logger`；`from astrbot.api.event import AstrMessageEvent, filter`；`from astrbot.api.provider import ProviderRequest`；`from astrbot.api.star import Context, Star`。
- `_emit(self, level: str, message: str)`：`getattr(logger, level, logger.info)(f"[opencode_session_header] {message}")`，整体 try/except 吞掉日志错误。
- `__init__`：读取配置（`core.coerce_bool` / `core.coerce_str_list` / `core.sanitize_session_value`）；校验 `missing_session_mode`（非法或 fixed 值无效 → 警告并降级 random）；构建 `self._matcher`、`self._resolver`、`self._engine`（logger 传 `self._emit`）；安装调度器兜底钩子与传输钩子并记录结果日志。
- `_install_scheduler_hook()`：`try: from astrbot.core.pipeline.scheduler import PipelineScheduler`；取 `execute`（既有标记则取 `_astrbot_opencode_session_header_original`）；包装后 `finally: core.CURRENT_SESSION.reset(token)`，事件来源取 `unified_msg_origin`；保存 `(cls, "execute", original, wrapped)` 记录；失败仅警告返回 False。
- `@filter.on_llm_request()` `on_llm_request(event, req)`：来源解析顺序 `req.conversation.cid` → `event.unified_msg_origin` → `req.session_id`；`session, _ = self._resolver.resolve(source)`；`core.CURRENT_SESSION.set(session)`。
- `@filter.command("oc-session")` + `@filter.permission_type(filter.PermissionType.ADMIN)`：尝试 `self.context.conversation_manager.get_curr_conversation_id(umo)`（存在性与 awaitable 探测，失败忽略）取 cid；输出行：会话来源、来源值、`{header_name}: {session}`、匹配规则（`self._matcher.description`）、启用状态与 `hooked_labels`；`yield event.plain_result("\n".join(lines))`。
- `async def terminate(self)`：`self._engine.uninstall_hooks()`；还原调度器钩子（校验 `getattr(cls, name) is wrapped`）；`_emit("info", "插件已卸载/禁用。")`。
- 模块级 docstring 概述行为与配置（替代上游 docstring，不保留上游文字）。

- [ ] **Step 4: 重写 `_conf_schema.json`**

键与元数据（`description` / `hint` 用中文重写，逐键）：

| 键 | type | default | 要点 |
|---|---|---|---|
| `enable` | bool | true | obvious_hint |
| `target_hosts` | list | `["opencode.ai"]` | hint 说明子域规则与可填完整 URL |
| `target_path_prefixes` | list | `["/zen", "/inference"]` | hint 说明空列表=全部路径 |
| `header_name` | string | `"x-opencode-session"` | |
| `mirror_opencode_headers` | bool | true | hint 说明附加 Desktop 同款镜像头 |
| `missing_session_mode` | string | `"random"` | options `["random","fixed"]`，obvious_hint |
| `fixed_session_id` | string | `""` | hint 说明可见 ASCII 限制 |
| `log_injection` | bool | true | |
| `log_skip` | bool | true | |

- [ ] **Step 5: 验证语法与 schema**

Run:
```bash
.venv\Scripts\python -m py_compile main.py
.venv\Scripts\python -c "import json; json.load(open('_conf_schema.json', encoding='utf-8')); print('schema ok')"
.venv\Scripts\python -m pytest tests/test_main_smoke.py -q
```
Expected: 三条全部通过（冒烟测试 PASS）

- [ ] **Step 6: 提交**

```bash
git add main.py _conf_schema.json tests/test_main_smoke.py
git commit -m "refactor!: main.py 重写为薄胶水层并重写 _conf_schema.json（移除上游单端点配置项与 SDK 内部 hook）"
```

---

### Task 6: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Tasks 1-5 的测试
- Produces: CI 门禁（ruff + pytest）

- [ ] **Step 1: 本地全量验证先通过**

Run:
```bash
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q
```
Expected: 0 error / 全部 PASS（如有失败，修复后再继续）

- [ ] **Step 2: 创建 `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dev dependencies
        run: pip install httpx httpx2 pytest ruff
      - name: Lint
        run: ruff check .
      - name: Test
        run: pytest -q
```

- [ ] **Step 3: 提交**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: 添加 GitHub Actions（ruff + pytest，httpx/httpx2 双传输测试）"
```

---

### Task 7: README 与 metadata 重写

**Files:**
- Modify: `README.md`（整体替换）
- Modify: `metadata.yaml`（整体替换）

**Interfaces:**
- Consumes: 全部已完成功能（用于文档描述）
- Produces: 面向用户的发行文档

- [ ] **Step 1: 重写 `metadata.yaml`**

```yaml
name: astrbot_plugin_opencode_session_header
display_name: OpenCode 会话请求头注入
short_desc: 为 OpenCode 请求自动注入 x-opencode-session，覆盖全部 Go 模型与四类 Provider。
author: sIleNtot
description: 为指向 OpenCode（Zen / Go）端点的 LLM 请求自动注入 x-opencode-session 会话请求头。覆盖 Anthropic Compatible、OpenAI Compatible、OpenAI Responses、Gemini 四类 Provider 及 zen/inference 全部端点；会话 ID 与 OpenCode Desktop 保持一致（ses_ 格式，按 AstrBot 对话稳定推导）。已存在的会话头不会被覆盖，非 OpenCode 端点不受影响。
version: 2.0.0
repo: https://github.com/sIleNtot/opencode_session_header
```

- [ ] **Step 2: 重写 `README.md`**

章节结构与关键内容（全部原创中文文案，不保留上游句子）：
1. **标题与一句话简介** + 一句「本仓库为 Fork，重构自上游 AnzioNiko/…」，指向本仓库地址。
2. **功能**：四类 Provider 覆盖表（Anthropic Compatible / OpenAI Compatible / OpenAI Responses / Gemini Compatible → 是否支持：全部支持）；四种 API 形态说明；不变式（已有头不覆盖、非目标端点零影响、UA 不改写）。
3. **安装**：放入 `data/plugins/astrbot_plugin_opencode_session_header` 或插件市场安装。
4. **快速配置**（关键表，逐项给出）：

   | Provider 类型 | 示例 API 地址（填写值） | 最终请求路径 |
   |---|---|---|
   | Anthropic Compatible | `https://opencode.ai/zen/go` | `/zen/go/v1/messages` |
   | OpenAI Compatible | `https://opencode.ai/zen/go/v1` | `/zen/go/v1/chat/completions` |
   | OpenAI Responses | `https://opencode.ai/zen/go/v1` | `/zen/go/v1/responses` |
   | Gemini Compatible | `https://opencode.ai/inference/go/google` | `/inference/go/google/v1beta/models/...` |
   | Anthropic（直连端点） | `https://opencode.ai/inference/go/anthropic` | `/inference/go/anthropic/v1/messages` |
   | OpenAI（直连端点） | `https://opencode.ai/inference/go/openai/v1` | `/inference/go/openai/v1/...` |

5. **配置表**：与 `_conf_schema.json` 九项一致（键、类型、默认值、说明），并说明 `random`/`fixed` 兜底语义。
6. **会话 ID 设计**：`ses_` + 对话 UUID 十六进制；跨重启稳定；对话重置即换新；与 OpenCode Desktop 的对应关系（不覆盖用户自配值；镜像头集说明）。
7. **命令**：`/oc-session`（管理员）。
8. **FAQ**：`400 MissingSessionID` 的原因与排查；为什么得用这个插件；为什么低版本 AstrBot 不受支持（属性探测降级）。
9. **隐私**：不发起额外网络请求、不落盘、仅内存缓冲会话映射。
10. **开发与测试**：`.venv` 搭建、`ruff check .`、`pytest -q`；CI 说明。
11. **与上游差异**（列表形式）：传输层 hook 取代 SDK 内部 hook；单端点 → host+路径前缀；UUID v4 → `ses_` 会话 ID；镜像头；`core.py` 拆分；配置项变更；致谢一行。

- [ ] **Step 3: 验证**

Run:
```bash
.venv\Scripts\python -c "import yaml, pathlib; d=yaml.safe_load(pathlib.Path('metadata.yaml').read_text(encoding='utf-8')); assert {'name','author','description','version','repo','display_name','short_desc'} <= set(d); print('metadata ok')"
```
Expected: `metadata ok`（本机无 yaml 时改用逐项目视核对 name/author/description/version/repo 键存在）

- [ ] **Step 4: 提交**

```bash
git add README.md metadata.yaml
git commit -m "docs: README 与 metadata 重写为 Fork 版（四类 Provider 指南、会话设计说明、与上游差异；2.0.0）"
```

---

## 完成标准

- `ruff check .` 0 错误、`pytest -q` 全绿（含 httpx/httpx2 往返）。
- `git log` 呈现 8 个提交，逐一描述相对上游的改动。
- 手动端到端（用户在 AstrBot 中）：Anthropic 类型 Provider（`https://opencode.ai/zen/go`）从 400 恢复；OpenAI 兼容类型回归无感；`/oc-session` 输出正确。
