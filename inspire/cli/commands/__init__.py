"""CLI command modules.

This module imports the command **modules**, never their Click groups under the modules' own names. The reason
is load-bearing and easy to undo by accident: `import a.b.c as m` resolves `a.b` by *attribute* lookup on the
parent package, so a single `from .job import job` here replaces the `inspire.cli.commands.job` attribute with
a Group object and every dotted import below it stops working -

    import inspire.cli.commands.job.job_commands as jc
    ImportError: cannot import name 'job_commands' from '<unknown module name>'

an error that names nothing true. `from inspire.cli.commands.job import job_commands` still worked, which is
why our own suite never noticed while anything written against the documented layout failed. Import the group
from the package that defines it, e.g. `from inspire.cli.commands.job import job`. tests/test_import_surface.py
holds this line; the regression was measured at 20 dotted paths unreachable before the rule existed.
"""

from . import (
    api,
    bridge,
    config,
    dataset,
    hpc,
    image,
    init,
    job,
    model,
    model_hub,
    notebook,
    project,
    ray,
    resources,
    run,
    runtime,
    schema_cmd,
    storage,
    sync,
    tunnel,
    user,
)

__all__ = [
    "api",
    "job",
    "resources",
    "config",
    "dataset",
    "sync",
    "bridge",
    "tunnel",
    "run",
    "notebook",
    "init",
    "image",
    "project",
    "model",
    "model_hub",
    "runtime",
    "schema_cmd",
    "storage",
    "hpc",
    "ray",
    "user",
]
