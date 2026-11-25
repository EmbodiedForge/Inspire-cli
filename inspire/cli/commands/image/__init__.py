"""Image management commands.

Usage:
    inspire image list [--source official|public|private|all]
    inspire image detail <image-id>
    inspire image register -n "name" -v v1.0
    inspire image save <notebook-id> -n "name"
    inspire image delete <image-id>
    inspire image set-default --job <name> --notebook <name>
"""

from __future__ import annotations

from copy import copy

import click

from .image_commands import (
    create_image_cmd,
    delete_image_cmd,
    image_detail,
    list_images_cmd,
    preheat_image_cmd,
    push_image_cmd,
    register_image_cmd,
    save_image_cmd,
    search_images_cmd,
    set_default_image_cmd,
    sync_image_cmd,
)


@click.group()
def image():
    """Manage Docker images for notebooks and jobs.

    \b
    Examples:
        inspire image list                           # List official images
        inspire image list --source private          # List custom images
        inspire image save <notebook-id> -n my-img   # Save notebook as image
        inspire image register -n my-img -v v1.0     # Register external image
        inspire image set-default --job my-pytorch   # Set default image
    """
    pass


image.add_command(list_images_cmd)
image.add_command(search_images_cmd)
image.add_command(image_detail)
image.add_command(create_image_cmd)
image.add_command(push_image_cmd)
image.add_command(register_image_cmd)
image.add_command(save_image_cmd)
image.add_command(delete_image_cmd)
image.add_command(sync_image_cmd)
image.add_command(preheat_image_cmd)
image.add_command(set_default_image_cmd)
for _alias in ("info", "inspect", "describe"):
    _alias_command = copy(search_images_cmd)
    _alias_command.name = _alias
    _alias_command.short_help = "Alias for 'image search': find images by free text."
    _alias_command.help = (
        "Alias for 'inspire image search' (free-text image search).\n\n"
        "Use 'inspire image detail IMAGE_ID' to inspect one image by ID.\n\n"
        f"{search_images_cmd.help or ''}"
    )
    _alias_command.alias_of = "image search"  # type: ignore[attr-defined]
    image.add_command(_alias_command)
