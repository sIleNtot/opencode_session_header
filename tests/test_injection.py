"""注入引擎测试：头计划保留/镜像策略、端点匹配跳过与全量容错。"""

import httpx
import pytest

from core import (
    CURRENT_SESSION,
    EndpointMatcher,
    InjectionEngine,
    SessionResolver,
    plan_header_injection,
)

SESSION = "ses_" + "a" * 32
RANDOM = "ses_" + "r" * 32
FIXED = "ses_" + "f" * 32
MATCH_URL = "https://opencode.ai/zen/go/v1/messages"
OTHER_URL = "https://api.openai.com/v1/chat/completions"
HOSTS, PREFIXES = ("opencode.ai",), ("/zen", "/inference")


@pytest.fixture(autouse=True)
def _reset_current_session():
    """隔离 ContextVar：避免 CURRENT_SESSION 跨用例残留互相污染。"""
    token = CURRENT_SESSION.set(None)
    yield
    CURRENT_SESSION.reset(token)


class FakeRequest:
    """最小请求替身：url 为 str、headers 为普通 dict。"""

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = {} if headers is None else headers


class RecordingLogger:
    """按 ``(level, message)`` 记录日志调用的可调用对象。"""

    def __init__(self):
        self.calls = []

    def __call__(self, level, message):
        self.calls.append((level, message))


def make_engine(**overrides):
    """构造注入引擎：默认启用、命中端点、随机兜底、无 logger。"""
    kwargs = {
        "enable": True,
        "matcher": EndpointMatcher(HOSTS, PREFIXES),
        "resolver": SessionResolver(random_session=RANDOM),
        "logger": None,
    }
    kwargs.update(overrides)
    return InjectionEngine(**kwargs)


# ---------------------------------------------------------------------------
# plan_header_injection：纯函数头计划
# ---------------------------------------------------------------------------
def test_plan_writes_primary_and_mirrors():
    plan = plan_header_injection(
        {}, header_name="x-opencode-session", session=SESSION, mirror=True
    )
    assert plan == {
        "x-opencode-session": SESSION,
        "x-opencode-session-id": SESSION,
        "x-session-affinity": SESSION,
        "X-Session-Id": SESSION,
        "x-opencode-client": "astrbot",
    }


def test_plan_keeps_existing_primary_and_mirrors_its_value():
    plan = plan_header_injection(
        {"x-opencode-session": "custom-value"},
        header_name="x-opencode-session",
        session=SESSION,
        mirror=True,
    )
    assert "x-opencode-session" not in plan
    assert plan["x-opencode-session-id"] == "custom-value"
    assert plan["x-session-affinity"] == "custom-value"
    assert plan["X-Session-Id"] == "custom-value"
    assert plan["x-opencode-client"] == "astrbot"


def test_plan_keeps_existing_individual_mirror():
    plan = plan_header_injection(
        {"X-Session-Id": "user-set"},
        header_name="x-opencode-session",
        session=SESSION,
        mirror=True,
    )
    assert "X-Session-Id" not in plan
    assert plan["x-opencode-session-id"] == SESSION


def test_plan_mirror_off_and_empty_primary():
    assert plan_header_injection(
        {"x-opencode-session": ""},
        header_name="x-opencode-session",
        session=SESSION,
        mirror=False,
    ) == {"x-opencode-session": SESSION}
    assert plan_header_injection(
        {"x-opencode-session": SESSION},
        header_name="x-opencode-session",
        session=SESSION,
        mirror=False,
    ) == {}


def test_plan_does_not_mutate_input_mapping():
    current = {"x-opencode-session": "", "X-Session-Id": "user-set"}
    snapshot = dict(current)

    plan_header_injection(
        current, header_name="x-opencode-session", session=SESSION, mirror=True
    )

    assert current == snapshot


def test_plan_strips_existing_primary_for_mirror_values():
    plan = plan_header_injection(
        {"x-opencode-session": "  custom  "},
        header_name="x-opencode-session",
        session=SESSION,
        mirror=True,
    )
    assert "x-opencode-session" not in plan
    assert plan["x-opencode-session-id"] == "custom"
    assert plan["x-session-affinity"] == "custom"


def test_plan_skips_mirror_named_like_custom_header():
    plan = plan_header_injection(
        {}, header_name="x-session-id", session=SESSION, mirror=True
    )
    assert plan == {
        "x-session-id": SESSION,
        "x-opencode-session-id": SESSION,
        "x-session-affinity": SESSION,
        "x-opencode-client": "astrbot",
    }


# ---------------------------------------------------------------------------
# InjectionEngine：容错注入
# ---------------------------------------------------------------------------
def test_engine_injects_on_match_and_sets_all_headers():
    engine = make_engine()
    req = FakeRequest(MATCH_URL)
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    assert req.headers == {
        "x-opencode-session": SESSION,
        "x-opencode-session-id": SESSION,
        "x-session-affinity": SESSION,
        "X-Session-Id": SESSION,
        "x-opencode-client": "astrbot",
    }


def test_engine_skips_non_matching_endpoint():
    engine = make_engine()
    req = FakeRequest(OTHER_URL)
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    assert req.headers == {}


def test_engine_disabled_does_not_inject():
    engine = make_engine(enable=False)
    req = FakeRequest(MATCH_URL)
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    assert req.headers == {}


def test_engine_headers_are_case_insensitive_with_httpx_headers():
    engine = make_engine()
    req = FakeRequest(MATCH_URL, headers=httpx.Headers({"X-Opencode-Session": "abc"}))
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    # 已有主头按大小写不敏感查找视为已设置：保留 "abc" 且镜像头对齐它
    assert req.headers["x-opencode-session"] == "abc"
    assert req.headers["x-opencode-session-id"] == "abc"
    assert req.headers["x-session-affinity"] == "abc"
    assert req.headers["x-opencode-client"] == "astrbot"
    assert len(req.headers) == 5


def test_engine_swallows_injection_errors():
    class _BoomResolver:
        def resolve(self, source):
            raise RuntimeError("boom")

    recorder = RecordingLogger()
    engine = make_engine(resolver=_BoomResolver(), logger=recorder)
    req = FakeRequest(MATCH_URL)

    engine.inject_request(req)  # 绝不抛出

    assert req.headers == {}
    assert recorder.calls and recorder.calls[0][0] == "error"
    _, message = recorder.calls[0]
    assert "[opencode_session_header]" in message
    assert "boom" in message


def test_engine_uses_fallback_when_no_context():
    recorder = RecordingLogger()
    engine = make_engine(
        resolver=SessionResolver(random_session=RANDOM),
        logger=recorder,
    )
    req = FakeRequest(MATCH_URL)

    engine.inject_request(req)

    assert req.headers["x-opencode-session"] == RANDOM
    assert any("来源=random" in message for _, message in recorder.calls)


def test_engine_context_session_wins_over_resolver():
    recorder = RecordingLogger()
    engine = make_engine(
        resolver=SessionResolver(fixed_session=FIXED, random_session=RANDOM),
        logger=recorder,
    )
    req = FakeRequest(MATCH_URL)
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    assert req.headers["x-opencode-session"] == SESSION
    assert any("来源=conversation" in message for _, message in recorder.calls)


def test_engine_logs_skip_reasons():
    recorder = RecordingLogger()

    make_engine(enable=False, logger=recorder).inject_request(FakeRequest(MATCH_URL))
    assert [level for level, _ in recorder.calls] == ["info"]
    assert "插件已禁用" in recorder.calls[0][1]

    recorder.calls.clear()
    make_engine(logger=recorder).inject_request(FakeRequest(OTHER_URL))
    assert [level for level, _ in recorder.calls] == ["info"]
    assert "端点不匹配" in recorder.calls[0][1]


def test_engine_respects_log_switches():
    recorder = RecordingLogger()
    engine = make_engine(logger=recorder, log_skip=False, log_injection=False)

    engine.inject_request(FakeRequest(OTHER_URL))
    CURRENT_SESSION.set(SESSION)
    engine.inject_request(FakeRequest(MATCH_URL))

    assert recorder.calls == []


def test_engine_mirror_off_only_writes_primary():
    engine = make_engine(mirror=False)
    req = FakeRequest(MATCH_URL)
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    assert req.headers == {"x-opencode-session": SESSION}


def test_engine_success_log_reports_kept_primary_value():
    recorder = RecordingLogger()
    engine = make_engine(logger=recorder)
    req = FakeRequest(MATCH_URL, headers={"x-opencode-session": "custom-value"})
    CURRENT_SESSION.set(SESSION)

    engine.inject_request(req)

    assert req.headers["x-opencode-session"] == "custom-value"
    assert any(
        "已注入 x-opencode-session=custom-value" in message
        for _, message in recorder.calls
    )
