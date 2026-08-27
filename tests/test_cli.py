"""The CLI surface: what works today works, and the unbuilt seams say so."""
from typer.testing import CliRunner

from tellerly import __version__
from tellerly.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_the_seams():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("start-app", "discover", "replay", "capabilities"):
        assert command in result.output


def test_replay_seam_exits_nonzero_and_says_why():
    result = runner.invoke(app, ["replay", "some_capability"])
    assert result.exit_code == 2
    assert "not built yet" in result.output


def test_discover_requires_a_job_spec():
    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 2  # typer: missing required --job
