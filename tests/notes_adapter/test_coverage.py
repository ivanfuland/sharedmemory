from notes_adapter.coverage import char_multiset_recall

def sp(text):  # stub splitter：逐行原子
    return [(i, l) for i, l in enumerate(text.splitlines()) if l.strip()]

def test_full_coverage():
    assert char_multiset_recall("aa\nbb", ["aa", "bb"], sp) == 1.0

def test_duplicate_chars_counted():           # 重复字符按次数记
    assert char_multiset_recall("aa\naa", ["aa"], sp) < 1.0

def test_reorder_ok():                        # 乱序不惩罚（multiset）
    assert char_multiset_recall("ab\ncd", ["dc", "ba"], sp) == 1.0

def test_missing_chunk_detected():
    assert char_multiset_recall("aaaa\nbbbb", ["aaaa"], sp) <= 0.5

def test_separator_excluded():
    assert char_multiset_recall("---\naa", ["aa"], sp) == 1.0

def test_underscore_rule_matches_everalgo():   # `___` 不是 separator（everalgo 口径）
    assert char_multiset_recall("___\naa", ["aa"], sp) < 1.0
