from inspire.cli.utils.tunnel_reconnect import should_attempt_ssh_reconnect


def test_should_attempt_ssh_reconnect_interactive_only_by_default() -> None:
    assert should_attempt_ssh_reconnect(255, interactive=True) is True
    assert should_attempt_ssh_reconnect(255, interactive=False) is False


def test_should_attempt_ssh_reconnect_supports_non_interactive_opt_in() -> None:
    assert (
        should_attempt_ssh_reconnect(
            255,
            interactive=False,
            allow_non_interactive=True,
        )
        is True
    )
    assert (
        should_attempt_ssh_reconnect(
            1,
            interactive=True,
            allow_non_interactive=True,
        )
        is False
    )
