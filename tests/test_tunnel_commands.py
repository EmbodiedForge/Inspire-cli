from pathlib import Path

from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def test_tunnel_update_auto_refreshes_installed_ssh_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    rtunnel = tmp_path / ".local" / "bin" / "rtunnel"
    rtunnel.parent.mkdir(parents=True, exist_ok=True)
    rtunnel.write_text("#!/bin/sh\nexit 0\n")
    rtunnel.chmod(0o755)

    inspire_dir = tmp_path / ".inspire"
    inspire_dir.mkdir(parents=True, exist_ok=True)
    bridges_json = inspire_dir / "bridges.json"
    bridges_json.write_text(
        "{\n"
        '  "default": "cpu",\n'
        '  "bridges": [\n'
        '    {"name": "cpu", "proxy_url": "https://old.example/ws/cpu/proxy/31337/", "ssh_user": "root", "ssh_port": 22222, "has_internet": true}\n'
        "  ]\n"
        "}\n"
    )

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_config = ssh_dir / "config"
    ssh_config.write_text(
        "# >>> Inspire Bridges (auto-generated) >>>\n"
        "Host cpu\n"
        "    HostName localhost\n"
        "    User root\n"
        "    Port 22222\n"
        "    ProxyCommand /tmp/rtunnel wss://old.example/ws/cpu/proxy/31337/ localhost:%p\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n"
        "    LogLevel ERROR\n"
        "# <<< Inspire Bridges (auto-generated) <<<\n"
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["tunnel", "update", "cpu", "--url", "https://new.example/ws/cpu/proxy/31337/"],
    )

    assert result.exit_code == 0
    content = ssh_config.read_text()
    assert "old.example" not in content
    assert "new.example" in content
    assert "exec nc 127.0.0.1" in content
