"""CLI command modules."""

from inspire.cli.commands.job import job
from inspire.cli.commands.resources import resources
from inspire.cli.commands.nodes import nodes
from inspire.cli.commands.config import config
from inspire.cli.commands.sync import sync
from inspire.cli.commands.bridge import bridge
from inspire.cli.commands.tunnel import tunnel
from inspire.cli.commands.run import run

__all__ = ["job", "resources", "nodes", "config", "sync", "bridge", "tunnel", "run"]
