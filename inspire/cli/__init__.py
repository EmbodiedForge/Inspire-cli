"""Inspire CLI - Command-line interface for the Inspire training platform."""

# `from .main import main` here would replace the `inspire.cli.main` attribute with the Click group, which
# breaks `import inspire.cli.main as m` for every consumer (tests/test_import_surface.py). The group is
# exported as cli_entrypoint; the module keeps its own name.
from . import main as main  # noqa: F401  - submodule attribute must stay the module
from .main import cli as cli_entrypoint

__all__ = ["cli_entrypoint", "main"]
