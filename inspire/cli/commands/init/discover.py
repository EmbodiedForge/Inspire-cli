"""Discovery mode: probe workspaces, projects, compute groups, and shared paths."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    Config,
)

from .env_detect import _redact_token_like_text
from .toml_helpers import _toml_dumps


def _slugify_alias(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text


def _make_unique_alias(alias: str, used: set[str]) -> str:
    base = alias
    counter = 2
    while alias in used:
        alias = f"{base}-{counter}"
        counter += 1
    used.add(alias)
    return alias


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_global_user_dir(path: str, *, account_key: str | None) -> str | None:
    path = str(path or "").strip()
    if not path:
        return None

    if account_key:
        marker = f"/global_user/{account_key}"
        idx = path.find(marker)
        if idx != -1:
            return path[: idx + len(marker)]

    match = re.search(r"(/.*?/global_user/[^/]+)", path)
    if match:
        return match.group(1)
    return None


def _derive_shared_path_group(path: str, *, account_key: str | None) -> str | None:
    path = str(path or "").strip()
    if not path:
        return None

    global_user_dir = _extract_global_user_dir(path, account_key=account_key)
    if global_user_dir:
        if "/project/" in global_user_dir:
            root_match = re.search(r"(?P<root>/.*?)/project/", global_user_dir)
            if root_match and "/global_user/" in global_user_dir:
                root = root_match.group("root").rstrip("/")
                user_dir = global_user_dir.split("/global_user/", 1)[1].split("/", 1)[0].strip()
                if user_dir:
                    return f"{root}/global_user/{user_dir}"
        return global_user_dir

    # Heuristic: many workdir paths are under a per-volume project root like:
    #   /inspire/hdd/project/.../<user-dir>
    # The shared filesystem root for the account is typically:
    #   /inspire/hdd/global_user/<user-dir>
    # When we can infer the user directory from the workdir, prefer grouping by
    # the derived global_user path so fallback project selection doesn't cross
    # volume boundaries.
    if "/project/" in path:
        root_match = re.search(r"(?P<root>/.*?)/project/", path)
        if root_match:
            root = root_match.group("root").rstrip("/")

            user_dir = ""
            segments = [seg for seg in path.split("/") if seg]
            if account_key:
                for seg in reversed(segments):
                    if account_key in seg:
                        user_dir = seg
                        break

            if not user_dir:
                remainder_match = re.search(r"/project/[^/]+(?P<rest>/.*)?$", path)
                rest = remainder_match.group("rest") if remainder_match else ""
                rest_segments = [seg for seg in (rest or "").split("/") if seg]
                if rest_segments:
                    if rest_segments[0] == "global_user" and len(rest_segments) >= 2:
                        user_dir = rest_segments[1]
                    else:
                        user_dir = rest_segments[0]

            if user_dir:
                return f"{root}/global_user/{user_dir}"

    match = re.search(r"(/.*?/project/[^/]+)", path)
    if match:
        return match.group(1)

    return None


def _load_ssh_public_key(pubkey_path: str | None) -> str:
    candidates: list[Path]

    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if path.exists():
            key = path.read_text(encoding="utf-8", errors="ignore").strip()
            if key:
                return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


def _select_probe_cpu_compute_group_id(compute_groups: list[dict[str, Any]]) -> str | None:
    gpu_name_tokens = (
        "H200",
        "H100",
        "A100",
        "A800",
        "H800",
        "4090",
        "A6000",
        "V100",
        "T4",
        "L40",
        "RTX",
        "GPU",
    )

    def _group_id(group: dict[str, Any]) -> str:
        return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()

    def _looks_gpu_like(group: dict[str, Any]) -> bool:
        if group.get("gpu_type_stats"):
            return True
        gpu_type = str(group.get("gpu_type") or "").strip()
        if gpu_type:
            return True
        name = str(group.get("name") or "").upper()
        return any(token in name for token in gpu_name_tokens)

    # Strong preference: groups explicitly labeled as CPU.
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        if _looks_gpu_like(group):
            continue
        name = str(group.get("name") or "").upper()
        if "CPU" not in name:
            continue
        group_id = _group_id(group)
        if group_id:
            return group_id

    # Next: any group that does not look GPU-like.
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        if _looks_gpu_like(group):
            continue
        group_id = _group_id(group)
        if group_id:
            return group_id

    # Last resort: pick the first group we can.
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        group_id = _group_id(group)
        if group_id:
            return group_id

    return None


def _select_probe_cpu_quota(schedule: dict[str, Any]) -> tuple[str, int, int]:
    quota_list: Any = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json.loads(quota_list) if quota_list else []
    if not isinstance(quota_list, list):
        quota_list = []

    cpu_quotas = [q for q in quota_list if isinstance(q, dict) and q.get("gpu_count", 0) == 0]
    selected = None
    for quota in cpu_quotas:
        cpu_count = quota.get("cpu_count")
        if cpu_count is None:
            continue
        if selected is None or cpu_count < selected.get("cpu_count", 0):
            selected = quota

    if selected is None and cpu_quotas:
        selected = cpu_quotas[0]

    quota_id = str((selected or {}).get("id") or "").strip()
    cpu_count = int((selected or {}).get("cpu_count") or 4)
    memory_size = int((selected or {}).get("memory_size") or 32)
    return quota_id, cpu_count, memory_size


def _select_probe_image(images: list[object], *, preferred: str | None = None) -> object | None:
    if not images:
        return None

    preferred_text = str(preferred or "").strip().lower()
    if preferred_text:
        for img in images:
            name = str(getattr(img, "name", "") or "").lower()
            url = str(getattr(img, "url", "") or "").lower()
            image_id = str(getattr(img, "image_id", "") or "").strip()
            if preferred_text in name or preferred_text in url or preferred == image_id:
                return img

    for img in images:
        name = str(getattr(img, "name", "") or "").lower()
        if "pytorch" in name:
            return img
    return images[0]


def _build_shared_path_probe_command(account_key: str) -> str:
    import shlex

    account = shlex.quote(account_key)
    return (
        f"INSPIRE_ACCOUNT_KEY={account} "
        'PYTHON_BIN="$(command -v python3 || command -v python)" && '
        '"$PYTHON_BIN" - <<PY\n'
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import re\n"
        "\n"
        "account = os.environ.get('INSPIRE_ACCOUNT_KEY', '').strip()\n"
        "pwd = str(pathlib.Path().resolve())\n"
        "home = os.path.expanduser('~')\n"
        "\n"
        "found = ''\n"
        "\n"
        "def pick_from_global_user(global_user_dir: pathlib.Path) -> str:\n"
        "    if not account or not global_user_dir.is_dir():\n"
        "        return ''\n"
        "    direct = global_user_dir / account\n"
        "    if direct.is_dir():\n"
        "        return str(direct)\n"
        "    try:\n"
        "        children = list(global_user_dir.iterdir())[:200]\n"
        "    except Exception:\n"
        "        return ''\n"
        "    candidates = []\n"
        "    for child in children:\n"
        "        if not child.is_dir():\n"
        "            continue\n"
        "        name = child.name\n"
        "        if name.endswith(account):\n"
        "            candidates.append(child)\n"
        "        elif account in name:\n"
        "            candidates.append(child)\n"
        "    if not candidates:\n"
        "        return ''\n"
        "    candidates.sort(key=lambda p: (not p.name.endswith(account), len(p.name)))\n"
        "    return str(candidates[0])\n"
        "\n"
        "bases = [pathlib.Path('/inspire'), pathlib.Path('/train'), pathlib.Path('/shared'), pathlib.Path('/mnt'), pathlib.Path('/data')]\n"
        "for base in bases:\n"
        "    if not base.is_dir():\n"
        "        continue\n"
        "    found = pick_from_global_user(base / 'global_user')\n"
        "    if found:\n"
        "        break\n"
        "    try:\n"
        "        for child in list(base.iterdir())[:60]:\n"
        "            if not child.is_dir():\n"
        "                continue\n"
        "            found = pick_from_global_user(child / 'global_user')\n"
        "            if found:\n"
        "                break\n"
        "        if found:\n"
        "            break\n"
        "    except Exception:\n"
        "        continue\n"
        "\n"
        "def extract(value: str) -> str:\n"
        "    if account and f'/global_user/{account}' in value:\n"
        "        head = value.split(f'/global_user/{account}', 1)[0]\n"
        "        return head + f'/global_user/{account}'\n"
        "    match = re.search(r'(/.*?/global_user/[^/]+)', value)\n"
        "    return match.group(1) if match else ''\n"
        "\n"
        "if not found:\n"
        "    for value in (pwd, home):\n"
        "        extracted = extract(value)\n"
        "        if extracted:\n"
        "            found = extracted\n"
        "            break\n"
        "\n"
        "print(json.dumps({'account': account, 'pwd': pwd, 'home': home, 'global_user_dir': found}, ensure_ascii=False))\n"
        "PY\n"
    )


def _probe_project_shared_path_group(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    account_key: str,
    project_id: str,
    project_name: str,
    project_alias: str,
    ssh_public_key: str,
    ssh_runtime,  # noqa: ANN001
    logic_compute_group_id: str,
    quota_id: str,
    cpu_count: int,
    memory_size: int,
    image_id: str,
    image_url: str,
    shm_size: int,
    task_priority: int,
    keep_notebook: bool,
    timeout: int,
) -> dict[str, Any]:
    from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
    from inspire.bridge.tunnel.ssh_exec import run_ssh_command

    result: dict[str, Any] = {
        "notebook_id": None,
        "shared_path_group": None,
        "probe_data": None,
        "probe_error": None,
    }

    timeout = max(60, int(timeout))

    notebook_id: str | None = None
    try:
        name = f"insp-probe-{project_alias}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        resource_spec_price = {
            "cpu_type": "",
            "cpu_count": int(cpu_count),
            "gpu_type": "",
            "gpu_count": 0,
            "memory_size_gib": int(memory_size),
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": quota_id,
        }

        created = browser_api_module.create_notebook(
            name=name,
            project_id=project_id,
            project_name=project_name,
            image_id=image_id,
            image_url=image_url,
            logic_compute_group_id=logic_compute_group_id,
            quota_id=quota_id,
            gpu_type="",
            gpu_count=0,
            cpu_count=int(cpu_count),
            memory_size=int(memory_size),
            shared_memory_size=int(shm_size),
            auto_stop=True,
            workspace_id=workspace_id,
            session=session,
            task_priority=int(task_priority),
            resource_spec_price=resource_spec_price,
        )
        notebook_id = str((created or {}).get("notebook_id") or "").strip() or None
        result["notebook_id"] = notebook_id
        if not notebook_id:
            result["probe_error"] = "Notebook create succeeded but did not return notebook_id"
            return result

        browser_api_module.wait_for_notebook_running(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
        )

        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
            session=session,
            headless=True,
            timeout=min(timeout, 600),
        )

        bridge = BridgeProfile(
            name="probe",
            proxy_url=proxy_url,
            ssh_user="root",
            ssh_port=22222,
            has_internet=True,
        )
        tunnel_config = TunnelConfig(bridges={"probe": bridge}, default_bridge="probe")

        command = _build_shared_path_probe_command(account_key=account_key)

        last_error: str | None = None
        completed = None
        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            remaining = max(0.0, deadline - time.monotonic())
            per_attempt_timeout = max(10, min(60, int(remaining) if remaining else 10))

            try:
                completed = run_ssh_command(
                    command,
                    config=tunnel_config,
                    timeout=per_attempt_timeout,
                    capture_output=True,
                    check=False,
                    quiet_proxy=True,
                )
                if completed.returncode == 0:
                    break
                last_error = (completed.stderr or "").strip() or (completed.stdout or "").strip()
            except Exception as e:
                last_error = _redact_token_like_text(str(e))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            pause = min(20.0, 2.0 + (attempt * 1.5))
            time.sleep(min(pause, remaining))

        if completed is None or completed.returncode != 0:
            summary = (last_error or "SSH probe failed").strip()
            result["probe_error"] = _redact_token_like_text(summary)[:2000]
            return result

        stdout = completed.stdout or ""
        probe_data = None
        for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                probe_data = json.loads(line)
                break
            except Exception:
                continue

        result["probe_data"] = probe_data
        if isinstance(probe_data, dict):
            global_user_dir = str(probe_data.get("global_user_dir") or "").strip()
            if global_user_dir:
                result["shared_path_group"] = global_user_dir
        return result
    except Exception as e:  # pragma: no cover - network/runtime dependent
        result["probe_error"] = _redact_token_like_text(str(e))
        return result
    finally:
        if notebook_id and not keep_notebook:
            try:
                browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
            except Exception:
                pass


def _discover_workspace_aliases() -> dict[str, str]:
    """Collect workspace alias overrides from environment variables."""
    env_cpu = (os.getenv("INSPIRE_WORKSPACE_CPU_ID") or "").strip()
    env_gpu = (os.getenv("INSPIRE_WORKSPACE_GPU_ID") or "").strip()
    env_internet = (os.getenv("INSPIRE_WORKSPACE_INTERNET_ID") or "").strip()

    overrides: dict[str, str] = {}
    if env_cpu:
        overrides["cpu"] = env_cpu
    if env_gpu:
        overrides["gpu"] = env_gpu
    if env_internet:
        overrides["internet"] = env_internet
    return overrides


def _merge_alias_map(
    *,
    existing: dict[str, str],
    discovered: dict[str, str],
) -> dict[str, str]:
    merged = dict(existing)
    existing_ids = {v for v in existing.values() if isinstance(v, str) and v}
    used_aliases = set(existing.keys())

    alias_for_id: dict[str, str] = {}
    for alias, project_id in existing.items():
        if isinstance(project_id, str) and project_id and project_id not in alias_for_id:
            alias_for_id[project_id] = alias

    for alias, project_id in discovered.items():
        if not isinstance(project_id, str) or not project_id:
            continue
        if project_id in existing_ids:
            continue
        candidate = alias
        if not candidate:
            candidate = project_id
        candidate = _make_unique_alias(candidate, used_aliases)
        merged[candidate] = project_id

    return merged


def _build_project_aliases(
    projects: list[object],
    *,
    existing: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    existing_map = existing or {}
    used_aliases = set(existing_map.keys())
    alias_for_id: dict[str, str] = {}
    for alias, project_id in existing_map.items():
        if isinstance(project_id, str) and project_id and project_id not in alias_for_id:
            alias_for_id[project_id] = alias

    discovered_map: dict[str, str] = {}
    discovered_alias_for_id: dict[str, str] = {}

    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        name = str(getattr(project, "name", "") or "").strip()
        if not project_id:
            continue
        if project_id in alias_for_id:
            discovered_alias_for_id[project_id] = alias_for_id[project_id]
            continue

        alias = _slugify_alias(name)
        if not alias:
            suffix = project_id.split("-")[-1]
            alias = f"project-{suffix[:8]}" if suffix else "project"
        alias = _make_unique_alias(alias, used_aliases)
        discovered_map[alias] = project_id
        discovered_alias_for_id[project_id] = alias

    merged = _merge_alias_map(existing=existing_map, discovered=discovered_map)
    discovered_alias_for_id.update(
        {v: k for k, v in merged.items() if v not in discovered_alias_for_id}
    )
    return merged, discovered_alias_for_id


def _merge_compute_groups(
    existing: list[dict[str, Any]] | None,
    discovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip()
        if not group_id:
            continue
        by_id[group_id] = dict(item)

    for item in discovered:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip()
        if not group_id:
            continue
        merged = dict(by_id.get(group_id, {}))
        merged.update({k: v for k, v in item.items() if v is not None and v != ""})
        by_id[group_id] = merged

    merged_list = list(by_id.values())
    merged_list.sort(
        key=lambda entry: (str(entry.get("gpu_type") or ""), str(entry.get("name") or "").lower())
    )
    return merged_list


def _init_discover_mode(
    force: bool,
    *,
    probe_shared_path: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_pubkey: str | None,
    probe_timeout: int,
) -> None:
    """Initialize per-account catalogs by discovering projects and compute groups."""
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web import session as web_session_module
    from inspire.platform.web.session import DEFAULT_WORKSPACE_ID

    config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)

    try:
        session = web_session_module.get_web_session(require_workspace=True)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"))
        raise SystemExit(1) from e

    account_key = (config.username or session.login_username or "").strip()
    if not account_key:
        click.echo(click.style("Could not resolve account key (username)", fg="red"))
        raise SystemExit(1)

    workspace_id = str(session.workspace_id or "").strip()
    if not workspace_id or workspace_id == DEFAULT_WORKSPACE_ID:
        click.echo(
            click.style(
                "Could not detect a real workspace_id. Set INSPIRE_WORKSPACE_ID and retry.",
                fg="red",
            )
        )
        raise SystemExit(1)

    click.echo(click.style("Discovering account catalog...", bold=True))
    click.echo(f"Account: {account_key}")
    click.echo(f"Workspace: {workspace_id}")

    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:  # pragma: no cover - network/runtime dependent
        click.echo(click.style(f"Failed to list projects: {e}", fg="red"))
        raise SystemExit(1) from e

    if not projects:
        click.echo(click.style("No projects found for this workspace", fg="red"))
        raise SystemExit(1)

    if probe_shared_path and probe_limit < 0:
        click.echo(click.style("Invalid --probe-limit (must be >= 0)", fg="red"))
        raise SystemExit(1)

    selected_project = projects[0]
    try:
        selected_project, _ = browser_api_module.select_project(projects)
    except Exception:
        selected_project = projects[0]

    if not force:
        click.echo()
        click.echo(click.style("Projects:", bold=True))
        for idx, project in enumerate(projects, start=1):
            suffix = project.get_quota_status() if hasattr(project, "get_quota_status") else ""
            click.echo(f"  {idx}. {project.name} ({project.project_id}){suffix}")

        default_index = 1
        for idx, project in enumerate(projects, start=1):
            if project.project_id == selected_project.project_id:
                default_index = idx
                break
        choice = click.prompt(
            "Select default project",
            type=int,
            default=default_index,
            show_default=True,
        )
        if 1 <= choice <= len(projects):
            selected_project = projects[choice - 1]

    global_path = Config.GLOBAL_CONFIG_PATH
    if global_path.exists() and not force:
        click.echo()
        click.echo(click.style(f"Global config already exists: {global_path}", fg="yellow"))
        if not click.confirm(
            "Update it with discovered catalogs? (will rewrite file)", default=True
        ):
            click.echo("Aborted.")
            return

    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
    if project_path.exists() and not force:
        click.echo()
        click.echo(click.style(f"Project config already exists: {project_path}", fg="yellow"))
        if not click.confirm(
            "Update it with discovered context/defaults? (will rewrite file)", default=True
        ):
            click.echo("Aborted.")
            return

    global_data: dict[str, Any] = {}
    if global_path.exists():
        global_data = Config._load_toml(global_path)

    accounts = global_data.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        global_data["accounts"] = accounts

    account_section = accounts.get(account_key)
    if not isinstance(account_section, dict):
        account_section = {}
        accounts[account_key] = account_section

    existing_projects = account_section.get("projects")
    if not isinstance(existing_projects, dict):
        existing_projects = {}
    merged_projects, alias_for_id = _build_project_aliases(projects, existing=existing_projects)
    account_section["projects"] = merged_projects

    project_catalog = account_section.get("project_catalog")
    if not isinstance(project_catalog, dict):
        project_catalog = {}
        account_section["project_catalog"] = project_catalog

    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue

        entry = project_catalog.get(project_id)
        if not isinstance(entry, dict):
            entry = {}
            project_catalog[project_id] = entry

        entry.setdefault("id", project_id)

        alias = str(alias_for_id.get(project_id) or "").strip()
        if alias:
            entry["alias"] = alias

        name = str(getattr(project, "name", "") or "").strip()
        if name:
            entry["name"] = name

        project_workspace_id = str(getattr(project, "workspace_id", "") or workspace_id).strip()
        entry["workspace_id"] = project_workspace_id

        workdir = str(entry.get("workdir") or "").strip()
        if not workdir or force:
            try:
                workdir = (
                    browser_api_module.get_train_job_workdir(
                        project_id=project_id,
                        workspace_id=project_workspace_id,
                        session=session,
                    )
                    or ""
                ).strip()
            except Exception:
                workdir = ""

        if workdir:
            entry["workdir"] = workdir
            shared_group = _derive_shared_path_group(workdir, account_key=account_key)
            if shared_group and not str(entry.get("shared_path_group") or "").strip():
                entry["shared_path_group"] = shared_group

    global_user_groups: set[str] = set()
    for entry in project_catalog.values():
        if not isinstance(entry, dict):
            continue
        shared_group = str(entry.get("shared_path_group") or "").strip()
        if not shared_group or "/global_user/" not in shared_group:
            continue
        global_user_groups.add(shared_group)

    if len(global_user_groups) == 1:
        shared_path_group = next(iter(global_user_groups))
        if force or not str(account_section.get("shared_path_group") or "").strip():
            account_section["shared_path_group"] = shared_path_group

    shared_group_to_aliases: dict[str, list[str]] = {}
    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue
        alias = str(alias_for_id.get(project_id) or "").strip()
        if not alias:
            alias = _slugify_alias(str(getattr(project, "name", "") or "").strip()) or project_id

        entry = project_catalog.get(project_id)
        shared_group = (
            str(entry.get("shared_path_group") or "").strip() if isinstance(entry, dict) else ""
        )
        shared_group_to_aliases.setdefault(shared_group or "<unknown>", []).append(alias)

    click.echo()
    if len(shared_group_to_aliases) == 1 and "<unknown>" not in shared_group_to_aliases:
        group = next(iter(shared_group_to_aliases))
        click.echo(click.style("Shared path group:", bold=True) + f" {group}")
    else:
        click.echo(click.style("Shared path groups:", bold=True))
        for group, aliases in sorted(
            shared_group_to_aliases.items(),
            key=lambda item: (item[0] == "<unknown>", item[0]),
        ):
            click.echo(f"  - {group} ({len(aliases)} project(s))")
            sample = ", ".join(sorted(aliases)[:8])
            if sample:
                suffix = " ..." if len(aliases) > 8 else ""
                click.echo(f"      {sample}{suffix}")
        if "<unknown>" in shared_group_to_aliases:
            click.echo(
                "  Hint: run with --probe-shared-path to populate unknown shared-path groups."
            )

    existing_workspaces = account_section.get("workspaces")
    if not isinstance(existing_workspaces, dict):
        existing_workspaces = {}
    merged_workspaces: dict[str, str] = dict(existing_workspaces)

    config_workspaces = getattr(config, "workspaces", None)
    if isinstance(config_workspaces, dict):
        for raw_alias, raw_workspace_id in config_workspaces.items():
            alias = str(raw_alias or "").strip()
            workspace_value = str(raw_workspace_id or "").strip()
            if not alias or not workspace_value:
                continue
            if force or alias not in merged_workspaces:
                merged_workspaces[alias] = workspace_value

    explicit_workspaces = {
        "cpu": getattr(config, "workspace_cpu_id", None),
        "gpu": getattr(config, "workspace_gpu_id", None),
        "internet": getattr(config, "workspace_internet_id", None),
    }
    for alias, raw_workspace_id in explicit_workspaces.items():
        workspace_value = str(raw_workspace_id or "").strip()
        if not workspace_value:
            continue
        if force or alias not in merged_workspaces:
            merged_workspaces[alias] = workspace_value

    env_overrides = _discover_workspace_aliases()
    for alias, workspace_value in env_overrides.items():
        value = str(workspace_value or "").strip()
        if value:
            merged_workspaces[alias] = value

    merged_workspaces.setdefault("cpu", workspace_id)
    merged_workspaces.setdefault("gpu", workspace_id)
    merged_workspaces.setdefault("internet", workspace_id)
    account_section["workspaces"] = merged_workspaces

    base_url = (config.base_url or "").strip()
    if base_url and base_url != "https://api.example.com":
        api_section = account_section.get("api")
        if not isinstance(api_section, dict):
            api_section = {}
            account_section["api"] = api_section
        api_section.setdefault("base_url", base_url)

    compute_groups: list[dict[str, Any]] = []
    try:
        raw_groups = browser_api_module.list_compute_groups(
            workspace_id=workspace_id, session=session
        )
        gpu_types: dict[str, str] = {}
        try:
            availability = browser_api_module.get_accurate_gpu_availability(
                workspace_id=workspace_id, session=session
            )
            gpu_types = {
                str(item.group_id): str(item.gpu_type)
                for item in availability
                if getattr(item, "group_id", None)
            }
        except Exception:
            gpu_types = {}

        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("logic_compute_group_id") or group.get("id") or "").strip()
            name = str(group.get("name") or "").strip()
            if not group_id or not name:
                continue

            location = str(
                group.get("location")
                or group.get("location_name")
                or group.get("cluster_name")
                or ""
            ).strip()
            if not location and "(" in name and name.endswith(")"):
                location = name.rsplit("(", 1)[-1].rstrip(")").strip()

            compute_groups.append(
                {
                    "name": name,
                    "id": group_id,
                    "gpu_type": str(gpu_types.get(group_id, "") or "").strip(),
                    "location": location,
                }
            )
    except Exception:
        compute_groups = []

    existing_compute_groups = account_section.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = []
    if compute_groups:
        account_section["compute_groups"] = _merge_compute_groups(
            existing_compute_groups, compute_groups
        )

    if probe_shared_path:
        click.echo()
        click.echo(click.style("Probing shared filesystem paths...", bold=True))

        try:
            ssh_public_key = _load_ssh_public_key(probe_pubkey)
        except ValueError as e:
            click.echo(click.style(str(e), fg="red"))
            raise SystemExit(1) from e

        try:
            from inspire.config.ssh_runtime import resolve_ssh_runtime_config

            ssh_runtime = resolve_ssh_runtime_config()
        except Exception as e:
            click.echo(click.style(f"Failed to resolve SSH runtime config: {e}", fg="red"))
            raise SystemExit(1) from e

        probe_workspace_id = str(
            getattr(config, "workspace_cpu_id", "") or merged_workspaces.get("cpu") or workspace_id
        ).strip()
        if not probe_workspace_id:
            probe_workspace_id = workspace_id

        try:
            notebook_groups = browser_api_module.list_notebook_compute_groups(
                workspace_id=probe_workspace_id,
                session=session,
            )
            logic_compute_group_id = _select_probe_cpu_compute_group_id(notebook_groups)
            if not logic_compute_group_id:
                raise ValueError("No CPU compute group found")

            schedule = browser_api_module.get_notebook_schedule(
                workspace_id=probe_workspace_id,
                session=session,
            )
            quota_id, cpu_count, memory_size = _select_probe_cpu_quota(schedule)

            images = browser_api_module.list_images(
                workspace_id=probe_workspace_id,
                session=session,
            )
            selected_image = _select_probe_image(images)
            if not selected_image:
                raise ValueError("No images available")
        except Exception as e:
            click.echo(click.style(f"Failed to resolve probe defaults: {e}", fg="red"))
            raise SystemExit(1) from e

        shm_size = int(config.shm_size) if config.shm_size is not None else 32
        task_priority = int(config.job_priority) if config.job_priority is not None else 6
        task_priority = max(1, min(9, task_priority))

        to_probe: list[object] = []
        for project in projects:
            entry = project_catalog.get(project.project_id)
            shared = (
                str((entry or {}).get("shared_path_group") or "").strip()
                if isinstance(entry, dict)
                else ""
            )
            error = (
                str((entry or {}).get("probe_error") or "").strip()
                if isinstance(entry, dict)
                else ""
            )
            if not force and shared and not error:
                continue
            to_probe.append(project)

        if probe_limit:
            to_probe = to_probe[:probe_limit]

        if not to_probe:
            click.echo("No projects require probing.")
        else:
            global_path.parent.mkdir(parents=True, exist_ok=True)

            for idx, project in enumerate(to_probe, start=1):
                project_id = str(getattr(project, "project_id", "") or "").strip()
                project_name = str(getattr(project, "name", "") or "").strip()
                project_alias = str(
                    alias_for_id.get(project_id) or _slugify_alias(project_name) or project_id
                )

                click.echo(f"[{idx}/{len(to_probe)}] {project_name} ({project_alias})")

                probe_result = _probe_project_shared_path_group(
                    browser_api_module=browser_api_module,
                    session=session,
                    workspace_id=probe_workspace_id,
                    account_key=account_key,
                    project_id=project_id,
                    project_name=project_name,
                    project_alias=project_alias,
                    ssh_public_key=ssh_public_key,
                    ssh_runtime=ssh_runtime,
                    logic_compute_group_id=logic_compute_group_id,
                    quota_id=quota_id,
                    cpu_count=cpu_count,
                    memory_size=memory_size,
                    image_id=str(getattr(selected_image, "image_id", "") or ""),
                    image_url=str(getattr(selected_image, "url", "") or ""),
                    shm_size=shm_size,
                    task_priority=task_priority,
                    keep_notebook=probe_keep_notebooks,
                    timeout=probe_timeout,
                )

                entry = project_catalog.get(project_id)
                if not isinstance(entry, dict):
                    entry = {"id": project_id}
                    project_catalog[project_id] = entry

                entry["probed_at"] = _utc_now_iso()
                if probe_result.get("notebook_id"):
                    entry["probe_notebook_id"] = probe_result["notebook_id"]

                shared_path_group = str(probe_result.get("shared_path_group") or "").strip()
                if shared_path_group:
                    entry["shared_path_group"] = shared_path_group
                    entry.pop("probe_error", None)
                else:
                    probe_error = str(probe_result.get("probe_error") or "").strip()
                    if probe_error:
                        entry["probe_error"] = probe_error

                global_path.write_text(_toml_dumps(global_data))

    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(_toml_dumps(global_data))

    selected_alias = alias_for_id.get(selected_project.project_id)
    if not selected_alias:
        selected_alias = _slugify_alias(selected_project.name) or "default"

    project_data: dict[str, Any] = {}
    if project_path.exists():
        project_data = Config._load_toml(project_path)

    auth_section = project_data.get("auth")
    if not isinstance(auth_section, dict):
        auth_section = {}
        project_data["auth"] = auth_section
    auth_section["username"] = account_key

    context = project_data.get("context")
    if not isinstance(context, dict):
        context = {}
        project_data["context"] = context

    context.update(
        {
            "account": account_key,
            "project": selected_alias,
            "workspace_cpu": "cpu",
            "workspace_gpu": "gpu",
            "workspace_internet": "internet",
        }
    )

    defaults = project_data.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        project_data["defaults"] = defaults

    if config.target_dir:
        defaults.setdefault("target_dir", config.target_dir)
    if config.log_pattern:
        defaults.setdefault("log_pattern", config.log_pattern)
    if config.job_image:
        defaults.setdefault("image", config.job_image)
    if config.notebook_image:
        defaults.setdefault("notebook_image", config.notebook_image)
    if config.notebook_resource:
        defaults.setdefault("notebook_resource", config.notebook_resource)
    if config.job_priority is not None:
        defaults.setdefault("priority", int(config.job_priority))
    if config.shm_size is not None:
        defaults.setdefault("shm_size", int(config.shm_size))

    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(_toml_dumps(project_data))

    resolved, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    if not str(getattr(resolved, "job_project_id", "") or "").startswith("project-"):
        click.echo(click.style("Wrote config, but could not resolve a project_id", fg="red"))
        raise SystemExit(1)

    click.echo()
    click.echo(click.style("Wrote configuration:", bold=True))
    click.echo(f"  - {global_path}")
    click.echo(f"  - {project_path}")
    click.echo()
    click.echo("Next steps:")
    click.echo(
        "  1. Ensure a password is available via INSPIRE_PASSWORD or global "
        '[accounts."<username>"].password'
    )
    click.echo("  2. Run: inspire config show")
