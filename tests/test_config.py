"""配置强制转换工具测试：布尔与字符串列表的兜底规则。"""

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
