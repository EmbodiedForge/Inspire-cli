"""Configuration commands for Inspire CLI.

Commands:
    inspire config show    - Display merged configuration with sources
    inspire config check   - Validate environment and authentication
    inspire config env     - Generate .env template
"""

import click

from inspire.cli.commands.config_check import check_config
from inspire.cli.commands.config_env_template import generate_env
from inspire.cli.commands.config_show import show_config


@click.group()
def config() -> None:
    """Inspect and validate Inspire CLI configuration."""
    pass


config.add_command(show_config)
config.add_command(generate_env)
config.add_command(check_config)
