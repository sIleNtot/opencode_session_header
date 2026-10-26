"""AstrBot 插件：OpenCode 请求头（会话头注入与 Anthropic 认证兼容）。

本插件在 httpx / httpx2 传输层统一拦截请求：只对命中 ``target_hosts`` +
``target_path_prefixes`` 的 URL 注入主头 ``x-opencode-session``，并可按需附加
OpenCode Desktop 同款镜像头（``x-opencode-session-id`` / ``x-session-affinity``
/ ``X-Session-Id`` / ``x-opencode-client``）；已存在的非空会话头不会被覆盖，
``User-Agent`` 与 ``x-astrbot-conversation-id`` 不受影响。

对 OpenCode 的 Anthropic Messages 端点，若请求只带 ``Authorization: Bearer``
而缺少 ``x-api-key``（Anthropic SDK 2.3+ 的默认行为），插件会补一个同值
``x-api-key``；不覆盖已有值，不记录凭据。

会话 ID 与 AstrBot 对话绑定：按 ``req.conversation.cid`` → 事件来源
``unified_msg_origin`` → ``req.session_id`` 的优先级解析来源，确定性推导为
OpenCode Desktop 同款 ``ses_`` 格式；无会话上下文时按 ``missing_session_mode``
使用随机会话（插件加载时生成、进程内复用）或 ``fixed_session_id``。

本插件：不发起额外网络请求；不收集 / 上传 / 落盘任何用户数据；不读写聊天内容。
注入路径任何异常只记日志、绝不抛出。

配置项（见 ``_conf_schema.json``，缺项一律回退默认值）：

* ``enable``                  —— 总开关，默认 ``True``；
* ``target_hosts``            —— 目标主机（含点后缀子域，可填完整 URL），默认 ``["opencode.ai"]``；
* ``target_path_prefixes``    —— 目标路径前缀（空列表 = 任意路径），默认 ``["/zen", "/inference"]``；
* ``header_name``             —— 注入的主请求头名，默认 ``x-opencode-session``；
* ``mirror_opencode_headers`` —— 是否附加 Desktop 同款镜像头，默认 ``True``；
* ``missing_session_mode``    —— 无会话上下文兜底：``random`` / ``fixed``，默认 ``random``；
* ``fixed_session_id``        —— fixed 模式取值（仅可见 ASCII），默认 ``""``；
* ``log_injection`` / ``log_skip`` —— 注入 / 跳过日志开关，默认均 ``True``。

管理员可用 ``/oc-session`` 查看当前会话的注入状态与匹配规则。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:
    from . import core
except ImportError:  # 例外：脚本式（非包）导入时回退绝对导入
    import core

# 会话来源标签的中文展示（SessionResolver.resolve 的第二项）
_ORIGIN_LABELS = {
    "conversation": "对话上下文",
    "fixed": "固定兜底",
    "random": "随机兜底",
}


class OpenCodeSessionHeaderPlugin(Star):
    """OpenCode Session Header 插件主类：装配 core 组件并转发 AstrBot 钩子。"""

    def __init__(self, context: Context | None = None, config: AstrBotConfig | None = None):
        """读取配置、构建匹配器 / 解析器 / 注入引擎，并安装一次性钩子。"""
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}
        cfg = self._as_mapping(self.config)

        # 主头名：空值或含不可见字符时回退默认头名
        header_name = core.sanitize_session_value(cfg.get("header_name"))

        # 兜底模式：非法取值或 fixed 值无效 → 警告并降级 random
        mode = str(cfg.get("missing_session_mode") or "random").strip().lower()
        if mode not in ("random", "fixed"):
            self._emit("warning", f"missing_session_mode={mode!r} 无效，已降级为 random")
            mode = "random"
        fixed_session = core.sanitize_session_value(cfg.get("fixed_session_id"))
        if mode == "fixed" and fixed_session is None:
            self._emit("warning", "fixed_session_id 为空或含不可见字符，已降级为 random")
            mode = "random"

        self._matcher = core.EndpointMatcher(
            core.coerce_str_list(cfg.get("target_hosts"), core.DEFAULT_TARGET_HOSTS),
            core.coerce_str_list(
                cfg.get("target_path_prefixes"), core.DEFAULT_TARGET_PATH_PREFIXES
            ),
        )
        self._resolver = core.SessionResolver(
            fixed_session=fixed_session if mode == "fixed" else None
        )
        self._engine = core.InjectionEngine(
            enable=core.coerce_bool(cfg.get("enable"), True),
            matcher=self._matcher,
            resolver=self._resolver,
            header_name=header_name or core.DEFAULT_HEADER_NAME,
            mirror=core.coerce_bool(cfg.get("mirror_opencode_headers"), True),
            log_injection=core.coerce_bool(cfg.get("log_injection"), True),
            log_skip=core.coerce_bool(cfg.get("log_skip"), True),
            logger=self._emit,
        )
        self._scheduler_hook: tuple[type, str, object, object] | None = None

        # 兜底钩子与传输钩子仅在插件启用（init）时安装一次，terminate 时还原
        self._install_scheduler_hook()
        labels = self._engine.install_hooks()
        self._emit(
            "info",
            f"插件已加载：enable={self._engine.enable}, 兜底模式={mode}, "
            f"匹配规则={self._matcher.description}, "
            f"传输钩子={', '.join(labels) or '无'}",
        )

    # ------------------------------------------------------------------ #
    # 日志与配置工具
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, message: str) -> None:
        """统一日志出口：补插件前缀；日志自身失败一律吞掉，绝不影响主流程。

        core 注入引擎回传的消息已自带前缀，这里避免重复拼接。
        """
        try:
            text = (
                message
                if message.startswith(core.LOG_PREFIX)
                else f"{core.LOG_PREFIX} {message}"
            )
            log_fn = getattr(logger, level, None)
            if not callable(log_fn):
                log_fn = logger.info
            log_fn(text)
        except Exception:  # noqa: BLE001, S110 - 日志失败绝不能影响插件流程
            pass

    @staticmethod
    def _as_mapping(config: object) -> Mapping[str, object]:
        """把配置对象归一为映射；非映射形态按空配置处理（全部走默认值）。"""
        if isinstance(config, Mapping):
            return config
        return {}

    # ------------------------------------------------------------------ #
    # 会话来源解析与兜底钩子
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_source(event: AstrMessageEvent, req: ProviderRequest) -> str | None:
        """按优先级解析会话来源：conversation.cid → unified_msg_origin → session_id。"""
        conversation = getattr(req, "conversation", None)
        cid = getattr(conversation, "cid", None) if conversation is not None else None
        if cid:
            return str(cid)
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            return str(umo)
        session_id = getattr(req, "session_id", None)
        return str(session_id) if session_id else None

    def _install_scheduler_hook(self) -> bool:
        """包装 ``PipelineScheduler.execute``：为未经 on_llm_request 的路径兜底。

        事件入口按 ``unified_msg_origin`` 推导会话并写入 ``CURRENT_SESSION``，
        结束后 ``reset`` 防串线；安装失败仅警告并返回 ``False``，不影响插件加载。
        """
        try:
            from astrbot.core.pipeline.scheduler import PipelineScheduler
        except Exception as exc:  # noqa: BLE001 - AstrBot 内部模块可能缺失或改版，兜底钩子尽力而为
            self._emit("warning", f"调度器兜底钩子安装失败：{type(exc).__name__}: {exc}")
            return False

        current = getattr(PipelineScheduler, "execute", None)
        if not callable(current):
            self._emit("warning", "调度器兜底钩子安装失败：PipelineScheduler.execute 不可调用")
            return False

        original = getattr(current, "_astrbot_opencode_session_header_original", current)

        async def wrapped(scheduler, event, *args, **kwargs):
            umo = getattr(event, "unified_msg_origin", None)
            session = self._resolver.resolve(str(umo))[0] if umo else None
            token = core.CURRENT_SESSION.set(session)
            try:
                return await original(scheduler, event, *args, **kwargs)
            finally:
                core.CURRENT_SESSION.reset(token)

        functools.update_wrapper(wrapped, original)
        wrapped._astrbot_opencode_session_header_hook = True
        wrapped._astrbot_opencode_session_header_original = original
        PipelineScheduler.execute = wrapped
        self._scheduler_hook = (PipelineScheduler, "execute", original, wrapped)
        return True

    def _restore_scheduler_hook(self) -> None:
        """还原调度器兜底钩子（仅当当前实现仍是本插件写入的包装对象）。"""
        if self._scheduler_hook is None:
            return
        cls, name, original, wrapped = self._scheduler_hook
        if getattr(cls, name, None) is wrapped:
            setattr(cls, name, original)
        self._scheduler_hook = None

    # ------------------------------------------------------------------ #
    # AstrBot 钩子与命令
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """解析会话来源并写入 ``CURRENT_SESSION``，供传输钩子注入。"""
        source = self._resolve_source(event, req)
        session, origin = self._resolver.resolve(source)
        core.CURRENT_SESSION.set(session)
        self._emit(
            "debug", f"会话上下文已更新：来源={origin}, 值={source or '<无>'}"
        )

    async def _fetch_current_cid(self, umo: str) -> str | None:
        """尽力读取当前对话 cid；版本差异或调用失败时静默返回 ``None``。"""
        if not umo:
            return None
        try:
            manager = getattr(self.context, "conversation_manager", None)
            getter = getattr(manager, "get_curr_conversation_id", None)
            if not callable(getter):
                return None
            result = getter(umo)
            if inspect.isawaitable(result):
                result = await result
            text = str(result).strip() if result else ""
            return text or None
        except Exception as exc:  # noqa: BLE001 - 仅为命令展示尽力取值，失败不影响输出
            self._emit("debug", f"读取当前对话 cid 失败：{type(exc).__name__}: {exc}")
            return None

    @filter.command("oc-session")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def oc_session(self, event: AstrMessageEvent):
        """查看当前会话的注入状态（仅管理员）。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        cid = await self._fetch_current_cid(umo)
        source = cid or umo or None
        session, origin = self._resolver.resolve(source)
        status = "已启用" if self._engine.enable else "已禁用"
        hooks = ", ".join(self._engine.hooked_labels) or "无"
        lines = [
            f"会话来源：{_ORIGIN_LABELS.get(origin, origin)}",
            f"来源值：{source or '<无>'}",
            f"{self._engine.header_name}: {session}",
            f"匹配规则：{self._matcher.description}",
            f"启用状态：{status}；传输钩子：{hooks}",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件被禁用或卸载时：还原传输钩子与调度器兜底钩子。"""
        self._engine.uninstall_hooks()
        self._restore_scheduler_hook()
        self._emit("info", "插件已卸载/禁用。")
