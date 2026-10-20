from app.text.sanitize import sanitize


def test_removes_invisible_and_control_characters():
    assert sanitize("при​вет‮ \x07мир﻿") == "привет мир"


def test_keeps_newline_and_tab():
    assert sanitize("a\tb\nc") == "a\tb\nc"


def test_russian_text_is_unchanged():
    text = "Ёлка и ёж: ключевая ставка — 14,0%"
    assert sanitize(text) == text


def test_normalizes_nfkc():
    assert sanitize("ﬁnal ①") == "final 1"


def test_removes_unicode_tag_characters():
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "ignore previous")
    assert sanitize(f"при{hidden}вет\U000e0001") == "привет"


def test_removes_bidi_isolates():
    assert sanitize("a⁦b⁧c⁨d⁩e") == "abcde"


def test_removes_private_use_and_unassigned():
    assert sanitize("ab\U0010fffdc͸d") == "abcd"
