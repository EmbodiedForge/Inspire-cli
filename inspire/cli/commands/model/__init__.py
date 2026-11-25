"""Model serving commands for inspire-cli.

Commands for deploying and managing model inference servings on inspire platform.

Usage:
    inspire model deploy --name my-model --image vllm:latest --command "..."
    inspire model list
    inspire model stop <serving-id>
    inspire model delete <serving-id>
"""

from __future__ import annotations

import click

from inspire.cli.commands.workload_profile import build_profile_group
from inspire.cli.commands.workload_catalog import (
    build_availability_command,
    build_offers_command,
)
from inspire.platform.web import browser_api

from .model_commands import (
    api_keys_cmd,
    deploy_model_cmd,
    events_model_cmd,
    instances_model_cmd,
    list_models_cmd,
    logs_model_cmd,
    metrics_model_cmd,
    scale_model_cmd,
    serving_models_cmd,
    start_model_cmd,
    status_model_cmd,
    stop_model_cmd,
    delete_model_cmd,
    update_model_cmd,
    wait_model_cmd,
    exec_model_cmd,
    validate_domain_model_cmd,
    versions_model_cmd,
)


@click.group()
def model():
    """Deploy and manage model inference servings.

    Deploy models as inference services on inspire platform with specified
    GPU resources and configurations.

    \b
    Examples:
        inspire model deploy --name glm5-serve \\
            --image sglang:nightly-dev-cu13-202602 \\
            --command "python -m sglang.launch_server --model-path ..." \\
            --gpu-type NVIDIA_H200 --gpu-count 8

        inspire model list
        inspire model stop serving-abc-123
        inspire model delete serving-abc-123
    """
    pass


model.add_command(deploy_model_cmd)
model.add_command(list_models_cmd)
model.add_command(status_model_cmd)
model.add_command(wait_model_cmd)
model.add_command(start_model_cmd)
model.add_command(stop_model_cmd)
model.add_command(scale_model_cmd)
model.add_command(logs_model_cmd)
model.add_command(metrics_model_cmd)
model.add_command(events_model_cmd)
model.add_command(instances_model_cmd)
model.add_command(exec_model_cmd)
model.add_command(versions_model_cmd)
model.add_command(serving_models_cmd)
model.add_command(update_model_cmd)
model.add_command(validate_domain_model_cmd)
model.add_command(api_keys_cmd)
model.add_command(delete_model_cmd)
model.add_command(build_profile_group("model"))
model.add_command(
    build_offers_command("model", lambda **kwargs: browser_api.list_model_offers(**kwargs))
)
model.add_command(
    build_availability_command("model", lambda **kwargs: browser_api.list_model_offers(**kwargs))
)


__all__ = ["model"]
