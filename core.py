"""零 AstrBot 依赖的核心逻辑：端点匹配、配置工具与会话头注入基础设施。"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
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
