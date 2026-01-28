"""CLI command modules."""

from inspire.cli.commands.job import job
from inspire.cli.commands.resources import resources
from inspire.cli.commands.config import config
from inspire.cli.commands.sync import sync
from inspire.cli.commands.bridge import bridge
from inspire.cli.commands.tunnel import tunnel
from inspire.cli.commands.run import run
from inspire.cli.commands.notebook import notebook
from inspire.cli.commands.init import init

__all__ = ["job", "resources", "config", "sync", "bridge", "tunnel", "run", "notebook", "init"]
