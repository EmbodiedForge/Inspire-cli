"""Configuration commands for Inspire CLI."""

from __future__ import annotations

import click

from .check import check_config

# aliased: `from .account import account` would shadow the config.account submodule attribute
from .account import account as account_group
from .env_cmd import generate_env
from .set_cmd import set_config
from .show import show_config


@click.group()
def config() -> None:
    """Inspect and validate Inspire CLI configuration."""


config.add_command(show_config)
config.add_command(generate_env)
config.add_command(check_config)
config.add_command(set_config)
config.add_command(account_group)

__all__ = ["config"]
