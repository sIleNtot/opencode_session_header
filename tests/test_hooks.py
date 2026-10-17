"""传输钩子测试：模块发现、send 包装往返、幂等安装与并发会话隔离。"""

import asyncio
import inspect
import sys
import types

import pytest

import core

SESSION = "ses_" + "b" * 32
SESSION_A = "ses_" + "a" * 32
SESSION_B = "ses_" + "c" * 32
RANDOM = "ses_" + "r" * 32
MATCH_URL = "https://opencode.ai/zen/go/v1/chat/completions"
OTHER_URL = "https://api.openai.com/v1/chat/completions"
HOSTS, PREFIXES = ("opencode.ai",), ("/zen", "/inference")


@pytest.fixture(autouse=True)
def _reset_current_session():
    """隔离 ContextVar：避免 CURRENT_SESSION 跨用例残留互相污染。"""
    token = core.CURRENT_SESSION.set(None)
    yield
    core.CURRENT_SESSION.reset(token)


def _make_engine(**overrides):
    """构造注入引擎：默认启用、命中端点、随机兜底、关闭日志输出。"""
    kwargs = {
        "enable": True,
        "matcher": core.EndpointMatcher(HOSTS, PREFIXES),
        "resolver": core.SessionResolver(random_session=RANDOM),
        "log_injection": False,
        "log_skip": False,
        "logger": None,
    }
    kwargs.update(overrides)
    return core.InjectionEngine(**kwargs)


# ---------------------------------------------------------------------------
# 发现机制
# ---------------------------------------------------------------------------
def test_discovery_finds_fake_httpx2(monkeypatch):
    fake = types.ModuleType("httpx2")
    fake.Client = type("Client", (), {})
    fake.AsyncClient = type("AsyncClient", (), {})
    monkeypatch.setitem(sys.modules, "httpx2", fake)

    found = dict(core.discover_transport_modules())

    assert found.get("httpx2") is fake


def test_discovery_skips_none_and_non_transport_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", None)  # 导入必然失败
    empty = types.ModuleType("openai._base_client")  # 无 Client/AsyncClient 属性
    monkeypatch.setitem(sys.modules, "openai._base_client", empty)

    labels = [label for label, _ in core.discover_transport_modules()]

    assert "httpx2" not in labels
    assert "openai._base_client.httpx" not in labels


def test_discovery_dedupes_same_module_object(monkeypatch):
    import httpx

    base = types.ModuleType("openai._base_client")
    base.httpx = httpx  # 与顶层 httpx 同一对象
    base.httpx2 = None
    monkeypatch.setitem(sys.modules, "openai._base_client", base)

    labels = [label for label, _ in core.discover_transport_modules()]

    assert labels.count("httpx") <= 1
    assert "openai._base_client.httpx" not in labels


# ---------------------------------------------------------------------------
# 安装 / 还原
# ---------------------------------------------------------------------------
def test_install_uninstall_restores_original_send():
    import httpx

    saved = httpx.AsyncClient.send
    engine = _make_engine()

    engine.install_hooks()
    assert httpx.AsyncClient.send is not saved
    assert getattr(httpx.AsyncClient.send, core.HOOK_MARKER, False) is True

    engine.install_hooks()  # 二次安装幂等
    assert httpx.AsyncClient.send is not saved  # 未叠加第二层
    assert httpx.AsyncClient.send.__wrapped__ is saved  # 仅一层包装

    engine.uninstall_hooks()

    assert httpx.AsyncClient.send is saved


def test_install_reports_labels_and_clears_on_uninstall():
    engine = _make_engine()

    labels = engine.install_hooks()
    try:
        assert "httpx.AsyncClient.send" in labels
        assert engine.hooked_labels == labels
    finally:
        engine.uninstall_hooks()

    assert engine.hooked_labels == []


class _FakeAsyncClient:
    async def send(self, request):
        return request


class _FakeSyncClient:
    def send(self, request):
        return request


def test_install_wraps_fake_module_classes_and_restores(monkeypatch):
    fake = types.ModuleType("fakehttpx")
    fake.AsyncClient = _FakeAsyncClient
    fake.Client = _FakeSyncClient
    monkeypatch.setattr(
        core, "discover_transport_modules", lambda: [("fakehttpx", fake)]
    )
    engine = _make_engine()
    saved_async = _FakeAsyncClient.send
    saved_sync = _FakeSyncClient.send

    labels = engine.install_hooks()

    assert labels == ["fakehttpx.AsyncClient.send", "fakehttpx.Client.send"]
    assert _FakeAsyncClient.send is not saved_async
    assert _FakeSyncClient.send is not saved_sync
    assert inspect.iscoroutinefunction(_FakeAsyncClient.send)
    assert not inspect.iscoroutinefunction(_FakeSyncClient.send)
    assert getattr(_FakeAsyncClient.send, core.HOOK_MARKER, False) is True

    engine.install_hooks()  # 二次安装不叠加
    assert _FakeAsyncClient.send.__wrapped__ is saved_async

    engine.uninstall_hooks()

    assert _FakeAsyncClient.send is saved_async
    assert _FakeSyncClient.send is saved_sync


def test_install_skips_non_callable_send(monkeypatch):
    broken = types.ModuleType("broken")
    broken.AsyncClient = type("AsyncClient", (), {"send": None})
    broken.Client = type("Client", (), {})  # 无 send 属性
    monkeypatch.setattr(
        core, "discover_transport_modules", lambda: [("broken", broken)]
    )
    engine = _make_engine()

    labels = engine.install_hooks()

    assert labels == []
    assert engine.hooked_labels == []


def test_install_counts_pre_hooked_send_without_rewrapping(monkeypatch):
    class PreHooked:
        async def send(self, request):
            return request

    async def pre_wrapped(self, request):  # 模拟已被本插件包装过的 send
        return await PreHooked.send(self, request)

    pre_wrapped.__wrapped__ = PreHooked.send
    setattr(pre_wrapped, core.HOOK_MARKER, True)
    PreHooked.send = pre_wrapped
    fake = types.ModuleType("prehooked")
    fake.AsyncClient = PreHooked
    monkeypatch.setattr(
        core, "discover_transport_modules", lambda: [("prehooked", fake)]
    )
    engine = _make_engine()

    labels = engine.install_hooks()

    assert labels == ["prehooked.AsyncClient.send"]
    assert PreHooked.send is pre_wrapped  # 未重复包装

    engine.uninstall_hooks()

    assert PreHooked.send is pre_wrapped  # 不还原别人的包装


# ---------------------------------------------------------------------------
# 真实 httpx / httpx2 往返
# ---------------------------------------------------------------------------
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
                await client.request(
                    "POST", MATCH_URL, json={}, follow_redirects=True
                )

        asyncio.run(main())
    finally:
        engine.uninstall_hooks()

    assert seen["x-opencode-session"] == SESSION
    assert seen["x-session-affinity"] == SESSION
    assert seen["x-opencode-client"] == "astrbot"


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_send_roundtrip_ignores_other_hosts(module_name):
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
                await client.request(
                    "POST", OTHER_URL, json={}, follow_redirects=True
                )

        asyncio.run(main())
    finally:
        engine.uninstall_hooks()

    assert "x-opencode-session" not in seen
    assert "x-opencode-client" not in seen


def test_concurrent_sessions_do_not_leak():
    mod = pytest.importorskip("httpx")
    mock_transport = getattr(mod, "MockTransport", None)
    if mock_transport is None:
        pytest.skip("httpx 无 MockTransport")
    engine = _make_engine()
    engine.install_hooks()
    try:
        seen = []

        def handler(request):
            seen.append(request.headers.get("x-opencode-session"))
            return mod.Response(200, json={})

        async def send(session):
            core.CURRENT_SESSION.set(session)
            async with mod.AsyncClient(transport=mock_transport(handler)) as client:
                await client.post(MATCH_URL, json={})

        async def main():
            await asyncio.gather(send(SESSION_A), send(SESSION_B))

        asyncio.run(main())
    finally:
        engine.uninstall_hooks()

    assert set(seen) == {SESSION_A, SESSION_B}
