from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def test_cli_help_includes_top_level_groups() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    assert "job" in result.output
    assert "notebook" in result.output
    assert "image" in result.output
    assert "resources" in result.output
    assert "tunnel" in result.output


def test_job_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output
    assert "logs" in result.output


def test_notebook_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["notebook", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "status" in result.output
    assert "top" in result.output


def test_resources_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["resources", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "nodes" in result.output


def test_tunnel_help_includes_key_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["tunnel", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "list" in result.output
    assert "status" in result.output


def test_job_create_help_shows_fault_tolerant_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["job", "create", "--help"])
    assert result.exit_code == 0
    assert "--fault-tolerant" in result.output
    assert "--no-fault-tolerant" in result.output


def test_run_help_shows_fault_tolerant_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--fault-tolerant" in result.output
    assert "--no-fault-tolerant" in result.output
