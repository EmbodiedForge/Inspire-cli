"""CLI command modules."""

from inspire.cli.commands.job import job
from inspire.cli.commands.resources import resources
from inspire.cli.commands.nodes import nodes
from inspire.cli.commands.config import config

__all__ = ["job", "resources", "nodes", "config"]
