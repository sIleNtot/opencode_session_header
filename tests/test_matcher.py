"""端点匹配器测试：zen/inference 全部正例、反例与归一化边界。"""

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


def test_default_arguments_match_default_config():
    assert EndpointMatcher().matches("https://opencode.ai/zen/go/v1/messages") is True


def test_description_lists_hosts_and_prefixes():
    matcher = EndpointMatcher(DEFAULT_TARGET_HOSTS, DEFAULT_TARGET_PATH_PREFIXES)
    assert matcher.description == "opencode.ai | /zen, /inference"


def test_description_shows_any_path_when_no_prefixes():
    assert EndpointMatcher(("opencode.ai",), ()).description == "opencode.ai | 任意路径"
