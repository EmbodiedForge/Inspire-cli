"""Job create command."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import job_submit, resource_plan
from inspire.cli.utils.common import json_option
from inspire.cli.utils.datasets import (
    DATASET_REFERENCE_HINT,
    MOUNT_PATH_PREFIX,
    DatasetValidationError,
    dataset_mount_path,
    parse_dataset_target,
    validate_dataset_targets,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success, emit_warning
from inspire.cli.logging_setup import redact_text
from inspire.config import Config, ConfigError
from inspire.config.workspaces import WORKSPACE_ALIAS_HELP, select_workspace_id
from inspire.platform.openapi.resources import parse_resource_request
from inspire.platform.web.browser_api.availability.models import (
    GPUAvailability,
    PLACEMENT_AGGREGATE,
    PLACEMENT_CAVEATS,
    placement_basis_for_source,
)
from inspire.platform.web.browser_api.priority import priority_can_preempt
from inspire.platform.web.session import RateLimitError, WebSession


def _complete_workspace(ctx, param, incomplete):
    """Shell completion for workspace aliases."""
    from inspire.cli.completion import get_workspace_alias_completions

    aliases = get_workspace_alias_completions()
    return [alias for alias in aliases if alias.startswith(incomplete)]


def _complete_project(ctx, param, incomplete):
    """Shell completion for project names."""
    from inspire.cli.completion import get_project_name_completions

    projects = get_project_name_completions()
    return [comp for comp in projects if comp.value.startswith(incomplete)]


def _complete_resource(ctx, param, incomplete):
    """Shell completion for resource specs."""
    from inspire.cli.completion import get_resource_spec_completions

    specs = get_resource_spec_completions()
    return [comp for comp in specs if comp.value.startswith(incomplete)]


def run_job_create(
    ctx: Context,
    *,
    name: str,
    resource: Optional[str],
    command: str,
    framework: str,
    priority: int | None,
    max_time: float,
    location: str,
    group: str | None,
    quota: str | None,
    workspace: str | None,
    workspace_id_override: str | None,
    auto: bool,
    image: str | None,
    project: str | None,
    nodes: int,
    fault_tolerant: bool | None,
    fault_tolerance_max_retry: int | None,
    keep_alive_on_success: int | None,
    keep_alive_on_fail: int | None,
    dry_run: bool,
    datasets: tuple[str, ...] = (),
    shm_size: int | None = None,
) -> job_submit.JobSubmission | None:
    """Run the shared job creation flow and return a real submission for compatibility callers."""
    try:
        try:
            dataset_pairs = [parse_dataset_target(raw) for raw in datasets]
        except ValueError as e:
            _handle_error(
                ctx,
                "ValidationError",
                str(e),
                EXIT_VALIDATION_ERROR,
                hint=DATASET_REFERENCE_HINT,
            )
            return

        config, sources = Config.from_files_and_env(require_target_dir=False)

        image_from_cli = image is not None
        resource_from_cli = bool(resource)
        priority_from_cli = priority is not None
        project_from_cli = project is not None
        workspace_from_cli = bool(workspace or workspace_id_override)
        if group and location:
            raise ConfigError("Pass either --group or --location, not both")
        if quota and not group:
            raise ConfigError("--quota requires an exact --group")
        if group:
            location = group
        auto_select_offer = auto and not location

        # Whose number is this? A priority that came from --priority or from the caller's own
        # configuration is theirs: the platform refusing it is news for them, never something
        # to work around. Only a number this CLI supplied itself (the 6 below, or a project cap
        # applied further down) may be replaced by a value the platform takes.
        priority_is_callers_own = priority is not None
        if priority is None:
            priority = config.job_priority
            if priority is None:
                priority = getattr(config, "default_priority", None)
            priority_is_callers_own = priority is not None
            if priority is None:
                priority = 6
        if image is None:
            image = config.job_image or getattr(config, "default_image", None)
        if not resource:
            resource = getattr(config, "job_resource", None) or getattr(
                config, "default_resource", None
            )
        if not resource:
            raise ConfigError(
                "Missing resource specification.\n"
                "Pass --resource or set [job].resource or [defaults].resource in config.toml."
            )

        try:
            requested_gpu_type, requested_gpu_count = parse_resource_request(resource)
        except Exception as e:
            _handle_error(
                ctx,
                "ValidationError",
                f"Invalid resource spec: {e}",
                EXIT_VALIDATION_ERROR,
            )
            return

        selected_workspace_id = (
            select_workspace_id(
                config,
                explicit_workspace_id=workspace_id_override,
                explicit_workspace_name=workspace,
            )
            or config.job_workspace_id
            or config.default_workspace_id
        )
        if not selected_workspace_id:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace selected. Pass --workspace/--workspace-id or set "
                "[job].workspace_id or [defaults].workspace_id.",
                EXIT_CONFIG_ERROR,
            )
            return

        dataset_info: Optional[list[dict[str, str]]] = None
        dataset_session: Optional[WebSession] = None
        if dataset_pairs:
            # Whether a version mounts at all is the platform's decision per workspace, and only a
            # web session can ask it - the same console pre-flight the notebook create runs.
            dataset_session = require_web_session(
                ctx,
                hint=(
                    "Checking and mounting datasets needs an authenticated web session: "
                    "run `inspire init`."
                ),
                account=config.account_alias,
            )
            try:
                dataset_info = validate_dataset_targets(
                    dataset_pairs, selected_workspace_id, dataset_session
                )
            except DatasetValidationError as e:
                _handle_error(
                    ctx,
                    "APIError",
                    f"Refusing to create job: {e}",
                    EXIT_API_ERROR,
                    hint=(
                        "Check what will mount: "
                        f"inspire dataset check {e.dataset_code}@{e.version_code} "
                        f"--workspace-id {selected_workspace_id}"
                    ),
                )
                return
            except Exception as e:  # noqa: BLE001
                _handle_error(ctx, "APIError", f"Failed to validate datasets: {e}", EXIT_API_ERROR)
                return

            if not ctx.json_output and not dry_run:
                for entry in dataset_info:
                    click.echo(
                        f"Using dataset: {entry['dataset_id']}@{entry['version_id']} -> "
                        f"{dataset_mount_path(entry['dataset_id'], entry['version_id'])}"
                    )

        try:
            selected, fallback_msg = job_submit.select_project_for_workspace(
                config,
                workspace_id=selected_workspace_id,
                requested=project,
            )
        except ValueError as e:
            error_type = "QuotaExceeded" if "over quota" in str(e) else "ValidationError"
            _handle_error(ctx, error_type, str(e), EXIT_CONFIG_ERROR)
            return

        selected_project_id = selected.project_id
        requested_priority = priority

        # Cap priority to the selected project's max priority
        if selected.priority_name:
            try:
                max_priority = int(selected.priority_name)
                if priority is not None and priority > max_priority:
                    if not ctx.json_output:
                        emit_info(
                            ctx,
                            f"Capping priority {priority} → {max_priority} "
                            f"(max for project '{selected.name}')",
                        )
                    priority = max_priority
            except ValueError:
                pass
        # A cap is the CLI changing the caller's number, however it arrived, so from here on
        # the value being submitted is one the CLI invented.
        priority_is_callers_own = priority_is_callers_own and priority == requested_priority
        capped_by = ["project priority"] if priority != requested_priority else []

        try:
            launch_plan = resource_plan.build_job_resource_plan(
                workspace_id=selected_workspace_id,
                config=config,
                group=group,
                exact_group=bool(group),
                location=location if not group else None,
                resource=resource,
                quota=quota,
                priority=int(priority),
                priority_from_caller=priority_is_callers_own,
                include_capacity=True,
            )
            offer_selection = resource_plan.select_job_offer(
                launch_plan,
                gpu_type=requested_gpu_type.value,
                gpu_count=requested_gpu_count,
                nodes=nodes,
                auto=auto_select_offer,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
        priority_plan = offer_selection.planned.priority_plan
        if priority_plan is None:
            raise RuntimeError("The selected Scheduling Offer has no priority plan")
        priority_effective = priority_plan.priority
        if priority_effective != priority:
            capped_by.append("Scheduling Offer ceiling")

        auto_selected = offer_selection.availability
        auto_selected_offer = offer_selection.offer
        if auto_select_offer:
            location = auto_selected_offer.logic_compute_group_id
            if auto_selected is None:
                if not ctx.json_output:
                    required_gpu_count = requested_gpu_count * max(1, int(nodes))
                    emit_warning(
                        ctx,
                        f"No {requested_gpu_type.value} group currently has "
                        f"{required_gpu_count} free GPUs; submitting to a valid "
                        "Scheduling Offer so the platform can queue the job.",
                    )
            elif not ctx.json_output:
                best = auto_selected
                selected_group_name = auto_selected_offer.logic_compute_group_name
                source = getattr(best, "selection_source", "")
                if source.startswith("nodes") and getattr(best, "free_nodes", 0):
                    emit_info(
                        ctx,
                        "Auto-selected: "
                        f"{selected_group_name}, {best.free_nodes} full nodes free "
                        f"({best.available_gpus} GPUs)",
                    )
                elif source.startswith("nodes") and getattr(best, "low_priority_nodes_estimate", 0):
                    emit_info(
                        ctx,
                        "Auto-selected: "
                        f"{selected_group_name}, {best.free_nodes} full nodes free "
                        f"(+{best.low_priority_nodes_estimate} low-priority node estimate)",
                    )
                else:
                    preempt_note = (
                        f" (+{best.low_priority_gpus} preemptible)"
                        if getattr(best, "low_priority_gpus", 0) > 0
                        else ""
                    )
                    emit_info(
                        ctx,
                        f"Auto-selected: {selected_group_name}, "
                        f"{best.available_gpus} GPUs available{preempt_note}",
                    )
                fallback_reason = getattr(best, "fallback_reason", "")
                if fallback_reason:
                    emit_info(ctx, f"Node-level availability fallback: {fallback_reason}")

        if fault_tolerance_max_retry is not None:
            if fault_tolerant is False:
                raise ConfigError(
                    "--fault-tolerance-max-retry cannot be used with --no-fault-tolerant"
                )
            fault_tolerant = True

        # Low-priority jobs are preemptible, so enable the platform's native
        # restart loop by default unless the user explicitly disables it. The judgement is made
        # on the priority that will actually be submitted: a request the workspace caps into 1-3
        # runs preemptibly and needs the restart loop, whatever number was asked for.
        is_low_priority, auto_fault_tolerance = job_submit.resolve_fault_tolerance(
            selected,
            fault_tolerant,
            task_priority=priority_effective,
        )

        if not ctx.json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            if is_low_priority:
                restart_note = " and auto-restarted" if auto_fault_tolerance else ""
                click.echo(
                    f"Using project: {selected.name} "
                    f"(low priority — job may be preempted{restart_note})"
                )
            else:
                click.echo(f"Using project: {selected.name}{selected.get_quota_status()}")

        if not ctx.json_output and auto:
            summary_snapshots = [
                (
                    replace(snapshot, low_priority_gpus=0)
                    if snapshot.low_priority_gpus is None
                    else snapshot
                )
                for snapshot in launch_plan.gpu_snapshots()
            ]
            summary = job_submit.format_gpu_availability_summary(
                summary_snapshots, requested_gpu_type.value
            )
            if summary:
                emit_info(ctx, summary)

        try:
            prepared = job_submit.prepare_training_job_launch(
                config=config,
                name=name,
                command=command,
                resource=resource,
                framework=framework,
                location=location,
                project_id=selected_project_id,
                workspace_id=selected_workspace_id,
                image=image,
                shm_size=shm_size,
                priority=priority,
                priority_from_caller=priority_is_callers_own,
                priority_plan=priority_plan,
                nodes=nodes,
                max_time_hours=max_time,
                project_name=selected.name,
                auto_fault_tolerance=auto_fault_tolerance,
                fault_tolerance_max_retry=fault_tolerance_max_retry,
                keep_alive_on_success=keep_alive_on_success,
                keep_alive_on_fail=keep_alive_on_fail,
                exact_quota=quota,
                exact_group=bool(group),
                auto_select_offer=auto_select_offer,
                selected_offer=auto_selected_offer,
                dataset_info=dataset_info,
            )
        except ValueError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return

        if not dry_run:
            for warning in dict.fromkeys([*prepared.warnings, *launch_plan.warnings]):
                emit_warning(ctx, warning)

        if dry_run:
            preview = build_launch_preview(
                config=config,
                sources=sources,
                prepared=prepared,
                workspace=workspace,
                workspace_id=selected_workspace_id,
                project=selected,
                resource=resource,
                priority=priority,
                image=image,
                image_from_cli=image_from_cli,
                resource_from_cli=resource_from_cli,
                group_from_cli=bool(group),
                quota_from_cli=bool(quota),
                priority_from_cli=priority_from_cli,
                priority_capped_by=" and ".join(capped_by),
                project_from_cli=project_from_cli,
                workspace_from_cli=workspace_from_cli,
                auto_selected=auto_selected,
                launch_plan=launch_plan,
                offer_selection=offer_selection,
                requested_priority=requested_priority,
            )
            try:
                existing = job_submit.find_active_job_by_name(
                    name=name, workspace_id=selected_workspace_id
                )
            except job_submit.ActiveJobLookupIncomplete as exc:
                preview["warnings"].append(
                    f"Could not complete the active-job name check for '{name}': {exc}. "
                    "The real create will refuse until the lookup can enumerate all active jobs."
                )
                existing = None
            except Exception as exc:  # noqa: BLE001
                emit_warning(ctx, f"Could not check for an active job named '{name}': {exc}")
                existing = None
            if existing is not None:
                preview["warnings"].append(
                    f"A job named '{name}' is already active: {existing.job_id} "
                    f"(status: {existing.status}) — the real create would refuse."
                )
            if ctx.json_output:
                click.echo(json_formatter.format_json(preview))
            else:
                render_launch_preview(preview)
            return None

        try:
            existing = job_submit.find_active_job_by_name(
                name=name, workspace_id=selected_workspace_id
            )
        except job_submit.ActiveJobLookupIncomplete as exc:
            _handle_error(
                ctx,
                "ActiveJobLookupIncomplete",
                f"Could not complete the active-job name check for '{name}': {exc}",
                EXIT_API_ERROR,
                hint=(
                    "Retry when the job list can be enumerated; refusing to create a name that "
                    "may already belong to an active job."
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001
            emit_warning(ctx, f"Could not check for an active job named '{name}': {exc}")
            existing = None
        if existing is not None:
            _handle_error(
                ctx,
                "DuplicateJobName",
                f"A job named '{name}' is already active: {existing.job_id} "
                f"(status: {existing.status})",
                EXIT_VALIDATION_ERROR,
                hint=(
                    "Use a different --name, or stop the existing job first: "
                    f"inspire job stop {existing.job_id}"
                ),
                data={"existing_job_id": existing.job_id, "existing_status": existing.status},
            )

        submission = job_submit.submit_prepared_training_job(
            config=config,
            prepared=prepared,
            name=name,
            session=dataset_session,
        )

        if submission.reconciled and not ctx.json_output:
            emit_warning(
                ctx,
                "The create response was ambiguous, but the accepted job was recovered "
                "from the platform job list.",
            )

        if submission.priority_note:
            emit_warning(ctx, submission.priority_note)

        if dataset_info:
            if not submission.job_id:
                _handle_error(
                    ctx,
                    "APIError",
                    "The platform created the job without naming it, so nothing could be read "
                    "back to confirm the datasets were attached.",
                    EXIT_API_ERROR,
                    hint=(
                        "Find it by name with: inspire job list "
                        f"--workspace-id {selected_workspace_id}"
                    ),
                )
                return
            job_submit.verify_mounted_datasets(
                job_id=submission.job_id,
                dataset_info=dataset_info,
                session=dataset_session,
            )

        wrapped_command = submission.wrapped_command
        result = submission.result

        data = submission.data
        job_id = submission.job_id

        if ctx.json_output:
            payload = _redact(data if data else result)
            click.echo(json_formatter.format_json(payload))
            return submission

        if job_id:
            emit_success(
                ctx,
                payload={"job_id": job_id, "name": name, "status": "created"},
                text=f"Job created: {job_id}",
            )
            click.echo(f"\nName:     {name}")
            click.echo(f"Resource: {resource}")
            effective_priority = (
                submission.priority
                if submission.priority is not None
                else prepared.payload.get("task_priority")
            )
            if effective_priority is not None:
                priority_display = str(effective_priority)
                try:
                    if int(effective_priority) <= 3:
                        priority_display = f"{priority_display} (LOW, preemptible)"
                except (TypeError, ValueError):
                    pass
                click.echo(f"Priority: {priority_display}")
            if nodes > 1:
                click.echo(f"Nodes:    {nodes}")
            max_cmd_len = 80
            if len(wrapped_command) > max_cmd_len:
                display_cmd = wrapped_command[:max_cmd_len]
                suffix = " ... (truncated)"
            else:
                display_cmd = wrapped_command
                suffix = ""
            click.echo(f"Command:  {display_cmd}{suffix}")
            click.echo(f"\nCheck status with: inspire job status {job_id}")
            return submission

        if isinstance(result, dict):
            message = result.get("message") or "Job created (no job ID returned)"
            emit_success(ctx, payload=result, text=message)
            if result.get("data"):
                click.echo(str(result["data"]))
        else:
            emit_success(
                ctx, payload={"status": "created"}, text="Job created (no job ID returned)"
            )
            click.echo(str(result))
        return submission

    except RateLimitError as e:
        _handle_error(ctx, "RateLimitError", str(e), EXIT_API_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except job_submit.PriorityRefusedError as e:
        _handle_error(ctx, "PriorityRefused", str(e), EXIT_API_ERROR, hint=e.hint)
    except job_submit.DatasetMountNotAppliedError as e:
        _handle_error(
            ctx,
            "DatasetMountNotApplied",
            str(e),
            EXIT_API_ERROR,
            hint=(
                "The platform mounted none of the requested versions; check "
                f"`inspire job status {e.job_id}` and stop the job before it writes results "
                "against missing input."
            ),
        )
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def build_launch_preview(
    *,
    config: Config,
    sources: dict[str, str],
    prepared: job_submit.PreparedJobLaunch,
    workspace: str | None,
    workspace_id: str,
    project: Any,
    resource: str,
    priority: int,
    image: str | None,
    image_from_cli: bool,
    resource_from_cli: bool,
    group_from_cli: bool,
    quota_from_cli: bool,
    priority_from_cli: bool,
    project_from_cli: bool,
    workspace_from_cli: bool,
    auto_selected: Any = None,
    availability_snapshot: GPUAvailability | None = None,
    launch_plan: resource_plan.JobResourcePlan | None = None,
    offer_selection: resource_plan.JobOfferSelection | None = None,
    requested_priority: int | None = None,
    priority_capped_by: str = "",
) -> dict[str, Any]:
    selection = prepared.resource
    # Read from the payload rather than from the caller: the preview must not be able to describe a
    # body other than the one this prepared launch will submit.
    datasets = prepared.payload.get("dataset_info") or []
    warnings: list[str] = list(prepared.warnings)
    if launch_plan is not None:
        warnings.extend(launch_plan.warnings)
    if requested_priority is None:
        requested_priority = priority
    effective_priority = int(prepared.payload.get("task_priority", priority))
    framework_config = (prepared.payload.get("framework_config") or [{}])[0]
    instance_count = max(1, int(framework_config.get("instance_count", 1) or 1))
    required_gpu_count = selection.gpu_count * instance_count
    # A request in the low-priority band is the one the platform evicts, so capacity
    # held by preemptible jobs is not something this launch can plan on.
    preemptible_usable = priority_can_preempt(effective_priority)
    availability: dict[str, Any] | None = None
    if offer_selection is not None and offer_selection.availability is not None:
        auto_selected = offer_selection.availability
    try:
        if launch_plan is not None:
            snapshots = launch_plan.gpu_snapshots()
        elif availability_snapshot is not None:
            snapshots = [availability_snapshot]
        else:
            snapshots = []
        match = next(
            (item for item in snapshots if item.group_id == selection.logic_compute_group_id),
            None,
        )
        if match is not None:
            # The numbers come from this group-wide node snapshot, while the group
            # itself may have been picked by auto-selection.  Report both: a
            # group-wide count is not a statement that the request schedules.
            placement_owner = match
            selection_source = str(getattr(match, "selection_source", "") or "")
            fallback_reason = str(getattr(match, "fallback_reason", "") or "")
            if (
                auto_selected is not None
                and getattr(auto_selected, "group_id", "") == match.group_id
            ):
                selection_source = (
                    str(getattr(auto_selected, "selection_source", "") or "") or selection_source
                )
                fallback_reason = (
                    str(getattr(auto_selected, "fallback_reason", "") or "") or fallback_reason
                )
                placement_owner = auto_selected
            placement_basis = str(
                getattr(placement_owner, "placement_basis", "")
                or placement_basis_for_source(selection_source)
            )
            if placement_basis == PLACEMENT_AGGREGATE and not fallback_reason:
                fallback_reason = (
                    "Node-level visibility was not checked for this selection; "
                    "using aggregate compute-resource counters"
                )
            availability = {
                "observed_at": (
                    launch_plan.observed_at
                    if launch_plan is not None
                    else datetime.now(timezone.utc).isoformat()
                ),
                "available_gpus": match.available_gpus,
                "low_priority_gpus": match.low_priority_gpus,
                "advisory": True,
                "selection_source": selection_source,
                "placement_basis": placement_basis,
                "placement_caveat": PLACEMENT_CAVEATS.get(
                    placement_basis, PLACEMENT_CAVEATS[PLACEMENT_AGGREGATE]
                ),
                "fallback_reason": fallback_reason,
                "preemptible_usable": preemptible_usable,
            }
            if match.available_gpus < required_gpu_count:
                # Priorities 4+ run non-preemptibly over low-priority work, so a group
                # that is "full" because low-priority jobs are sitting on it is still
                # usable at our priority.  Say which number the queue estimate is about
                # instead of a flat "may queue".
                low_priority_known = match.low_priority_gpus is not None
                preemptible = max(int(match.low_priority_gpus or 0), 0)
                if not preemptible_usable:
                    held_capacity = (
                        f"{preemptible} GPU(s) held by preemptible low-priority jobs"
                        if low_priority_known
                        else "low-priority capacity whose usage is unknown"
                    )
                    warnings.append(
                        f"Current free capacity ({match.available_gpus} idle) is below the "
                        f"request, and priority {effective_priority} cannot take over "
                        f"{held_capacity}; the Job may queue."
                    )
                elif not low_priority_known:
                    warnings.append(
                        f"Current free capacity ({match.available_gpus} idle) is below the "
                        "request; low-priority GPU usage is unknown and is not counted as "
                        "reclaimable capacity, so the Job may queue."
                    )
                elif match.available_gpus + preemptible >= required_gpu_count:
                    warnings.append(
                        f"Only {match.available_gpus} GPU(s) are idle, but {preemptible} more "
                        "are held by preemptible low-priority (priority 1-3) jobs that this "
                        "request's priority can take over; preemption may free capacity, "
                        "but the Job may still queue."
                    )
                else:
                    warnings.append(
                        f"Current free capacity ({match.available_gpus} idle + {preemptible} "
                        "preemptible) is below the request; the Job may queue."
                    )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Availability signal unavailable: {exc}")
    return {
        "kind": "job",
        "mutation": "skipped",
        "account_context": {
            "alias": config.account_alias,
            "username": config.username,
            "base_url": config.base_url,
        },
        "intent": {
            "workspace": workspace or workspace_id,
            "project": project.name,
            "group": selection.logic_compute_group_name,
            "quota": {
                "gpu": selection.gpu_count,
                "cpu": selection.cpu_count,
                "memory_gib": selection.memory_size_gib,
            },
            "resource": resource,
            "image": image or prepared.image.image,
            "shm_gib": framework_config.get("shm_gi"),
            "priority": effective_priority,
            "priority_requested": requested_priority,
            "priority_effective": effective_priority,
            "datasets": [
                f"{entry['dataset_id']}@{entry['version_id']} -> "
                f"{dataset_mount_path(entry['dataset_id'], entry['version_id'])}"
                for entry in datasets
            ],
        },
        "sources": {
            "workspace": (
                "cli" if workspace_from_cli else sources.get("job_workspace_id", "default")
            ),
            "project": (
                "cli" if project_from_cli else sources.get("project_order", "live selection")
            ),
            "group": "cli" if group_from_cli else "selection policy",
            "quota": "cli" if quota_from_cli else "resource resolution",
            "resource": "cli" if resource_from_cli else sources.get("job_resource", "default"),
            "image": "cli" if image_from_cli else sources.get("job_image", "default"),
            "priority": (
                f"{'cli' if priority_from_cli else sources.get('job_priority', 'default')}"
                + (f", capped by {priority_capped_by}" if priority_capped_by else "")
            ),
        },
        "resolved": {
            "workspace_id": workspace_id,
            "project_id": project.project_id,
            "logic_compute_group_id": selection.logic_compute_group_id,
            "quota_id": selection.quota_id,
            "image": prepared.image.image,
            "image_type": prepared.image.image_type,
        },
        "availability": availability,
        "platform_request": _redact(prepared.payload),
        "warnings": warnings,
    }


def render_launch_preview(preview: dict[str, Any]) -> None:
    intent = preview["intent"]
    quota = intent["quota"]
    click.echo("Launch Preview\n")
    account = preview["account_context"]
    click.echo(f"Account:    {account['alias'] or '(legacy)'}")
    click.echo(f"Username:   {account['username']}")
    click.echo(f"Workspace:  {intent['workspace']}")
    click.echo(f"Project:    {intent['project']}")
    click.echo("Workload:   Job")
    click.echo(f"Group:      {intent['group']}")
    click.echo(f"Quota:      {quota['gpu']} GPU / {quota['cpu']} CPU / {quota['memory_gib']} GiB")
    click.echo(f"Image:      {intent['image']}")
    if intent.get("shm_gib") is not None:
        click.echo(f"SHM:        {intent['shm_gib']} GiB")
    click.echo(f"Priority:   {intent['priority']}")
    for dataset in intent.get("datasets") or []:
        click.echo(f"Dataset:    {dataset}")
    availability = preview.get("availability")
    if availability:
        click.echo(f"Capacity:   {availability['available_gpus']} GPUs free (advisory)")
        click.echo(
            f"Placement:  {availability['placement_basis']} "
            f"(selection source: {availability['selection_source']})"
        )
        caveat = availability.get("placement_caveat")
        if caveat:
            click.echo(f"Note:       {caveat}")
        fallback_reason = availability.get("fallback_reason")
        if fallback_reason:
            click.echo(f"Probe:      {fallback_reason}")
    for warning in preview.get("warnings", []):
        click.echo(f"Warning:    {warning}")
    click.echo("Mutation:   skipped")


@click.command("create")
@click.option("--name", "-n", required=True, help="Job name")
@click.option(
    "--resource",
    "-r",
    required=False,
    shell_complete=_complete_resource,
    help="Resource spec (e.g., '4xH200') (default from config [job].resource or [defaults].resource)",
)
@click.option("--command", "-c", required=True, help="Start command")
@click.option("--framework", default="pytorch", help="Training framework (default: pytorch)")
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help="Task priority 1-10 (default from config [job].priority or [defaults].priority or 6). "
    "The project and the selected Scheduling Offer can cap it using their own limits. "
    "A cap reports the value asked for, the effective value, and its evidence; unknown or "
    "conflicting offer evidence never borrows another offer's ceiling.",
)
@click.option("--max-time", type=float, default=100.0, help="Max runtime in hours (default: 100)")
@click.option("--location", help="Preferred datacenter location")
@click.option("--group", help="Exact compute-group name or ID")
@click.option("--quota", help="Exact live quota row as GPU,CPU,MEM (for example 4,80,900)")
@click.option(
    "--workspace",
    shell_complete=_complete_workspace,
    help=WORKSPACE_ALIAS_HELP,
)
@click.option(
    "--workspace-id",
    "workspace_id_override",
    help="Workspace ID override (escape hatch; highest precedence)",
)
@click.option(
    "--auto/--no-auto",
    default=True,
    help="Auto-select best location based on node availability (default: auto)",
)
@click.option(
    "--image",
    default=None,
    help="Custom Docker image (default from config [job].image or [defaults].image)",
)
@click.option(
    "--shm",
    "shm_size",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Shared memory size in GB (overrides [job].shm_size / INSPIRE_JOB_SHM_SIZE and "
        "[defaults].shm_size / INSPIRE_SHM_SIZE; default 200, capped to the selected quota memory)"
    ),
)
@click.option(
    "--project",
    "-p",
    shell_complete=_complete_project,
    default=None,
    help="Project name or ID (default from config [job].project_id or [defaults].project_order)",
)
@click.option(
    "--nodes",
    type=click.IntRange(min=1),
    default=1,
    help="Number of nodes for multi-node training (default: 1)",
)
@click.option(
    "--fault-tolerant/--no-fault-tolerant",
    default=None,
    help="Auto-restart on failure/preemption (auto-enabled for LOW projects or priority < 4)",
)
@click.option(
    "--fault-tolerance-max-retry",
    type=click.IntRange(1, 2000),
    default=None,
    help="Maximum platform runs when fault tolerance is enabled (default: 100)",
)
@click.option(
    "--keep-alive-on-success",
    type=click.IntRange(1, 7200),
    default=None,
    help="Keep the job pod alive after success for N seconds (max 7200).",
)
@click.option(
    "--keep-alive-on-fail",
    type=click.IntRange(1, 7200),
    default=None,
    help="Keep the job pod alive after failure for N seconds (max 7200).",
)
@click.option(
    "--dataset",
    "datasets",
    multiple=True,
    metavar="CODE@VERSION",
    help=(
        "Mount a data-square dataset version in the container, repeatable "
        "(e.g. --dataset pointworld@behavior). The version is checked against the target "
        "workspace before the job is created, and the created job is read back to confirm "
        f"it really mounted. Mounted at {MOUNT_PATH_PREFIX}/CODE/VERSION."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Prepare and display the exact launch without creating a job",
)
@json_option
@pass_context
def create(
    ctx: Context,
    name: str,
    resource: Optional[str],
    command: str,
    framework: str,
    priority: Optional[int],
    max_time: float,
    location: str,
    group: Optional[str],
    quota: Optional[str],
    workspace: Optional[str],
    workspace_id_override: Optional[str],
    auto: bool,
    image: Optional[str],
    shm_size: Optional[int],
    project: Optional[str],
    nodes: int,
    fault_tolerant: Optional[bool],
    fault_tolerance_max_retry: Optional[int],
    keep_alive_on_success: Optional[int],
    keep_alive_on_fail: Optional[int],
    dry_run: bool,
    datasets: tuple[str, ...] = (),
    json_output: bool = False,
) -> None:
    """Create a new training job.

    The command runs from the configured remote runtime cwd when one is set:
    [remote_runtime].cwd, or [defaults].target_dir / INSPIRE_TARGET_DIR. That path is optional and
    is not a log destination. Logs are platform-side: `inspire job logs <job_id>` reads the
    container output from the platform log API; the CLI writes no local job log file.

    \b
    Datasets (--dataset CODE@VERSION, repeatable):
      - Mounts a published data-square dataset version into the job container. List what exists
        with `inspire dataset list` / `inspire dataset versions CODE`.
      - Each version is checked against the target workspace with ValidateDataset before the job
        is created, and the created job is read back with GetJob to confirm it actually mounted.
        Either step failing stops the command rather than leaving a job running on missing data.
      - Inside the container a version appears at /inspire/dataset/CODE/VERSION, regardless of
        where the platform stores it.

    \b
    Examples:
        inspire job create --name "pr-123" --resource "4xH200" --command "cd /path/to/code && bash train.sh"
        inspire job create -n test -r H200 -c "python train.py" --priority 9
        inspire job create -n test -r 4xH200 -c "python train.py" --no-auto
        inspire job create -n test -r 1xH200 -c "python train.py" --dataset pointworld@behavior
        inspire job logs <job_id>
    """

    json_output = resolve_json_output(ctx, json_output)
    run_job_create(
        ctx,
        name=name,
        resource=resource,
        command=command,
        framework=framework,
        priority=priority,
        max_time=max_time,
        location=location,
        group=group,
        quota=quota,
        workspace=workspace,
        workspace_id_override=workspace_id_override,
        auto=auto,
        image=image,
        shm_size=shm_size,
        project=project,
        nodes=nodes,
        fault_tolerant=fault_tolerant,
        fault_tolerance_max_retry=fault_tolerance_max_retry,
        keep_alive_on_success=keep_alive_on_success,
        keep_alive_on_fail=keep_alive_on_fail,
        dry_run=dry_run,
        datasets=datasets,
    )
