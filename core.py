"""零 AstrBot 依赖的核心逻辑：端点匹配、配置工具与会话头注入基础设施。"""

from __future__ import annotations

import contextvars
import hashlib
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

    def matches(self, url: str) -> bool:
        """主机命中（完全相等或点后缀子域）且路径命中任一前缀时为 True。

        畸形 URL（解析异常 / 无主机 / 相对路径）一律返回 False，绝不抛出。
        """
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            return False
        if not host:
            return False
        if not any(host == h or host.endswith("." + h) for h in self._hosts):
            return False
        if not self._path_prefixes:
            return True
        path = parts.path or "/"
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


class InjectionEngine:
    """按端点匹配容错注入会话头：失败只记日志，绝不抛出、不覆盖已有值。"""

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

        跳过条件（按 ``log_skip`` 记 skip 日志）：插件禁用、端点不匹配。
        会话来源：优先 ``CURRENT_SESSION``（来源标签 ``conversation``），
        否则交给 ``SessionResolver`` 兜底（``fixed`` / ``random``）。
        """
        url = ""
        try:
            url = str(getattr(request, "url", "") or "")

            if not self.enable:
                self._log_skip("插件已禁用", url)
                return

            if not self.matcher.matches(url):
                self._log_skip("端点不匹配", url)
                return

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

    def _log(self, level: str, message: str) -> None:
        """带统一前缀输出日志；``logger`` 为空或日志自身失败时静默跳过。"""
        if self._logger is None:
            return
        try:
            self._logger(level, f"{LOG_PREFIX} {message}")
        except Exception:  # noqa: BLE001, S110 - 日志失败也绝不能影响注入流程
            pass

    def _log_skip(self, reason: str, url: str) -> None:
        """按 ``log_skip`` 开关记录跳过原因。"""
        if self._log_skip_enabled:
            self._log("info", f"已跳过注入：{reason} (url={url})")
