"""main.py 冒烟测试：在 stub AstrBot 环境下加载插件类并验证核心行为。

以 ``types.ModuleType`` 伪造 ``astrbot`` 各模块后，按包路径加载 ``main.py``，
真实执行其类定义与实例化；插件内部复用测试进程的顶层 ``core`` 模块，
保证 ``CURRENT_SESSION`` 等状态为同一实例。
"""

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

import core

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
PKG = "data.plugins.astrbot_plugin_opencode_session_header"
CID = "550e8400-e29b-41d4-a716-446655440000"
CID_SESSION = "ses_550e8400e29b41d4a716446655440000"


@pytest.fixture(autouse=True)
def _reset_current_session():
    """隔离 ContextVar：避免 CURRENT_SESSION 跨用例残留互相污染。"""
    token = core.CURRENT_SESSION.set(None)
    yield
    core.CURRENT_SESSION.reset(token)


class _FilterStub:
    """伪造 astrbot filter：全部装饰器恒等返回，便于直接调用原方法。"""

    class PermissionType:
        """权限类型占位。"""

        ADMIN = "admin"

    @staticmethod
    def on_llm_request():
        """返回恒等装饰器。"""
        return lambda func: func

    @staticmethod
    def command(_name):
        """返回恒等装饰器。"""
        return lambda func: func

    @staticmethod
    def permission_type(_permission):
        """返回恒等装饰器。"""
        return lambda func: func


class _StarStub:
    """伪造 astrbot Star 基类：仅记录 context。"""

    def __init__(self, context=None):
        """保存 context，对齐 AstrBot 基类行为。"""
        self.context = context


def _install_astrbot_stubs(monkeypatch):
    """向 sys.modules 注入 AstrBot 各模块的 stub。"""
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []

    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = logging  # 标准库 logging 具备 info/warning/error/debug
    api.AstrBotConfig = dict

    event = types.ModuleType("astrbot.api.event")
    event.filter = _FilterStub()
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderRequest = type("ProviderRequest", (), {})

    star = types.ModuleType("astrbot.api.star")
    star.Star = _StarStub
    star.Context = type("Context", (), {})

    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.provider", provider)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)


def _load_main(monkeypatch):
    """在 stub 环境下按包路径加载 main.py，返回模块对象。"""
    _install_astrbot_stubs(monkeypatch)

    data = types.ModuleType("data")
    data.__path__ = []
    plugins = types.ModuleType("data.plugins")
    plugins.__path__ = []
    package = types.ModuleType(PKG)
    package.__path__ = [str(ROOT)]
    # 预置同一 core 模块实例：`from . import core` 复用测试进程的 core，
    # 保证 CURRENT_SESSION 等 ContextVar 身份一致（避免重复加载成两个模块对象）
    package.core = core
    monkeypatch.setitem(sys.modules, "data", data)
    monkeypatch.setitem(sys.modules, "data.plugins", plugins)
    monkeypatch.setitem(sys.modules, PKG, package)

    spec = importlib.util.spec_from_file_location(PKG + ".main", MAIN)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_plugin_smoke(monkeypatch):
    """插件可在无 AstrBot 环境实例化，挂载传输钩子且卸载后完全还原。"""
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(context=None, config={})
    try:
        labels = plugin._engine.hooked_labels
        assert labels
        assert any(label.startswith("httpx") for label in labels)
    finally:
        asyncio.run(plugin.terminate())
    assert plugin._engine.hooked_labels == []


def test_on_llm_request_prefers_conversation_cid(monkeypatch):
    """会话来源优先取 req.conversation.cid，并推导出 Desktop 同款会话 ID。"""
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(context=None, config={})

    class _Conversation:
        cid = CID

    class _Request:
        conversation = _Conversation()
        session_id = "legacy"

    class _Event:
        unified_msg_origin = "platform:group:1"

    async def run():
        await plugin.on_llm_request(_Event(), _Request())
        return core.CURRENT_SESSION.get()

    try:
        assert asyncio.run(run()) == CID_SESSION
    finally:
        asyncio.run(plugin.terminate())


def test_fixed_mode_invalid_falls_back(monkeypatch):
    """fixed 模式但固定值无效 → 警告并降级为随机兜底。"""
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(
        context=None, config={"missing_session_mode": "fixed", "fixed_session_id": ""}
    )
    try:
        _session, origin = plugin._resolver.resolve(None)
        assert origin == "random"
    finally:
        asyncio.run(plugin.terminate())


def test_oc_session_command_reports_state(monkeypatch):
    """管理员命令输出注入会话值、匹配规则与启用状态。"""
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(context=None, config={})

    class _Event:
        unified_msg_origin = "platform:group:1"

        def plain_result(self, text):
            return text

    async def run():
        return [item async for item in plugin.oc_session(_Event())]

    try:
        report = asyncio.run(run())[0]
        assert "来源值：platform:group:1" in report
        assert f"{plugin._engine.header_name}: ses_" in report
        assert f"匹配规则：{plugin._matcher.description}" in report
        assert "启用状态：已启用" in report
    finally:
        asyncio.run(plugin.terminate())


def test_scheduler_hook_sets_and_restores_session(monkeypatch):
    """调度器兜底钩子：执行期间按 umo 设置会话、结束后还原，terminate 复原原方法。"""
    scheduler_module = types.ModuleType("astrbot.core.pipeline.scheduler")

    class _PipelineScheduler:
        async def execute(self, event):
            return core.CURRENT_SESSION.get()

    scheduler_module.PipelineScheduler = _PipelineScheduler
    monkeypatch.setitem(sys.modules, "astrbot.core", types.ModuleType("astrbot.core"))
    monkeypatch.setitem(
        sys.modules, "astrbot.core.pipeline", types.ModuleType("astrbot.core.pipeline")
    )
    monkeypatch.setitem(sys.modules, "astrbot.core.pipeline.scheduler", scheduler_module)

    original_execute = _PipelineScheduler.execute
    module = _load_main(monkeypatch)
    plugin = module.OpenCodeSessionHeaderPlugin(context=None, config={})

    class _Event:
        unified_msg_origin = "platform:group:1"

    async def run():
        before = core.CURRENT_SESSION.get()
        during = await _PipelineScheduler().execute(_Event())
        after = core.CURRENT_SESSION.get()
        return before, during, after

    try:
        before, during, after = asyncio.run(run())
        assert before is None
        assert after is None
        assert during == core.derive_session_id("platform:group:1")
    finally:
        asyncio.run(plugin.terminate())
    assert _PipelineScheduler.execute is original_execute
