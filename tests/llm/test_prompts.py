import jinja2
import pytest

from app.llm.prompts import Prompts


def write(directory, name, body, version=3):
    header = f"---\ntask: {name.strip('_')}\nversion: {version}\n---\n" if version is not None else ""
    (directory / f"{name}.md").write_text(header + body, encoding="utf-8")


def test_render_strips_header_and_returns_version(tmp_path):
    write(tmp_path, "demo", "Скажи {{ what }}\n")
    text, version = Prompts(tmp_path).render("demo", {"what": "привет"})
    assert text == "Скажи привет"
    assert version == "demo@3"


def test_include_without_header(tmp_path):
    write(tmp_path, "_style", "Пиши коротко.", version=1)
    write(tmp_path, "demo", '{% include "_style.md" %}\nДальше.')
    text, _ = Prompts(tmp_path).render("demo", {})
    assert text == "Пиши коротко.\nДальше."
    assert "version" not in text


def test_utf8_bom_is_ignored(tmp_path):
    source = "---\ntask: demo\nversion: 2\n---\nТекст"
    (tmp_path / "demo.md").write_bytes(source.encode("utf-8-sig"))
    assert Prompts(tmp_path).render("demo", {}) == ("Текст", "demo@2")


def test_missing_header_raises(tmp_path):
    write(tmp_path, "demo", "Без шапки", version=None)
    with pytest.raises(ValueError, match="demo.md"):
        Prompts(tmp_path).render("demo", {})


def test_header_without_version_raises(tmp_path):
    (tmp_path / "demo.md").write_text("---\ntask: demo\n---\nТекст", encoding="utf-8")
    with pytest.raises(ValueError, match="demo.md"):
        Prompts(tmp_path).render("demo", {})


def test_missing_variable_raises(tmp_path):
    write(tmp_path, "demo", "Скажи {{ what }}")
    with pytest.raises(jinja2.UndefinedError):
        Prompts(tmp_path).render("demo", {})


def test_missing_template_raises(tmp_path):
    with pytest.raises(jinja2.TemplateNotFound):
        Prompts(tmp_path).render("nope", {})
