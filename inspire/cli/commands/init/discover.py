"""Discovery mode: probe workspaces, projects, compute groups, and shared paths."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, TextIO, cast

import click

from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    SOURCE_INFERRED,
    Config,
    get_account_discovery_state_path,
    load_account_discovery_state,
    reconcile_account_discovery_state,
    save_account_discovery_state,
)
from inspire.config.toml_write import atomic_write_text
from inspire.platform.web.browser_api.projects import ProjectInfo
from inspire.platform.web.session import (
    SessionAuthenticationError,
    SessionExpiredError,
    WebSession,
)

from .discovery_state import (
    build_project_selectors,
    build_workspace_selectors,
    observe_account_discovery,
    state_diff,
)
from .env_detect import _redact_token_like_text
from .toml_helpers import _toml_dumps

from inspire.platform.web.browser_api.core import _set_base_url
from inspire.platform.web.browser_api.notebooks import NotebookFailedError

_CATALOG_DROP_FIELDS = frozenset(
    {
        "id",
        "alias",
        "workspace_id",
        "probed_at",
        "probe_notebook_id",
        "probe_error",
    }
)


class _ProbeDefaults(NamedTuple):
    ssh_runtime: object
    ssh_public_key: str
    probe_workspace_id: str
    logic_compute_group_id: str
    quota_id: str
    cpu_count: int
    memory_size: int
    selected_image: object
    task_priority: int
    shm_size: int


@dataclass(frozen=True)
class _DiscoveryPersistRequest:
    force: bool
    config: Config
    browser_api_module: object
    session: WebSession
    account_alias: str
    platform_username: str
    workspace_id: str
    projects: list[ProjectInfo]
    selected_project: ProjectInfo | None
    probe_shared_path: bool
    probe_limit: int
    probe_keep_notebooks: bool
    probe_pubkey: str | None
    probe_timeout: int
    prompted_credentials: tuple[str, str, str] | None
    prompted_password: bool
    cli_target_dir: str | None
    dry_run: bool = False
    yes: bool = False
    interactive: bool = True


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
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        if group.get("gpu_type_stats") or str(group.get("gpu_type") or "").strip():
            continue
        group_id = str(group.get("logic_compute_group_id") or group.get("id") or "").strip()
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
    # Assignments must be `export`ed: a leading `VAR=value` with no command word
    # (or a `&&` chain after bare assignments) leaves the variable in the shell
    # without exporting it, so the heredoc python would see an empty environment.
    return (
        f"export INSPIRE_ACCOUNT_KEY={account}; "
        'export PYTHON_BIN="$(command -v python3 || command -v python)"; '
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
            _has_internet_stored=True,
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
    except NotebookFailedError as e:
        result["probe_error"] = f"Notebook failed: {e.status}"
        if e.events:
            result["probe_error"] += f" - {e.events}"
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


def _resolve_credentials_interactive(
    config: object,
    *,
    cli_username: str | None,
    cli_base_url: str | None,
    allow_config_username: bool = True,
    allow_config_password: bool = False,
) -> tuple[str, str, str, bool]:
    """Resolve base_url, username, and password, prompting when missing."""
    placeholder = "https://api.example.com"

    # --- base_url ---
    base_url = (cli_base_url or "").strip()
    if not base_url:
        cfg_base_url = str(getattr(config, "base_url", "") or "").strip()
        if cfg_base_url and cfg_base_url != placeholder:
            base_url = cfg_base_url
    if not base_url:
        base_url = click.prompt("Platform URL", type=str).strip()
    if not base_url:
        click.echo(click.style("Platform URL is required.", fg="red"))
        raise SystemExit(1)

    # --- username ---
    username = (cli_username or "").strip()
    if not username and allow_config_username:
        cfg_username = str(getattr(config, "username", "") or "").strip()
        if cfg_username:
            username = cfg_username
    if not username:
        username = click.prompt("Username", type=str).strip()
    if not username:
        click.echo(click.style("Username is required.", fg="red"))
        raise SystemExit(1)

    # --- password ---
    # When the caller explicitly provided credentials (allow_config_password=True),
    # the config/env password is likely valid — use it to support non-interactive
    # --force mode.  In the session-failed fallback path the old password may be
    # stale, so always prompt for a fresh one.
    password = ""
    prompted_password = False
    if allow_config_password:
        password = str(getattr(config, "password", "") or "").strip()
    if not password:
        password = click.prompt("Password", type=str, hide_input=True)
        prompted_password = True
    if not password:
        click.echo(click.style("Password is required.", fg="red"))
        raise SystemExit(1)

    return username, password, base_url, prompted_password


def _login_with_saved_or_prompted_credentials(
    *,
    config: object,
    web_session_module,  # noqa: ANN001
    cli_username: str | None,
    cli_base_url: str | None,
) -> tuple[WebSession, tuple[str, str, str] | None, bool]:
    """Login for discover, preferring saved config credentials on migration/reruns.

    Behavior:
    - If username/base_url/password are already resolvable from config, try them
      first without prompting.
    - If that login fails, prompt only for the missing or stale password and
      retry.
    - Returned ``prompted_credentials`` is populated only when the user was
      actually prompted, so reruns do not rewrite the stored password unless it
      changed interactively.
    """
    username, password, base_url, prompted_password = _resolve_credentials_interactive(
        config,
        cli_username=cli_username,
        cli_base_url=cli_base_url,
        allow_config_password=True,
    )

    click.echo("Logging in...")
    if prompted_password:
        session = web_session_module.login_with_requests(
            username,
            password,
            base_url=base_url,
        )
        click.echo("Logged in.")
        return session, (username, password, base_url), True

    try:
        session = web_session_module.login_with_requests(
            username,
            password,
            base_url=base_url,
        )
        click.echo("Logged in.")
        return session, None, False
    except SessionAuthenticationError as exc:
        if "Wrong username or password" not in str(exc):
            raise
        click.echo("Stored credentials failed; please enter the password again.")
        return _login_with_prompted_credentials(
            config=config,
            web_session_module=web_session_module,
            cli_username=cli_username,
            cli_base_url=cli_base_url,
        )


def _login_with_prompted_credentials(
    *,
    config: object,
    web_session_module,  # noqa: ANN001
    cli_username: str | None,
    cli_base_url: str | None,
) -> tuple[WebSession, tuple[str, str, str], bool]:
    """Prompt for a replacement password and perform exactly one login."""
    username, password, base_url, prompted_password = _resolve_credentials_interactive(
        config,
        cli_username=cli_username,
        cli_base_url=cli_base_url,
        allow_config_password=False,
    )
    session = web_session_module.login_with_requests(
        username,
        password,
        base_url=base_url,
    )
    click.echo("Logged in.")
    return session, (username, password, base_url), prompted_password


def _ensure_ssh_key() -> None:
    """Check for an SSH key; offer to generate one if missing."""
    import subprocess

    ssh_dir = Path.home() / ".ssh"
    candidates = [ssh_dir / "id_ed25519.pub", ssh_dir / "id_rsa.pub"]
    if any(p.exists() for p in candidates):
        return

    click.echo()
    click.echo("No SSH key found. SSH keys are needed for bridge/tunnel/notebook SSH features.")

    # Non-interactive contexts (CI, tests) must not block on prompts or fail on EOF.
    get_text_stream = cast(Callable[[str], TextIO], click.get_text_stream)
    stdin = get_text_stream("stdin")
    if not stdin.isatty():
        click.echo("Skipping SSH key generation in non-interactive mode.")
        return

    if not click.confirm("Generate a new ed25519 SSH key?", default=True):
        return

    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = ssh_dir / "id_ed25519"
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "inspire-cli"],
        capture_output=True,
    )
    if result.returncode == 0:
        click.echo(f"SSH key generated: {key_path}")
    else:
        click.echo(click.style("SSH key generation failed.", fg="yellow"))


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
    projects: list[ProjectInfo],
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
        existing_ws = set(merged.get("workspace_ids") or [])
        new_ws = set(item.get("workspace_ids") or [])
        merged.update({k: v for k, v in item.items() if v is not None and v != ""})
        combined = sorted(existing_ws | new_ws)
        if combined:
            merged["workspace_ids"] = combined
        by_id[group_id] = merged

    merged_list = list(by_id.values())
    for entry in merged_list:
        for k in [k for k, v in entry.items() if v == ""]:
            del entry[k]
    merged_list.sort(
        key=lambda entry: (str(entry.get("gpu_type") or ""), str(entry.get("name") or "").lower())
    )
    return merged_list


def _resolve_discover_runtime(
    *,
    config: Config,
    web_session_module,  # noqa: ANN001
    default_workspace_id: str,
    cli_username: str | None,
    cli_base_url: str | None,
) -> tuple[WebSession, tuple[str, str, str] | None, bool, str, str, str]:
    # When the caller explicitly provides credentials via CLI flags, skip the
    # cached-session fast path so we honour the override instead of silently
    # using a session that belongs to a different user / base-url.
    session = None
    prompted_credentials: tuple[str, str, str] | None = None
    prompted_password = False
    if cli_username or cli_base_url:
        username, password, base_url, prompted_password = _resolve_credentials_interactive(
            config,
            cli_username=cli_username,
            cli_base_url=cli_base_url,
            allow_config_username=bool(cli_username),
            # CLI overrides force an interactive login path; do not silently
            # reuse a config-derived password because the caller may be fixing
            # stale credentials while updating the username or base URL.
            allow_config_password=False,
        )
        prompted_credentials = (username, password, base_url)
        click.echo("Logging in...")
        session = web_session_module.login_with_requests(
            username,
            password,
            base_url=base_url,
        )
        click.echo("Logged in.")
    else:
        try:
            session = web_session_module.get_web_session(
                require_workspace=True,
                account=config.account_alias or None,
            )
        except SessionAuthenticationError as exc:
            if "Wrong username or password" not in str(exc):
                raise
            click.echo("Stored credentials failed; please enter the password again.")
            session, prompted_credentials, prompted_password = _login_with_prompted_credentials(
                config=config,
                web_session_module=web_session_module,
                cli_username=cli_username,
                cli_base_url=cli_base_url,
            )
        except ValueError:
            session, prompted_credentials, prompted_password = (
                _login_with_saved_or_prompted_credentials(
                    config=config,
                    web_session_module=web_session_module,
                    cli_username=cli_username,
                    cli_base_url=cli_base_url,
                )
            )

    if prompted_credentials:
        platform_username = prompted_credentials[0]
    else:
        platform_username = (config.username or session.login_username or "").strip()
    if not platform_username:
        click.echo(click.style("Could not resolve platform username", fg="red"))
        raise SystemExit(1)
    account_alias = str(config.account_alias or "").strip() or platform_username

    placeholder = "https://api.example.com"
    if prompted_credentials:
        _set_base_url(prompted_credentials[2])
    else:
        cfg_base_url = str(getattr(config, "base_url", "") or "").strip()
        if cfg_base_url and cfg_base_url != placeholder:
            _set_base_url(cfg_base_url)
        elif session.base_url:
            _set_base_url(session.base_url)

    workspace_id = str(session.workspace_id or "").strip()
    if not workspace_id or workspace_id == default_workspace_id:
        click.echo(
            click.style(
                "Could not detect a real workspace_id. Set INSPIRE_WORKSPACE_ID and retry.",
                fg="red",
            )
        )
        raise SystemExit(1)

    return (
        session,
        prompted_credentials,
        prompted_password,
        account_alias,
        platform_username,
        workspace_id,
    )


def _candidate_workspace_ids_for_discovery(
    *,
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[str]:
    """Return deduplicated workspace IDs to query during discovery."""
    candidates: list[str] = [workspace_id]
    candidates.extend(str(ws or "").strip() for ws in (session.all_workspace_ids or []))

    # Best-effort augmentation for stale/partial session metadata.
    try:
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        for ws in try_enumerate_workspaces(session, workspace_id=workspace_id):
            ws_id = str(ws.get("id") or "").strip()
            if ws_id:
                candidates.append(ws_id)
    except SessionExpiredError:
        raise
    except Exception:
        pass

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for raw_ws in candidates:
        candidate = str(raw_ws or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered_unique.append(candidate)
    return ordered_unique


def _collect_discovery_projects(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> tuple[list[ProjectInfo], list[tuple[str, str]]]:
    """Collect projects across discovered workspaces (best-effort per workspace)."""
    workspace_ids = _candidate_workspace_ids_for_discovery(
        session=session,
        workspace_id=workspace_id,
    )

    discovered: list[ProjectInfo] = []
    errors: list[tuple[str, str]] = []
    seen_project_ids: set[str] = set()

    for ws_id in workspace_ids:
        try:
            ws_projects = browser_api_module.list_projects(workspace_id=ws_id, session=session)
        except SessionExpiredError:
            raise
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            errors.append((ws_id, str(exc)))
            continue

        for project in ws_projects:
            project_id = str(getattr(project, "project_id", "") or "").strip()
            if not project_id:
                continue
            if project_id in seen_project_ids:
                continue
            seen_project_ids.add(project_id)
            discovered.append(project)

    return discovered, errors


def _load_projects_for_discovery(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    force: bool,
    probe_shared_path: bool,
    probe_limit: int,
    yes: bool = False,
) -> tuple[list[ProjectInfo], ProjectInfo]:
    projects, workspace_errors = _collect_discovery_projects(
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
    )

    if not projects:
        if workspace_errors:
            sample = ", ".join(f"{ws}: {msg}" for ws, msg in workspace_errors[:3])
            if len(workspace_errors) > 3:
                sample += ", ..."
            click.echo(
                click.style(
                    f"Failed to list projects across discovered workspaces "
                    f"({len(workspace_errors)} failed: {sample})",
                    fg="red",
                )
            )
        else:
            click.echo(click.style("No projects found for discovered workspaces", fg="red"))
        raise SystemExit(1)

    if workspace_errors and not force:
        sample = ", ".join(f"{ws}: {msg}" for ws, msg in workspace_errors[:3])
        if len(workspace_errors) > 3:
            sample += ", ..."
        click.echo(
            click.style(
                f"Warning: some workspaces failed during project discovery "
                f"({len(workspace_errors)}): {sample}",
                fg="yellow",
            )
        )

    if probe_shared_path and probe_limit < 0:
        click.echo(click.style("Invalid --probe-limit (must be >= 0)", fg="red"))
        raise SystemExit(1)

    selected_project = projects[0]
    try:
        selected_project, _ = browser_api_module.select_project(projects)
    except Exception:
        selected_project = projects[0]

    def _echo_project_list() -> None:
        click.echo()
        click.echo(click.style("Projects:", bold=True))
        for idx, project in enumerate(projects, start=1):
            suffix = project.get_quota_status() if hasattr(project, "get_quota_status") else ""
            click.echo(f"  {idx}. {project.name} ({project.project_id}){suffix}")

    if force or yes:
        # ``--yes``/``--force`` must stay usable in non-interactive shells: keep
        # the platform's own selection heuristic instead of prompting.
        _echo_project_list()
        click.echo(
            f"Auto-selected project: {selected_project.name} ({selected_project.project_id})"
        )
        return projects, selected_project

    _echo_project_list()
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

    return projects, selected_project


def _confirm_discovery_writes(
    *,
    force: bool,
    global_path: Path,
    project_path: Path,
    yes: bool = False,
) -> bool:
    if yes or force:
        return True

    if global_path.exists() and not force:
        click.echo()
        click.echo(click.style(f"Global config already exists: {global_path}", fg="yellow"))
        if not click.confirm(
            "Update it with discovered catalogs? (will rewrite file)", default=True
        ):
            click.echo("Aborted.")
            return False

    if project_path.exists() and not force:
        click.echo()
        click.echo(click.style(f"Project config already exists: {project_path}", fg="yellow"))
        if not click.confirm(
            "Update it with discovered context/defaults? (will rewrite file)", default=True
        ):
            click.echo("Aborted.")
            return False
    return True


def _load_discovery_global_state(
    *,
    global_path: Path,
    account_alias: str,
    platform_username: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    global_data: dict[str, Any] = {}
    if global_path.exists():
        global_data = Config._load_toml(global_path)

    accounts = global_data.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        global_data["accounts"] = accounts

    account_section = accounts.get(account_alias)
    if not isinstance(account_section, dict):
        account_section = {}
        accounts[account_alias] = account_section
    account_section["username"] = platform_username

    context = global_data.get("context")
    if not isinstance(context, dict):
        context = {}
        global_data["context"] = context
    context.setdefault("account", account_alias)

    return global_data, account_section


def _resolve_project_catalog_aliases(
    *,
    account_section: dict[str, Any],
    projects: list[ProjectInfo],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    existing_projects = account_section.get("projects")
    if not isinstance(existing_projects, dict):
        existing_projects = {}
    merged_projects, alias_for_id = _build_project_aliases(projects, existing=existing_projects)
    account_section["projects"] = merged_projects

    project_catalog = account_section.get("project_catalog")
    if not isinstance(project_catalog, dict):
        project_catalog = {}
        account_section["project_catalog"] = project_catalog

    typed_catalog: dict[str, dict[str, Any]] = {}
    for project_id, entry in project_catalog.items():
        if not isinstance(project_id, str):
            continue
        if isinstance(entry, dict):
            typed_catalog[project_id] = entry
        else:
            typed_catalog[project_id] = {}

    account_section["project_catalog"] = typed_catalog
    return alias_for_id, typed_catalog


def _populate_project_catalog(
    *,
    project_catalog: dict[str, dict[str, Any]],
    projects: list[ProjectInfo],
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    account_key: str,
    force: bool,
) -> None:
    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue

        entry = project_catalog.setdefault(project_id, {})
        name = str(getattr(project, "name", "") or "").strip()
        if name:
            entry["name"] = name

        project_workspace_id = str(getattr(project, "workspace_id", "") or workspace_id).strip()
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

        if not workdir:
            continue
        entry["workdir"] = workdir
        shared_group = _derive_shared_path_group(workdir, account_key=account_key)
        if shared_group and not str(entry.get("shared_path_group") or "").strip():
            entry["shared_path_group"] = shared_group


def _update_account_shared_path_group(
    *,
    account_section: dict[str, Any],
    project_catalog: dict[str, dict[str, Any]],
    force: bool,
) -> None:
    global_user_groups: set[str] = set()
    for entry in project_catalog.values():
        shared_group = str(entry.get("shared_path_group") or "").strip()
        if shared_group and "/global_user/" in shared_group:
            global_user_groups.add(shared_group)

    if len(global_user_groups) != 1:
        return

    shared_path_group = next(iter(global_user_groups))
    if force or not str(account_section.get("shared_path_group") or "").strip():
        account_section["shared_path_group"] = shared_path_group


def _print_shared_path_group_summary(
    *,
    projects: list[ProjectInfo],
    project_catalog: dict[str, dict[str, Any]],
    alias_for_id: dict[str, str],
) -> None:
    shared_group_to_aliases: dict[str, list[str]] = {}
    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue
        alias = str(alias_for_id.get(project_id) or "").strip()
        if not alias:
            alias = _slugify_alias(str(getattr(project, "name", "") or "").strip()) or project_id

        entry = project_catalog.get(project_id) or {}
        shared_group = str(entry.get("shared_path_group") or "").strip()
        shared_group_to_aliases.setdefault(shared_group or "<unknown>", []).append(alias)

    click.echo()
    if len(shared_group_to_aliases) == 1 and "<unknown>" not in shared_group_to_aliases:
        group = next(iter(shared_group_to_aliases))
        click.echo(click.style("Shared path group:", bold=True) + f" {group}")
        return

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
        click.echo("  Hint: run with --probe-shared-path to populate unknown shared-path groups.")


def _persist_api_base_url(
    *,
    global_data: dict[str, Any],
    account_section: dict[str, Any],
    config: Config,
) -> None:
    base_url = (config.base_url or "").strip()
    if base_url and base_url != "https://api.example.com":
        account_section.setdefault("base_url", base_url)
        api_section = global_data.get("api")
        if not isinstance(api_section, dict):
            api_section = {}
            global_data["api"] = api_section
        api_section.setdefault("base_url", base_url)
    account_section.pop("api", None)


def _discover_docker_registry(
    *,
    global_data: dict[str, Any],
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> None:
    """Auto-detect docker_registry from image URLs returned by the platform."""
    api_section = global_data.get("api")
    if isinstance(api_section, dict) and api_section.get("docker_registry"):
        return  # already set

    try:
        images = browser_api_module.list_images(
            workspace_id=workspace_id, source="SOURCE_OFFICIAL", session=session
        )
    except Exception:
        return

    for img in images:
        url = str(getattr(img, "url", "") or "").strip()
        if not url:
            continue
        # Image URLs look like "registry.host/path/image:tag" — extract hostname.
        url = url.split("://", 1)[-1]  # strip scheme if present
        host = url.split("/", 1)[0]
        if host and "." in host:
            if not isinstance(api_section, dict):
                api_section = {}
                global_data["api"] = api_section
            api_section["docker_registry"] = host
            return


def _discover_compute_groups(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[dict[str, Any]]:
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

            cg_entry: dict[str, Any] = {"name": name, "id": group_id}
            gpu_type = str(gpu_types.get(group_id, "") or "").strip()
            if gpu_type:
                cg_entry["gpu_type"] = gpu_type
            if location:
                cg_entry["location"] = location
            compute_groups.append(cg_entry)
    except Exception:
        return []
    return compute_groups


def _discover_workspace_specs(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Discover resource specs for a workspace from browser API.

    Returns list of spec dictionaries suitable for persistence to config.
    Follows same pattern as _discover_compute_groups().
    """
    try:
        from inspire.platform.openapi.workspace_specs import fetch_workspace_specs

        specs = fetch_workspace_specs(workspace_id)
        return [
            {
                "spec_id": spec.spec_id,
                "gpu_type": (
                    spec.gpu_type.value if hasattr(spec.gpu_type, "value") else str(spec.gpu_type)
                ),
                "gpu_count": spec.gpu_count,
                "cpu_cores": spec.cpu_cores,
                "memory_gb": spec.memory_gb,
                "gpu_memory_gb": spec.gpu_memory_gb,
                "description": spec.description,
            }
            for spec in specs
        ]
    except Exception:
        return []


def _persist_compute_groups(
    *,
    global_data: dict[str, Any],
    account_section: dict[str, Any],
    compute_groups: list[dict[str, Any]],
) -> None:
    existing_compute_groups = global_data.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = account_section.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = []
    if compute_groups:
        global_data["compute_groups"] = _merge_compute_groups(
            existing_compute_groups, compute_groups
        )
    account_section.pop("compute_groups", None)


def _persist_workspace_specs(
    *,
    global_data: dict[str, Any],
    workspace_specs: dict[str, list[dict[str, Any]]],
    workspace_names: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    """Persist discovered workspace specs to global config.

    Flat structure: workspace_specs.<id> = [spec_dict, ...]
    Preserves existing specs (backward compatible).

    Args:
        global_data: The global config data dict to modify
        workspace_specs: Dict mapping workspace_id -> list of spec dicts
        workspace_names: Optional dict mapping workspace_id -> workspace name

    Returns:
        Tuple of (new_workspaces, existing_workspaces, total_specs)
    """
    if "workspace_specs" not in global_data:
        global_data["workspace_specs"] = {}

    existing_specs = global_data["workspace_specs"]
    new_count = 0
    existing_count = 0
    total_specs = 0

    for workspace_id, specs in workspace_specs.items():
        if workspace_id in existing_specs:
            existing_count += 1
            total_specs += len(existing_specs[workspace_id])
        else:
            global_data["workspace_specs"][workspace_id] = specs
            new_count += 1
            total_specs += len(specs)

    if workspace_names:
        global_data.setdefault("workspace_names", {})
        for ws_id, ws_name in workspace_names.items():
            global_data["workspace_names"].setdefault(ws_id, ws_name)

    return new_count, existing_count, total_specs


def _resolve_probe_defaults(
    *,
    config: Config,
    merged_workspaces: dict[str, str],
    workspace_id: str,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    probe_pubkey: str | None,
) -> _ProbeDefaults:
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

    del merged_workspaces
    probe_workspace_id = str(config.default_workspace_id or workspace_id).strip()

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

    return _ProbeDefaults(
        ssh_runtime=ssh_runtime,
        ssh_public_key=ssh_public_key,
        probe_workspace_id=probe_workspace_id,
        logic_compute_group_id=logic_compute_group_id,
        quota_id=quota_id,
        cpu_count=cpu_count,
        memory_size=memory_size,
        selected_image=selected_image,
        task_priority=task_priority,
        shm_size=shm_size,
    )


def _build_probe_project_list(
    *,
    projects: list[ProjectInfo],
    project_catalog: dict[str, dict[str, Any]],
    force: bool,
    probe_limit: int,
) -> list[ProjectInfo]:
    to_probe: list[ProjectInfo] = []
    for project in projects:
        entry = project_catalog.get(project.project_id) or {}
        shared = str(entry.get("shared_path_group") or "").strip()
        # A known group outranks a stale probe_error: rebuilding the notebook on
        # every run would re-probe a project whose group is already resolved, and
        # the error stays visible in the summary regardless.
        if not force and shared:
            continue
        to_probe.append(project)
    if probe_limit:
        to_probe = to_probe[:probe_limit]
    return to_probe


def _apply_probe_result(
    *,
    entry: dict[str, Any],
    probe_result: dict[str, Any],
) -> None:
    if probe_result.get("notebook_id"):
        entry["probe_notebook_id"] = probe_result["notebook_id"]

    shared_path_group = str(probe_result.get("shared_path_group") or "").strip()
    if shared_path_group:
        entry["probed_at"] = _utc_now_iso()
        entry["shared_path_group"] = shared_path_group
        entry.pop("probe_error", None)
        return

    # rc=0 with an empty global_user_dir is a failed probe, not a success:
    # stamping probed_at here would hide the gap and every later run would
    # either trust an <unknown> group or rebuild a notebook invisibly.
    probe_error = str(probe_result.get("probe_error") or "").strip()
    entry["probe_error"] = probe_error or "Probe completed but returned no global_user_dir"


def _run_shared_path_probe(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    account_key: str,
    projects: list[ProjectInfo],
    project_catalog: dict[str, dict[str, Any]],
    alias_for_id: dict[str, str],
    force: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_timeout: int,
    probe_defaults: _ProbeDefaults,
) -> None:
    to_probe = _build_probe_project_list(
        projects=projects,
        project_catalog=project_catalog,
        force=force,
        probe_limit=probe_limit,
    )
    if not to_probe:
        click.echo("No projects require probing.")
        return

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
            workspace_id=probe_defaults.probe_workspace_id,
            account_key=account_key,
            project_id=project_id,
            project_name=project_name,
            project_alias=project_alias,
            ssh_public_key=probe_defaults.ssh_public_key,
            ssh_runtime=probe_defaults.ssh_runtime,
            logic_compute_group_id=probe_defaults.logic_compute_group_id,
            quota_id=probe_defaults.quota_id,
            cpu_count=probe_defaults.cpu_count,
            memory_size=probe_defaults.memory_size,
            image_id=str(getattr(probe_defaults.selected_image, "image_id", "") or ""),
            image_url=str(getattr(probe_defaults.selected_image, "url", "") or ""),
            shm_size=probe_defaults.shm_size,
            task_priority=probe_defaults.task_priority,
            keep_notebook=probe_keep_notebooks,
            timeout=probe_timeout,
        )

        entry = project_catalog.setdefault(project_id, {"id": project_id})
        _apply_probe_result(entry=entry, probe_result=probe_result)


def _drop_catalog_runtime_fields(project_catalog: dict[str, dict[str, Any]]) -> None:
    for entry in project_catalog.values():
        for field in _CATALOG_DROP_FIELDS:
            entry.pop(field, None)


def _persist_prompted_credentials(
    *,
    account_section: dict[str, Any],
    prompted_credentials: tuple[str, str, str] | None,
) -> None:
    if not prompted_credentials:
        return
    prompted_username, prompted_password, prompted_base_url = prompted_credentials
    account_section["username"] = prompted_username
    account_section["password"] = prompted_password
    account_section["base_url"] = prompted_base_url


def _get_or_create_dict_table(
    *,
    container: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    section = container.get(key)
    if isinstance(section, dict):
        return section
    section = {}
    container[key] = section
    return section


def _populate_project_defaults_from_config(
    *,
    defaults: dict[str, Any],
    config: Config,
) -> None:
    if config.target_dir:
        defaults.setdefault("target_dir", config.target_dir)
    if getattr(config, "default_image", None):
        defaults.setdefault("image", config.default_image)
    elif config.notebook_image:
        defaults.setdefault("image", config.notebook_image)
    elif config.job_image:
        defaults.setdefault("image", config.job_image)
    if getattr(config, "default_resource", None):
        defaults.setdefault("resource", config.default_resource)
    elif config.notebook_resource:
        defaults.setdefault("resource", config.notebook_resource)
    if (default_priority := getattr(config, "default_priority", None)) is not None:
        defaults.setdefault("priority", int(default_priority))
    elif config.job_priority is not None:
        defaults.setdefault("priority", int(config.job_priority))
    elif (notebook_priority := getattr(config, "notebook_priority", None)) is not None:
        defaults.setdefault("priority", int(notebook_priority))
    if config.shm_size is not None:
        defaults.setdefault("shm_size", int(config.shm_size))
    if config.project_order:
        defaults.setdefault("project_order", list(config.project_order))


def _prompt_target_dir(
    *,
    force: bool,
    cli_target_dir: str | None,
    existing_target_dir: str | None,
    selected_project: object,
    project_catalog: dict[str, dict[str, Any]],
    yes: bool = False,
) -> str | None:
    """Resolve target_dir while preserving explicit user config when present.

    Ownership rule:
    - ``cli_target_dir`` is an explicit discover-time override and wins.
    - ``existing_target_dir`` is a user-managed setting from config and must be
      preserved across reruns.
    - ``catalog_workdir`` is a discovery hint only; it should fill blanks, not
      overwrite an intentional user value.
    """
    project_id = str(getattr(selected_project, "project_id", "") or "").strip()
    entry = project_catalog.get(project_id, {})
    catalog_workdir = str(entry.get("workdir") or "").strip()

    if cli_target_dir and cli_target_dir.strip():
        # Documented contract: --target-dir skips the prompt.
        return cli_target_dir.strip()

    if force:
        return existing_target_dir or catalog_workdir or None

    default = existing_target_dir or catalog_workdir or ""
    if yes:
        # Non-interactive: accept the preserved/hinted value, never hang on a prompt.
        return default or None

    if default:
        result = click.prompt(
            "Target directory on shared filesystem",
            default=default,
            show_default=True,
        )
    else:
        result = click.prompt(
            "Target directory on shared filesystem (e.g. /inspire/...)",
            default="",
            show_default=False,
        )
    return result.strip() or None


def _write_discovered_project_config(
    *,
    project_path: Path,
    config: Config,
    account_alias: str,
    target_dir: str | None = None,
) -> None:
    project_data: dict[str, Any] = {}
    if project_path.exists():
        project_data = Config._load_toml(project_path)

    # Legacy top-level [workspaces] was the old discover output. Workspace
    # routing is account-scoped now, so remove the stale project-level table.
    project_data.pop("workspaces", None)

    # Legacy project-level [[compute_groups]] from old discover runs had no
    # workspace binding metadata. If every entry is unbound, drop the whole
    # block so the refreshed global catalog becomes authoritative. Preserve any
    # modern project-specific overrides that already carry workspace_ids.
    legacy_project_compute_groups = project_data.get("compute_groups")
    if isinstance(legacy_project_compute_groups, list) and legacy_project_compute_groups:
        if all(
            isinstance(item, dict) and not (item.get("workspace_ids") or [])
            for item in legacy_project_compute_groups
        ):
            project_data.pop("compute_groups", None)

    auth_section = project_data.get("auth")
    if isinstance(auth_section, dict):
        auth_section.pop("username", None)
        if not auth_section:
            project_data.pop("auth", None)

    context = project_data.get("context")
    if not isinstance(context, dict):
        context = {}
        project_data["context"] = context
    context["account"] = account_alias
    for key in ("project", "workspace", "workspace_cpu", "workspace_gpu", "workspace_internet"):
        context.pop(key, None)

    defaults = _get_or_create_dict_table(container=project_data, key="defaults")
    _populate_project_defaults_from_config(defaults=defaults, config=config)
    if target_dir:
        defaults["target_dir"] = target_dir

    atomic_write_text(project_path, _toml_dumps(project_data))


def _print_discover_completion(
    *,
    global_path: Path,
    project_path: Path,
    prompted_password: bool,
) -> None:
    click.echo()
    click.echo(click.style("Wrote configuration:", bold=True))
    click.echo(f"  - {global_path}")
    click.echo(f"  - {project_path}")
    click.echo()
    if prompted_password:
        click.echo("Note: prompted account password was stored in global config for this account.")
        click.echo(f"  Location: {global_path}")
        click.echo()
        click.echo("Ready to use:")
        click.echo("  inspire config show     # Verify configuration")
        click.echo("  inspire resources list  # View available GPUs")
        click.echo("  inspire notebook list   # List notebooks")
        return
    click.echo("Next steps:")
    click.echo("  Run: inspire config show")


def _persist_discovery_catalog(request: _DiscoveryPersistRequest) -> dict[str, Any]:
    """Persist selector intent in TOML and rebuildable platform facts in state JSON."""
    global_path = Config.resolve_global_config_path()
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
    if not request.dry_run and not _confirm_discovery_writes(
        force=request.force,
        global_path=global_path,
        project_path=project_path,
        yes=request.yes,
    ):
        return {"cancelled": True}

    global_data, account_section = _load_discovery_global_state(
        global_path=global_path,
        account_alias=request.account_alias,
        platform_username=request.platform_username,
    )
    raw_workspaces = account_section.get("workspaces", {})
    raw_projects = account_section.get("projects", {})
    previous = load_account_discovery_state(request.account_alias)
    observed = observe_account_discovery(
        account_alias=request.account_alias,
        platform_username=request.platform_username,
        base_url=str(request.session.base_url or request.config.base_url or ""),
        session=request.session,
        browser_api_module=request.browser_api_module,
        fallback_workspace_id=request.workspace_id,
    )
    current = reconcile_account_discovery_state(previous, observed)
    workspace_selectors, workspace_errors = build_workspace_selectors(
        raw_workspaces=raw_workspaces,
        state=current,
        previous_state=previous,
    )

    prior_project_catalog = previous.project_catalog() if previous else {}
    project_catalog: dict[str, dict[str, Any]] = {
        project_id: dict(entry) for project_id, entry in prior_project_catalog.items()
    }
    for project_id, entry in current.project_catalog().items():
        project_catalog.setdefault(project_id, {}).update(entry)
    if not request.dry_run:
        _populate_project_catalog(
            project_catalog=project_catalog,
            projects=request.projects,
            browser_api_module=request.browser_api_module,
            session=request.session,
            workspace_id=request.workspace_id,
            account_key=request.platform_username,
            force=request.force,
        )
        _aliases, alias_for_id = _build_project_aliases(request.projects, existing={})
        if request.probe_shared_path:
            workspace_ids = {
                alias: str(resolution.get("last_seen_id") or "")
                for alias, resolution in current.workspace_aliases.items()
                if str(resolution.get("last_seen_id") or "").strip()
            }
            probe_defaults = _resolve_probe_defaults(
                config=request.config,
                merged_workspaces=workspace_ids,
                workspace_id=request.workspace_id,
                browser_api_module=request.browser_api_module,
                session=request.session,
                probe_pubkey=request.probe_pubkey,
            )
            _run_shared_path_probe(
                browser_api_module=request.browser_api_module,
                session=request.session,
                account_key=request.platform_username,
                projects=request.projects,
                project_catalog=project_catalog,
                alias_for_id=alias_for_id,
                force=request.force,
                probe_limit=request.probe_limit,
                probe_keep_notebooks=request.probe_keep_notebooks,
                probe_timeout=request.probe_timeout,
                probe_defaults=probe_defaults,
            )
        for project_workspace_id, observation in list(current.projects.items()):
            merged_items: list[dict[str, Any]] = []
            for item in observation.items:
                project_id = str(item.get("id") or item.get("project_id") or "").strip()
                merged = dict(project_catalog.get(project_id, {}))
                merged.update(item)
                for key in ("workdir", "shared_path_group", "probed_at", "probe_error"):
                    if key in project_catalog.get(project_id, {}):
                        merged[key] = project_catalog[project_id][key]
                merged_items.append(merged)
            current.projects[project_workspace_id] = replace(
                observation,
                items=tuple(merged_items),
            )

    project_selectors, project_errors = build_project_selectors(
        raw_projects=raw_projects,
        state=current,
        previous_state=previous,
    )
    if not request.dry_run and request.projects:
        _print_shared_path_group_summary(
            projects=request.projects,
            project_catalog=current.project_catalog(),
            alias_for_id=alias_for_id,
        )
    unresolved = workspace_errors + project_errors
    changes = state_diff(previous, current)
    changes.extend(
        {"action": "UNRESOLVED", "scope": "selector", "id": message} for message in unresolved
    )

    click.echo()
    click.echo(click.style("Discovery changes:", bold=True))
    if not changes:
        click.echo("  No changes")
    for change in changes:
        click.echo(f"  {change['action']:<14} {change['scope']:<16} {change['id']}")

    account_section["workspaces"] = workspace_selectors
    account_section["projects"] = project_selectors
    for legacy_key in ("compute_groups", "project_catalog"):
        account_section.pop(legacy_key, None)
    _persist_api_base_url(
        global_data=global_data,
        account_section=account_section,
        config=request.config,
    )
    _persist_prompted_credentials(
        account_section=account_section,
        prompted_credentials=request.prompted_credentials,
    )

    state_path = get_account_discovery_state_path(request.account_alias)
    report: dict[str, Any] = {
        "account": request.account_alias,
        "username": request.platform_username,
        "dry_run": request.dry_run,
        "status": current.status,
        "state_path": str(state_path),
        "diff": changes,
        "unresolved": unresolved,
    }
    if request.dry_run:
        click.echo(f"Dry run: no config or state files written ({state_path})")
    else:
        atomic_write_text(global_path, _toml_dumps(global_data))
        save_account_discovery_state(current)

    if unresolved:
        # An unresolved selector is kept in config and reported as UNRESOLVED, so
        # raising here would make every later run fail the same way with nothing
        # left to clean up; the run must still complete.
        warning = (
            f"{len(unresolved)} selector(s) unresolved; their previous selectors were kept. "
            f"Inspect {state_path} or run 'inspire config account doctor {request.account_alias}'."
        )
        report["warning"] = warning
        click.echo(click.style(f"Warning: {warning}", fg="yellow"))

    if current.status == "partial":
        persistence = "saved" if not request.dry_run else "observed"
        raise ValueError(
            f"Discovery {persistence} with status={current.status}; "
            f"inspect {state_path} or run "
            f"'inspire config account doctor {request.account_alias}'."
        )

    if not request.dry_run:
        project_catalog = current.project_catalog()
        target_dir = _prompt_target_dir(
            force=request.force,
            cli_target_dir=request.cli_target_dir,
            existing_target_dir=request.config.target_dir,
            selected_project=request.selected_project,
            project_catalog=project_catalog,
            yes=request.yes,
        )
        _write_discovered_project_config(
            project_path=project_path,
            config=request.config,
            account_alias=request.account_alias,
            target_dir=target_dir,
        )
        _ensure_ssh_key()
        _print_discover_completion(
            global_path=global_path,
            project_path=project_path,
            prompted_password=request.prompted_password,
        )
    return report


def _init_discover_mode(
    force: bool,
    *,
    probe_shared_path: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_pubkey: str | None,
    probe_timeout: int,
    cli_username: str | None = None,
    cli_base_url: str | None = None,
    cli_target_dir: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
    interactive: bool = True,
) -> dict[str, Any]:
    """Initialize per-account catalogs by discovering projects and compute groups."""
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web import session as web_session_module
    from inspire.platform.web.session import DEFAULT_WORKSPACE_ID

    config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    account_source = str(getattr(config, "_sources", {}).get("account_alias") or "")
    if not config.account_alias or account_source == SOURCE_INFERRED:
        raise ValueError(
            "Discover requires an explicit account alias. Pass --account <alias> or set "
            "[context].account before running --discover; account aliases are not usernames."
        )

    (
        session,
        prompted_credentials,
        prompted_password,
        account_alias,
        platform_username,
        workspace_id,
    ) = _resolve_discover_runtime(
        config=config,
        web_session_module=web_session_module,
        default_workspace_id=DEFAULT_WORKSPACE_ID,
        cli_username=cli_username,
        cli_base_url=cli_base_url,
    )

    click.echo(click.style("Discovering account catalog...", bold=True))
    click.echo(f"Account: {account_alias} ({platform_username})")
    click.echo(f"Workspace: {workspace_id}")
    projects: list[ProjectInfo]
    selected_project: ProjectInfo | None
    if dry_run:
        projects, selected_project = [], None
    else:
        projects, selected_project = _load_projects_for_discovery(
            browser_api_module=browser_api_module,
            session=session,
            workspace_id=workspace_id,
            force=force,
            probe_shared_path=probe_shared_path,
            probe_limit=probe_limit,
            yes=yes,
        )
    return _persist_discovery_catalog(
        _DiscoveryPersistRequest(
            force=force,
            config=config,
            browser_api_module=browser_api_module,
            session=session,
            account_alias=account_alias,
            platform_username=platform_username,
            workspace_id=workspace_id,
            projects=projects,
            selected_project=selected_project,
            probe_shared_path=probe_shared_path,
            probe_limit=probe_limit,
            probe_keep_notebooks=probe_keep_notebooks,
            probe_pubkey=probe_pubkey,
            probe_timeout=probe_timeout,
            prompted_credentials=prompted_credentials,
            prompted_password=prompted_password,
            cli_target_dir=cli_target_dir,
            dry_run=dry_run,
            yes=yes,
            interactive=interactive,
        )
    )
