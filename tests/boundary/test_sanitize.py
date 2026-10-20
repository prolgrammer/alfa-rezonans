import unicodedata

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


def test_removes_lone_surrogates():
    cleaned = sanitize("a\ud800b\udfffc")
    assert cleaned == "abc"
    cleaned.encode("utf-8")


def test_removes_variation_selectors():
    assert sanitize("a️b︀c\U000e0100d\U000e01efe") == "abcde"


def test_hidden_char_between_base_and_accent_does_not_block_composition():
    assert sanitize("e​́") == "é"


def test_result_is_nfkc_and_idempotent():
    mixed = "e​́ ﬁ ①⁦ \U000e0041 Ёж\ud800️"
    once = sanitize(mixed)
    assert sanitize(once) == once
    assert unicodedata.normalize("NFKC", once) == once
