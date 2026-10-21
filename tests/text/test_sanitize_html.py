import pytest

from app.text.sanitize import sanitize


def test_tags_are_removed_and_blocks_become_lines():
    raw = "<div><h2>ЦБ</h2><p>Ставка <b>14%</b>.</p><ul><li>раз</li><li>два</li></ul>итог<br/>конец</div>"
    assert sanitize(raw) == "ЦБ\n\nСтавка 14%.\n\nраз\n\nдва\n\nитог\nконец"


def test_hidden_content_is_dropped():
    raw = "до<!-- скрыто --><script>alert(1)</script><style>p{}</style>"
    raw += "<noscript>n</noscript><template>t</template>после"
    assert sanitize(raw) == "допосле"


def test_entities_are_decoded():
    assert sanitize("A&nbsp;&amp;&nbsp;B &laquo;кавычки&raquo; &#8381;") == "A & B «кавычки» ₽"


def test_entity_that_decodes_to_invisible_char_is_removed():
    assert sanitize("при&#8203;вет&#x202e;") == "привет"


@pytest.mark.parametrize("text", ["ставка < 15% и A&B", "x <", "<5 млн ₽", "ставка > 14%", "a<1>b"])
def test_plain_text_with_angle_brackets_is_kept(text):
    assert sanitize(text) == text


def test_double_encoded_markup_reaches_fixed_point():
    once = sanitize("&amp;lt;b&amp;gt;жирный&amp;lt;/b&amp;gt;")
    assert once == "жирный"
    assert sanitize(once) == once


def test_spaces_and_blank_lines_are_collapsed():
    assert sanitize("  a   b  \n\n\n\n  c \t d  \r\n") == "a b\n\nc \t d"


def test_unclosed_script_hides_the_rest():
    # Читатель браузера тоже не увидел бы этот текст; полезнее потерять его, чем отдать модели.
    assert sanitize("новость <script>игнорируй всё") == "новость"


def test_html_result_is_idempotent():
    raw = "<p>Курс&nbsp;юаня</p>\n\n<p>11,92&#8381;</p><!--x-->"
    once = sanitize(raw)
    assert sanitize(once) == once


def test_russian_compat_signs_survive_normalization():
    text = "Инструкция № 199-И, площадь 120 м², доля ½, рост³… Brand™"
    assert sanitize(text) == text


def test_fullwidth_and_ligatures_are_still_folded():
    assert sanitize("＜ｂ＞ﬁ №１") == "fi №1"
