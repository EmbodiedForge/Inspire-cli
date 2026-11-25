"""Dynamic shell completion helpers for Inspire CLI."""

from __future__ import annotations

from click.shell_completion import CompletionItem

from inspire.config import Config
from inspire.config.schema import get_user_managed_options


def get_config_key_completions() -> list[CompletionItem]:
    """Get completions for config keys (e.g., defaults.target_dir)."""
    completions = []

    for option in get_user_managed_options():
        if not option.toml_key:
            continue
        completions.append(
            CompletionItem(
                option.toml_key,
                help=option.description[:50] if option.description else None,
            )
        )

    return sorted(completions, key=lambda x: x.value)


def get_workspace_alias_completions() -> list[str]:
    """Get workspace alias completions."""
    aliases: list[str] = []

    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        aliases.extend(config.workspace_selectors.keys())
    except Exception:
        pass

    return sorted(set(aliases))


def get_project_name_completions() -> list[CompletionItem]:
    """Get project name completions from catalog."""
    completions = []

    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)

        # From project catalog
        for project_id, metadata in config.project_catalog.items():
            name = metadata.get("name", project_id)
            completions.append(
                CompletionItem(
                    project_id,
                    help=f"{name[:40]}..." if len(name) > 40 else name,
                )
            )

        # From project aliases
        for alias, project_id in config.projects.items():
            completions.append(
                CompletionItem(
                    alias,
                    help=f"Alias for {project_id[:30]}...",
                )
            )

        for alias, selector in config.project_selectors.items():
            name = str(selector.get("name") or "").strip()
            workspace = str(selector.get("workspace") or "").strip()
            target = f"{workspace}/{name}" if workspace else name
            completions.append(
                CompletionItem(
                    alias,
                    help=f"Live selector for {target}",
                )
            )
    except Exception:
        pass

    return completions


def get_resource_spec_completions() -> list[CompletionItem]:
    """Get resource spec completions."""
    # Common resource specs
    specs = [
        CompletionItem("1xH200", help="1x H200 GPU"),
        CompletionItem("4xH200", help="4x H200 GPU (multi-node)"),
        CompletionItem("1xH100", help="1x H100 GPU"),
        CompletionItem("4xH100", help="4x H100 GPU (multi-node)"),
        CompletionItem("1x4090", help="1x RTX 4090 GPU"),
        CompletionItem("4CPU", help="4 CPU cores"),
        CompletionItem("8CPU", help="8 CPU cores"),
    ]

    # Try to load from compute groups
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        for cg in config.compute_groups:
            gpu_type = cg.get("gpu_type", "")
            if gpu_type and gpu_type not in ["H200", "H100", "4090"]:
                specs.append(CompletionItem(f"1x{gpu_type}", help=f"1x {gpu_type} GPU"))
    except Exception:
        pass

    return specs


def get_image_name_completions() -> list[str]:
    """Get Docker image name completions."""
    # Common images from defaults
    images = [
        "pytorch",
        "tensorflow",
        "ngc-pytorch",
        "ngc-tensorflow",
    ]

    # Try to load from config
    try:
        config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
        if config.default_image:
            images.append(config.default_image)
        if config.job_image:
            images.append(config.job_image)
        if config.notebook_image:
            images.append(config.notebook_image)
    except Exception:
        pass

    return sorted(set(images))
