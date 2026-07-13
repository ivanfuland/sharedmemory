"""钉住 cass_corpus.reader 的公开 API(EverOS adapter 及未来消费者 import 公开名)。
删/改任一公开符号,或把 coerce_tool_call_id 退化成恒等函数,本文件必须变红。"""
from cass_corpus import reader


# ── extra_dict 是 _extra_dict 的实现别名(同一对象,非拷贝) ──

def test_extra_dict_is_impl_alias():
    assert reader.extra_dict is reader._extra_dict


# ── EXTRA_COLS 是 _EXTRA_COLS 的公开名 ──

def test_extra_cols_public_alias():
    assert reader.EXTRA_COLS is reader._EXTRA_COLS
    assert reader.EXTRA_COLS == ("extra_bin", "extra_json")


# ── coerce_tool_call_id 收紧类型:非 str / 空串 → None,非空 str 原样返回 ──

def test_coerce_tool_call_id_drops_non_string():
    # 反证心态:若 coerce 退化成恒等函数,这两条 `is None` 会红。
    assert reader.coerce_tool_call_id(123) is None
    assert reader.coerce_tool_call_id(b"abc") is None


def test_coerce_tool_call_id_drops_empty_string():
    assert reader.coerce_tool_call_id("") is None


def test_coerce_tool_call_id_passes_through_nonempty_string():
    assert reader.coerce_tool_call_id("t1") == "t1"


# ── __all__ 声明了三件套 + 既有公开函数 ──

def test_all_lists_public_api():
    expected = {
        "extra_dict", "EXTRA_COLS", "coerce_tool_call_id",
        "select_conversations", "max_conversation_cursor", "read_messages",
        "get_conversation", "get_conversation_by_external_id", "max_message_ts",
    }
    assert expected <= set(reader.__all__)


def test_all_names_are_importable():
    # __all__ 里每个名字都真在模块上(挡住写错名 / 删符号忘改 __all__)。
    for name in reader.__all__:
        assert hasattr(reader, name), f"__all__ 声明了不存在的名字: {name}"
