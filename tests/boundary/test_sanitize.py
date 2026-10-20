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
