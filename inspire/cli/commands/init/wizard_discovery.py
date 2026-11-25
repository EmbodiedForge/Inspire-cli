"""Discovery integration for wizard mode."""

from __future__ import annotations

from typing import Any

import click


def _coerce_project_field(project: Any, field_name: str) -> Any:
    if isinstance(project, dict):
        return project.get(field_name)
    return getattr(project, field_name, None)


def run_discovery_for_wizard(
    username: str,
    password: str,
    base_url: str,
) -> dict[str, Any] | None:
    """Run read-only discovery and return platform facts for the wizard."""
    try:
        from inspire.platform.web import browser_api as browser_api_module
        from inspire.platform.web import session as web_session_module
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces
        from inspire.platform.web.browser_api.core import _set_base_url
        from .wizard import _format_project_label

        results: dict[str, Any] = {
            "workspaces": {},
            "workspace_items": [],
            "projects": [],
            "compute_groups": [],
            "success": False,
        }

        click.echo("\n🔍 Discovering your Inspire platform account...")

        try:
            _set_base_url(base_url)
            session = web_session_module.login_with_playwright(
                username,
                password,
                base_url=base_url,
            )
            click.echo("✓ Authenticated successfully")
        except Exception as e:
            click.echo(f"✗ Authentication failed: {e}")
            return None

        click.echo("\n📋 Discovering workspaces...")
        discovered_workspace_ids: list[str] = []
        try:
            workspace_list = try_enumerate_workspaces(session, workspace_id=session.workspace_id)
            results["workspace_items"] = workspace_list
            discovered_workspace_ids = [
                str(ws.get("id") or "").strip()
                for ws in workspace_list
                if isinstance(ws, dict) and str(ws.get("id") or "").strip()
            ]
            click.echo(f"✓ Found {len(workspace_list)} workspace(s)")
            for workspace in workspace_list:
                click.echo(f"  - {workspace.get('name') or '(unnamed)'}: {workspace.get('id')}")
            if workspace_list:
                click.echo(
                    "  Aliases are not assigned automatically; address these by ID or exact name, "
                    "or add your own aliases in config.toml."
                )
        except Exception as e:
            click.echo(f"⚠ Could not discover workspaces: {e}")

        click.echo("\n📁 Discovering projects...")
        workspace_id = str(session.workspace_id or "").strip()
        if not workspace_id and discovered_workspace_ids:
            workspace_id = discovered_workspace_ids[0]
        try:
            projects_data = browser_api_module.list_projects(
                workspace_id=workspace_id,
                session=session,
            )

            for proj in projects_data:
                if isinstance(proj, dict):
                    project_id = str(proj.get("id", "")).strip()
                    name = str(proj.get("name", "Unknown")).strip() or "Unknown"
                    quota_data = proj.get("quota", {})
                    gpu_used = quota_data.get("used", 0)
                    gpu_total = quota_data.get("total", 0)
                else:
                    project_id = str(
                        getattr(proj, "project_id", "") or getattr(proj, "id", "")
                    ).strip()
                    name = str(getattr(proj, "name", "Unknown")).strip() or "Unknown"
                    gpu_used = 0
                    gpu_total = 0

                results["projects"].append(
                    {
                        "id": project_id,
                        "name": name,
                        "quota": {"gpu_used": gpu_used, "gpu_total": gpu_total},
                        "budget": _coerce_project_field(proj, "budget"),
                        "remain_budget": _coerce_project_field(proj, "remain_budget"),
                        "member_remain_budget": _coerce_project_field(proj, "member_remain_budget"),
                        "member_remain_gpu_hours": _coerce_project_field(
                            proj, "member_remain_gpu_hours"
                        ),
                        "gpu_limit": bool(_coerce_project_field(proj, "gpu_limit")),
                        "member_gpu_limit": bool(_coerce_project_field(proj, "member_gpu_limit")),
                        "priority_level": str(_coerce_project_field(proj, "priority_level") or ""),
                        "priority_name": str(_coerce_project_field(proj, "priority_name") or ""),
                    }
                )

            click.echo(f"✓ Found {len(results['projects'])} project(s)")
            for proj in results["projects"][:5]:
                click.echo(f"  - {_format_project_label(proj)}")
            if len(results["projects"]) > 5:
                click.echo(f"  ... and {len(results['projects']) - 5} more")
        except Exception as e:
            click.echo(f"⚠ Could not discover projects: {e}")

        click.echo("\n⚙️  Discovering compute groups...")
        try:
            from inspire.cli.commands.init.discover import (
                _discover_compute_groups,
                _merge_compute_groups,
            )

            merged_groups: list[dict[str, Any]] = []
            for group_workspace_id in discovered_workspace_ids or [workspace_id]:
                groups = _discover_compute_groups(
                    browser_api_module=browser_api_module,
                    session=session,
                    workspace_id=group_workspace_id,
                )
                for group in groups:
                    group.setdefault("workspace_ids", [])
                    if group_workspace_id not in group["workspace_ids"]:
                        group["workspace_ids"].append(group_workspace_id)
                merged_groups = _merge_compute_groups(merged_groups, groups)

            results["compute_groups"] = merged_groups
            click.echo(f"✓ Found {len(results['compute_groups'])} compute group(s)")
            for group in results["compute_groups"][:5]:
                click.echo(f"  - {group['name']} ({group.get('gpu_type') or 'unknown'})")
            if len(results["compute_groups"]) > 5:
                click.echo(f"  ... and {len(results['compute_groups']) - 5} more")
        except Exception as e:
            click.echo(f"⚠ Could not discover compute groups: {e}")

        results["success"] = True
        click.echo("\n✅ Discovery complete!")
        return results
    except ImportError as e:
        click.echo(f"✗ Discovery requires additional dependencies: {e}")
        click.echo("  Try: uv pip install playwright")
        return None
    except Exception as e:
        click.echo(f"✗ Discovery failed: {e}")
        return None
