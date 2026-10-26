"""零 AstrBot 依赖的核心逻辑：端点匹配、配置工具与会话头 / 认证头注入基础设施。"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import importlib
import inspect
import types
import uuid
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# 常量 / 默认值（与 _conf_schema.json 保持一致）
# ---------------------------------------------------------------------------

# 默认目标主机（含其点后缀子域）与目标路径前缀（空列表 = 任意路径）
DEFAULT_TARGET_HOSTS: tuple[str, ...] = ("opencode.ai",)
DEFAULT_TARGET_PATH_PREFIXES: tuple[str, ...] = ("/zen", "/inference")

# 主请求头名（可被配置覆盖）
DEFAULT_HEADER_NAME = "x-opencode-session"

# 会话 ID 前缀（对齐 OpenCode Desktop 的 ses_ 格式）与进程内推导缓存上限
SESSION_PREFIX = "ses_"
SESSION_CACHE_MAX = 4096

# 传输钩子标记：已包装的 send 方法凭此识别，保证安装幂等、可完整还原
HOOK_MARKER = "_astrbot_opencode_session_hook"

# Desktop 同款镜像头集；None 表示取「有效会话值」，字符串为固定值
MIRROR_HEADERS: tuple[tuple[str, str | None], ...] = (
    ("x-opencode-session-id", None),
    ("x-session-affinity", None),
    ("X-Session-Id", None),
    ("x-opencode-client", "astrbot"),
)

# 当前请求的会话 ID：ContextVar 随任务链传播，天然隔离并发对话
CURRENT_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "astrbot_opencode_session", default=None
)

# 日志函数签名：level 取值 "info" | "warning" | "error" | "debug"
LoggerFn = Callable[[str, str], None]

# 插件所有日志统一前缀，便于在 AstrBot 日志中检索本插件输出
LOG_PREFIX = "[opencode_session_header]"


# ---------------------------------------------------------------------------
# 配置读取工具
# ---------------------------------------------------------------------------
def coerce_bool(value: object, default: bool) -> bool:
    """把配置值强制转换为布尔：None 回退默认值，字符串按常见真值表判断。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def coerce_str_list(value: object, default: Iterable[str]) -> list[str]:
    """把配置值强制转换为字符串列表。

    - ``None`` / 其它类型 → 默认值；
    - 字符串 → 按 ``,`` 切分，去空白与空项，全空则回退默认值；
    - list / tuple → 逐项 ``str(x).strip()`` 并去空项（显式空列表保持 ``[]``，
      语义为「任意路径」）。
    """
    if value is None:
        return list(default)
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        items = [item for item in items if item]
        return items or list(default)
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                result.append(text)
        return result
    return list(default)


# ---------------------------------------------------------------------------
# 端点匹配
# ---------------------------------------------------------------------------
def _split_host_entry(entry: str) -> tuple[str, str]:
    """拆分主机条目为 ``(主机, 粘贴 URL 的路径部分)``。

    - 含 ``://`` 的完整 URL（用户可能整段粘贴 Provider 的 API 基础地址，
      如 ``https://opencode.ai/zen/go``）→ 取 hostname，路径部分交由调用方
      折叠为附加路径前缀，保证粘贴完整 URL 依然可匹配；
    - 其余条目 → 归一化主机名（strip、小写、去尾部 ``.``），无路径；
    - 无 scheme 又含 ``/``、或无法解析 → ``("", "")``，调用方丢弃。
    """
    text = entry.strip()
    if not text:
        return "", ""
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return "", ""
        host = (parts.hostname or "").strip().lower().rstrip(".")
        return host, parts.path
    if "/" in text:
        return "", ""
    return text.lower().rstrip("."), ""


def _normalize_path_prefix(entry: str) -> str:
    """归一化路径前缀：补前导 ``/``、去尾部 ``/``；``/`` 单独保留（全部路径）。"""
    text = entry.strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


class EndpointMatcher:
    """按主机 + 路径前缀判断请求 URL 是否指向目标 OpenCode 端点。"""

    def __init__(
        self,
        hosts: Iterable[str] = DEFAULT_TARGET_HOSTS,
        path_prefixes: Iterable[str] = DEFAULT_TARGET_PATH_PREFIXES,
    ):
        self._hosts: list[str] = []
        self._path_prefixes: list[str] = []

        for entry in path_prefixes:
            prefix = _normalize_path_prefix(str(entry))
            if prefix and prefix not in self._path_prefixes:
                self._path_prefixes.append(prefix)
        for entry in hosts:
            host, base_path = _split_host_entry(str(entry))
            if not host:
                continue
            self._hosts.append(host)
            # 整段粘贴 URL 时其路径一并生效（如 https://opencode.ai/zen/go）；
            # 单独的 "/" 不作为派生前缀，避免退化为「任意路径」
            if base_path:
                prefix = _normalize_path_prefix(base_path)
                if prefix and prefix != "/" and prefix not in self._path_prefixes:
                    self._path_prefixes.append(prefix)

    def host_matches(self, url: str) -> bool:
        """仅判断主机是否为目标主机（完全相等或其点后缀子域）。

        与 ``matches`` 使用完全相同的归一化与子域规则：
        畸形 URL（解析异常 / 无主机 / 相对路径）一律返回 False，绝不抛出。
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = parts.hostname
        if not host:
            return False
        return any(host == h or host.endswith("." + h) for h in self._hosts)

    def matches(self, url: str) -> bool:
        """主机命中（完全相等或点后缀子域）且路径命中任一前缀时为 True。

        主机判定复用 ``host_matches``；畸形 URL 一律返回 False，绝不抛出。
        """
        if not self.host_matches(url):
            return False
        if not self._path_prefixes:
            return True
        # 上方 host_matches 已确认 URL 可解析，这里直接取路径
        path = urlsplit(url).path or "/"
        for prefix in self._path_prefixes:
            # 前缀边界：全等或后随 "/"，避免 /zenx 误配 /zen
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                return True
        return False

    @property
    def description(self) -> str:
        """人类可读的匹配规则，如 ``opencode.ai | /zen, /inference``。"""
        hosts_text = ", ".join(self._hosts)
        prefixes_text = ", ".join(self._path_prefixes) if self._path_prefixes else "任意路径"
        return f"{hosts_text} | {prefixes_text}"


# ---------------------------------------------------------------------------
# 会话 ID 推导与兜底解析
# ---------------------------------------------------------------------------
def is_visible_ascii(value: str) -> bool:
    """非空且所有字符都是可见 ASCII（``0x21``–``0x7E``，不含空格与 CRLF）。"""
    return bool(value) and all(0x21 <= ord(ch) <= 0x7E for ch in value)


def sanitize_session_value(value: object) -> str | None:
    """把配置值清洗为合法会话值：None / 含不可见字符 / 空串一律返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if is_visible_ascii(text) else None


def derive_session_id(source: str) -> str:
    """由会话来源推导 ``ses_`` 会话 ID。

    - 合法 UUID（含去连字符、大写形式）→ ``ses_`` + 32 位小写十六进制；
    - 其它任意字符串 → ``ses_`` + sha256 前 32 位十六进制，稳定可复现。
    """
    text = str(source).strip()
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return SESSION_PREFIX + digest[:32]
    return SESSION_PREFIX + parsed.hex


def new_random_session_id() -> str:
    """生成随机会话 ID：``ses_`` + uuid4 的 32 位小写十六进制。"""
    return SESSION_PREFIX + uuid.uuid4().hex


class SessionResolver:
    """会话 ID 兜底解析器：会话来源 > 固定会话 > 随机会话。

    - 传入非空 ``source`` 时按 ``derive_session_id`` 推导并缓存（缓存上限
      ``cache_max``，写满后不再新增，保证内存有界）；
    - ``source`` 为空时返回构造时清洗过的固定会话（非法即视为未设置），
      否则返回随机会话（``random_session=None`` 时构造即生成）。
    """

    def __init__(
        self,
        *,
        fixed_session: str | None = None,
        random_session: str | None = None,
        cache_max: int = SESSION_CACHE_MAX,
    ):
        self._fixed_session = sanitize_session_value(fixed_session)
        self._random_session = sanitize_session_value(random_session) or new_random_session_id()
        self._cache_max = cache_max
        self._cache: dict[str, str] = {}

    def resolve(self, source: str | None) -> tuple[str, str]:
        """解析本次请求的会话 ID，返回 ``(会话 ID, 来源标签)``。

        标签取值：``"conversation"``（由来源推导）/ ``"fixed"`` / ``"random"``。
        """
        text = source.strip() if source else ""
        if text:
            cached = self._cache.get(text)
            if cached is None:
                cached = derive_session_id(text)
                if len(self._cache) < self._cache_max:
                    self._cache[text] = cached
            return cached, "conversation"
        if self._fixed_session:
            return self._fixed_session, "fixed"
        return self._random_session, "random"


# ---------------------------------------------------------------------------
# 请求头注入：头计划与容错注入引擎
# ---------------------------------------------------------------------------
def plan_header_injection(
    current: Mapping[str, str],
    *,
    header_name: str,
    session: str,
    mirror: bool,
) -> dict[str, str]:
    """计算本次请求需要写入的请求头（纯函数，不修改 ``current``）。

    - 主头 ``header_name`` 缺失或为空 → 写入 ``session``；已有非空值 → 保留；
    - 已有非空主头的实际值（去首尾空白）作为「有效会话值」，用于填充镜像头，
      保证各镜像头与主头一致，不覆盖任何已存在的非空镜像头；
    - ``mirror=True`` 时按 ``MIRROR_HEADERS`` 补全镜像头：固定值项写固定值，
      ``None`` 项写有效会话值；与主头同名（大小写不敏感）的镜像项跳过。
    """
    existing = current.get(header_name)
    existing_ok = isinstance(existing, str) and existing.strip() != ""
    effective = existing.strip() if existing_ok else session

    to_set: dict[str, str] = {}
    if not existing_ok:
        to_set[header_name] = session

    if mirror:
        lowered = header_name.lower()
        for name, fixed in MIRROR_HEADERS:
            if name.lower() == lowered:
                continue
            value = current.get(name)
            if isinstance(value, str) and value.strip() != "":
                continue
            to_set[name] = fixed if fixed is not None else effective
    return to_set


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """大小写不敏感地读取请求头；兼容 ``httpx.Headers`` 与普通 dict。"""
    value = headers.get(name)
    if isinstance(value, str):
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == lowered and isinstance(candidate, str):
            return candidate
    return None


class InjectionEngine:
    """按端点匹配容错注入会话头与 Anthropic 认证兼容头：失败只记日志，绝不抛出、不覆盖已有值。"""

    def __init__(
        self,
        *,
        enable: bool,
        matcher: EndpointMatcher,
        resolver: SessionResolver,
        header_name: str = DEFAULT_HEADER_NAME,
        mirror: bool = True,
        log_injection: bool = True,
        log_skip: bool = True,
        logger: LoggerFn | None = None,
    ):
        self.enable = enable
        self.matcher = matcher
        self.resolver = resolver
        self.header_name = header_name
        self.mirror = mirror
        self._log_injection_enabled = log_injection
        self._log_skip_enabled = log_skip
        self._logger = logger

    def inject_request(self, request: object) -> None:
        """向 ``request`` 注入会话头；任何异常只记 error 日志，绝不抛出。

        跳过条件：插件禁用、端点不匹配。跳过日志按 ``log_skip`` 输出，
        且仅当请求主机命中 ``target_hosts`` 时才会记录（``host_matches``），
        其它主机的请求静默跳过，避免全进程 httpx 请求刷屏。
        会话来源：优先 ``CURRENT_SESSION``（来源标签 ``conversation``），
        否则交给 ``SessionResolver`` 兜底（``fixed`` / ``random``）。
        """
        url = ""
        try:
            url = str(getattr(request, "url", "") or "")
            # 主机命中一次性判定，供下方两处跳过日志做门控（R5）
            host_hit = self.matcher.host_matches(url)

            if not self.enable:
                if host_hit:
                    self._log_skip("插件已禁用", url)
                return

            if not self.matcher.matches(url):
                if host_hit:
                    self._log_skip("端点不匹配", url)
                return

            # Anthropic SDK 2.3+ 只发 Authorization: Bearer，而 OpenCode 的
            # Anthropic Messages 端点要求 x-api-key；补一个同值头，不覆盖已有值。
            self._ensure_anthropic_api_key(request)

            session = CURRENT_SESSION.get()
            if session:
                origin = "conversation"
            else:
                session, origin = self.resolver.resolve(None)

            to_set = plan_header_injection(
                request.headers,
                header_name=self.header_name,
                session=session,
                mirror=self.mirror,
            )
            for name, value in to_set.items():
                request.headers[name] = value

            if self._log_injection_enabled:
                value = request.headers.get(self.header_name, session)
                if isinstance(value, str):
                    value = value.strip()
                self._log(
                    "info",
                    f"已注入 {self.header_name}={value} (来源={origin}, url={url})",
                )
        except Exception as exc:  # noqa: BLE001 - 注入路径吞掉一切异常，只记日志
            self._log(
                "error",
                f"注入失败，已忽略：{type(exc).__name__}: {exc} (url={url})",
            )

    @staticmethod
    def _ensure_anthropic_api_key(request: object) -> None:
        """为 OpenCode 的 Anthropic Messages 端点补齐 ``x-api-key``。

        新版 Anthropic SDK（如 2.3.0）默认发送 ``Authorization: Bearer``，
        而 OpenCode 该端点只认 ``x-api-key``，缺失时网关返回
        ``401 Go requires a credential``。仅在请求已带 Bearer 且没有
        ``x-api-key`` 时复制同值头；不覆盖已有值，也不记录凭据。
        """
        path = urlsplit(str(getattr(request, "url", "") or "")).path or "/"
        if path != "/v1/messages" and not path.endswith("/v1/messages"):
            return
        headers = request.headers
        if _get_header(headers, "x-api-key"):
            return
        authorization = _get_header(headers, "authorization")
        if not isinstance(authorization, str):
            return
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return
        headers["x-api-key"] = token

    def _log(self, level: str, message: str) -> None:
        """带统一前缀输出日志；``logger`` 为空或日志自身失败时静默跳过。"""
        if self._logger is None:
            return
        try:
            self._logger(level, f"{LOG_PREFIX} {message}")
        except Exception:  # noqa: BLE001, S110 - 日志失败也绝不能影响注入流程
            pass

    def _log_skip(self, reason: str, url: str) -> None:
        """按 ``log_skip`` 开关记录跳过原因；调用方负责只对目标主机调用。"""
        if self._log_skip_enabled:
            self._log("info", f"已跳过注入：{reason} (url={url})")

    def install_hooks(self) -> list[str]:
        """挂载 httpx / httpx2 传输钩子，返回已挂载的标签列表（幂等）。

        对每个发现到的传输模块，依次处理 ``AsyncClient`` / ``Client`` 的
        ``send``；类对象按 ``id`` 去重（跨模块共享）。``send`` 不可调用的
        类跳过；已带 ``HOOK_MARKER`` 的 ``send`` 只记账、不再包装，保证
        二次安装不叠加包装层。
        """
        records: list[tuple[type, object, object]] = list(
            getattr(self, "_hooked", [])
        )
        labels: list[str] = list(getattr(self, "_hooked_labels", []))
        seen: set[int] = set()
        for label, module in discover_transport_modules():
            for cls_name in ("AsyncClient", "Client"):
                cls = getattr(module, cls_name, None)
                if not isinstance(cls, type) or id(cls) in seen:
                    continue
                seen.add(id(cls))
                original = getattr(cls, "send", None)
                if not callable(original):
                    continue
                entry = f"{label}.{cls_name}.send"
                if getattr(original, HOOK_MARKER, False):
                    # 已被本插件包装（如本引擎二次安装）：不叠加第二层
                    records.append((cls, original, original))
                else:
                    wrapped = self._make_send_wrapper(
                        original, is_async=inspect.iscoroutinefunction(original)
                    )
                    cls.send = wrapped
                    records.append((cls, original, wrapped))
                if entry not in labels:
                    labels.append(entry)
        self._hooked = records
        self._hooked_labels = labels
        return list(labels)

    def uninstall_hooks(self) -> None:
        """逆序还原已挂载的 ``send`` 包装，并清空记录与标签。

        仅当类上的 ``send`` 仍是本条记录写入的包装对象时才还原，
        避免误伤被其它代码替换过的 ``send``。
        """
        for cls, original, wrapped in reversed(getattr(self, "_hooked", [])):
            if getattr(cls, "send", None) is wrapped:
                cls.send = original
        self._hooked = []
        self._hooked_labels = []

    @property
    def hooked_labels(self) -> list[str]:
        """当前已挂载的传输钩子标签（未安装或已还原时为空列表）。"""
        return list(getattr(self, "_hooked_labels", []))

    def _make_send_wrapper(self, original, *, is_async: bool):
        """生成透明 ``send`` 包装器：先注入再原样转发，并打上 ``HOOK_MARKER``。"""
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


# ---------------------------------------------------------------------------
# 传输钩子：httpx / httpx2 模块发现
# ---------------------------------------------------------------------------
def discover_transport_modules() -> list[tuple[str, types.ModuleType]]:
    """发现可挂钩的传输模块，返回 ``[(标签, 模块)]``。

    候选顺序：顶层 ``httpx``、顶层 ``httpx2``、``openai._base_client`` 与
    ``anthropic._base_client`` 的 ``httpx`` / ``httpx2`` 属性。逐个用
    ``importlib.import_module`` 尝试，导入失败一律跳过；``None``、非模块
    对象、同时缺少 ``Client`` 与 ``AsyncClient`` 属性的候选同样跳过；
    按模块对象 ``id`` 去重（先到先得）。
    """
    candidates: tuple[tuple[str, str, str | None], ...] = (
        ("httpx", "httpx", None),
        ("httpx2", "httpx2", None),
        ("openai._base_client.httpx", "openai._base_client", "httpx"),
        ("openai._base_client.httpx2", "openai._base_client", "httpx2"),
        ("anthropic._base_client.httpx", "anthropic._base_client", "httpx"),
        ("anthropic._base_client.httpx2", "anthropic._base_client", "httpx2"),
    )
    found: list[tuple[str, types.ModuleType]] = []
    seen: set[int] = set()
    for label, module_name, attr in candidates:
        try:
            module: object = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - 可选依赖，导入失败一律跳过
            module = None
        if attr is not None:
            module = getattr(module, attr, None)
        if not isinstance(module, types.ModuleType):
            continue
        if not (hasattr(module, "Client") or hasattr(module, "AsyncClient")):
            continue
        if id(module) in seen:
            continue
        seen.add(id(module))
        found.append((label, module))
    return found
