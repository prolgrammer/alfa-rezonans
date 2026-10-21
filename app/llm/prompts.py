import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jinja2
import yaml

_HEADER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


def _split_header(source: str) -> tuple[dict[str, Any], str]:
    match = _HEADER_RE.match(source)
    if not match:
        return {}, source
    header = yaml.safe_load(match.group(1))
    return (header if isinstance(header, dict) else {}), source[match.end() :]


class _HeaderStrippingLoader(jinja2.FileSystemLoader):
    # шапку отрезаем у любых файлов: так {% include "_style.md" %} не тащит её в текст промпта
    def get_source(self, environment: jinja2.Environment, template: str):
        source, filename, uptodate = super().get_source(environment, template)
        return _split_header(source)[1], filename, uptodate


class Prompts:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._env = jinja2.Environment(
            loader=_HeaderStrippingLoader(str(directory), encoding="utf-8"),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            keep_trailing_newline=False,
        )

    def render(self, name: str, variables: Mapping[str, Any]) -> tuple[str, str]:
        """Текст промпта без шапки и версия вида `<имя>@<version>`."""
        path = self._dir / f"{name}.md"
        if not path.is_file():
            raise jinja2.TemplateNotFound(f"{name}.md")
        header, _ = _split_header(path.read_text(encoding="utf-8"))
        version = header.get("version")
        if version is None:
            raise ValueError(f"в шапке {path.name} нет version: шапка с версией обязательна")
        text = self._env.get_template(f"{name}.md").render(**variables).strip()
        return text, f"{name}@{version}"
