"""会话 ID 推导与兜底解析测试：UUID 向量、稳定哈希、清洗与解析优先级。"""

import re

from core import (
    SessionResolver,
    derive_session_id,
    is_visible_ascii,
    new_random_session_id,
    sanitize_session_value,
)


def test_derive_from_uuid_is_deterministic_vector():
    assert derive_session_id("550e8400-e29b-41d4-a716-446655440000") == "ses_550e8400e29b41d4a716446655440000"
    # 去连字符 / 大写输入均归一到同一值
    assert derive_session_id("550E8400E29B41D4A716446655440000") == "ses_550e8400e29b41d4a716446655440000"
    assert derive_session_id("550e8400e29b41d4a716446655440000") == "ses_550e8400e29b41d4a716446655440000"
    assert derive_session_id("550E8400-E29B-41D4-A716-446655440000") == "ses_550e8400e29b41d4a716446655440000"


def test_derive_from_non_uuid_is_stable_hash():
    value = derive_session_id("aiocqhttp:group:12345")
    assert re.fullmatch(r"ses_[0-9a-f]{32}", value)
    assert derive_session_id("aiocqhttp:group:12345") == value


def test_is_visible_ascii_bounds():
    assert is_visible_ascii("abc_123") is True
    assert is_visible_ascii("!~") is True
    assert is_visible_ascii("") is False
    assert is_visible_ascii("a b") is False      # 空格（0x20）不在可见区间
    assert is_visible_ascii("a\tb") is False
    assert is_visible_ascii("a中") is False       # 非 ASCII


def test_sanitize_rejects_crlf_and_space():
    assert sanitize_session_value("abc-123") == "abc-123"
    assert sanitize_session_value(" a b ") is None      # 含空格
    assert sanitize_session_value("a\r\nX: y") is None  # CRLF
    assert sanitize_session_value("") is None
    assert sanitize_session_value(None) is None


def test_sanitize_strips_outer_whitespace_and_coerces():
    assert sanitize_session_value("  abc-123  ") == "abc-123"
    assert sanitize_session_value(123) == "123"


def test_resolver_precedence_and_origins():
    r = SessionResolver(fixed_session="ses_" + "f" * 32, random_session="ses_" + "r" * 32)
    assert r.resolve("550e8400-e29b-41d4-a716-446655440000") == ("ses_550e8400e29b41d4a716446655440000", "conversation")
    assert r.resolve(None) == ("ses_" + "f" * 32, "fixed")
    plain = SessionResolver(random_session="ses_" + "r" * 32)
    assert plain.resolve(None) == ("ses_" + "r" * 32, "random")


def test_resolver_empty_source_uses_fallback_labels():
    r = SessionResolver(fixed_session="ses_" + "f" * 32, random_session="ses_" + "r" * 32)
    assert r.resolve("") == ("ses_" + "f" * 32, "fixed")
    assert r.resolve("   ") == ("ses_" + "f" * 32, "fixed")


def test_resolver_invalid_fixed_falls_back_to_random():
    r = SessionResolver(fixed_session="bad value", random_session="ses_" + "r" * 32)
    assert r.resolve(None) == ("ses_" + "r" * 32, "random")


def test_resolver_generates_random_when_unset():
    value, origin = SessionResolver().resolve(None)
    assert origin == "random"
    assert re.fullmatch(r"ses_[0-9a-f]{32}", value)


def test_resolver_cache_cap():
    r = SessionResolver(random_session="ses_" + "r" * 32, cache_max=2)
    for i in range(5):
        r.resolve(f"source-{i}")
    assert len(r._cache) == 2


def test_resolver_cache_hit_is_stable_and_single_entry():
    r = SessionResolver(random_session="ses_" + "r" * 32)
    first = r.resolve("aiocqhttp:group:12345")
    assert first[1] == "conversation"
    assert r.resolve("aiocqhttp:group:12345") == first
    assert r._cache == {"aiocqhttp:group:12345": first[0]}


def test_random_session_format():
    assert re.fullmatch(r"ses_[0-9a-f]{32}", new_random_session_id())
