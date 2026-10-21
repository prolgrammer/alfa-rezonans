import pytest
from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()

STUBS = [
    (["seed"], "BE-9"),
    (["load-clients", "data/clients", "--report"], "DQ-1"),
    (["ingest-bank", "data/bank"], "BE-7"),
    (["collect", "--source", "cbr", "--backfill"], "BE-4"),
    (["import", "news.jsonl"], "BE-8a"),
    (["warm"], "BE-10"),
    (["eval"], "DQ-14"),
    (["smoke"], "BE-13"),
    (["llm", "probe", "--dry-run"], "ML-2"),
    (["sources", "test"], "BE-4"),
    (["demo"], "BE-12"),
    (["status", "--watch"], "BE-10"),
    (["lint-text", "docs", "README.md"], "ML-16"),
]


@pytest.mark.parametrize(("cmd", "item"), STUBS, ids=lambda v: " ".join(v) if isinstance(v, list) else v)
def test_not_implemented_command(cmd, item):
    result = runner.invoke(app, cmd)
    assert result.exit_code == 2
    assert f"ещё не реализовано ({item})" in result.output


def test_help_lists_all_commands():
    output = runner.invoke(app, ["--help"]).output
    top_level = {cmd[0] for cmd, _ in STUBS} | {"migrate", "openapi"}
    for name in top_level:
        assert name in output
    sub = runner.invoke(app, ["llm", "--help"]).output
    assert "probe" in sub
    assert "test" in runner.invoke(app, ["sources", "--help"]).output
