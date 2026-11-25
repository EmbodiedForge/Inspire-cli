import base64
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import click
import tomlkit

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.logging_setup import redact_text
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    is_full_uuid,
    is_partial_id,
    normalize_partial,
    resolve_partial_id,
)
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.config.toml_write import atomic_write_toml, load_toml_document
from inspire.platform import tls_policy
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IMAGE_SOURCES = ("official", "public", "private", "personal-visible")
_SOURCE_CHOICES = _IMAGE_SOURCES + ("all",)
_FALLBACK_PUSH_REGISTRY = "docker-qb.sii.edu.cn"
_FALLBACK_PUSH_NAMESPACE = "inspire-studio"
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "set_cookie",
)


@dataclass
class _RegistryCredentials:
    registry: str = ""
    username: str = ""
    password: str = ""
    push_target: str = ""

    @property
    def redact_values(self) -> list[str]:
        return [value for value in (self.username, self.password) if value]


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _image_to_dict(img: browser_api_module.CustomImageInfo) -> dict:
    """Convert a CustomImageInfo to a plain dict for JSON output."""
    return {
        "image_id": img.image_id,
        "url": img.url,
        "name": img.name,
        "framework": img.framework,
        "version": img.version,
        "source": img.source,
        "status": img.status,
        "description": img.description,
        "created_at": img.created_at,
    }


def _resolve_image_id(
    ctx: Context,
    image_id: str,
    json_output: bool,
    session,
) -> str:
    """Resolve a full or partial image ID.

    Full UUIDs pass through; partial hex triggers a list + prefix match.
    """
    image_id = image_id.strip()

    if is_full_uuid(image_id):
        return image_id

    if not is_partial_id(image_id):
        return image_id  # not hex — let the API handle the error

    partial = normalize_partial(image_id)

    try:
        all_images: list[browser_api_module.CustomImageInfo] = []
        for src_key in _IMAGE_SOURCES:
            items = browser_api_module.list_images_by_source(source=src_key, session=session)
            all_images.extend(items)
    except Exception:
        return image_id  # can't list — pass through and let the API error

    matches: list[tuple[str, str]] = []
    seen: set[str] = set()
    for img in all_images:
        iid = img.image_id
        if iid in seen:
            continue
        seen.add(iid)
        if iid.lower().startswith(partial):
            label = img.name or img.status or ""
            matches.append((iid, label))

    if not matches:
        return image_id  # no match — pass through for API error

    return resolve_partial_id(ctx, partial, "image", matches, json_output)


def _image_name_version(image: browser_api_module.CustomImageInfo) -> tuple[str, str]:
    name = image.name
    version = image.version
    if ":" in name:
        base, tag = name.rsplit(":", 1)
        name = base
        version = version or tag
    if not version and ":" in image.url:
        version = image.url.rsplit(":", 1)[-1]
    if not name:
        raise ValueError("Image detail did not include a name")
    if not version:
        raise ValueError("Image detail did not include a version")
    return name, version


def _dedupe_images(images: list[browser_api_module.CustomImageInfo]) -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []
    for image in images:
        item = _image_to_dict(image)
        key = item.get("image_id") or item.get("url") or item.get("name")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        results.append(item)
    return results


def _image_matches_query(image: dict, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    fields = (
        image.get("image_id", ""),
        image.get("name", ""),
        image.get("url", ""),
        image.get("version", ""),
        image.get("description", ""),
    )
    return any(needle in str(value).lower() for value in fields if value)


def _visibility_value(visibility: str) -> str:
    return "VISIBILITY_PUBLIC" if visibility.lower() == "public" else "VISIBILITY_PRIVATE"


def _default_push_target(name: str, version: str) -> str:
    """Where an image goes when the platform did not tell us - the only place this shape is written.

    Two call sites previously spelled the same f-string inline, and deleting the registry constant left the
    suite green because no test ever reached the fallback. The default destination of `image save`/`image push`
    is not a detail: an image that lands in the wrong registry is either a failed push or a surprise for
    whoever pulls that name next, and a typo in a namespace is silent.
    """
    return f"{_FALLBACK_PUSH_REGISTRY}/{_FALLBACK_PUSH_NAMESPACE}/{name}:{version}"


def _push_target_from_result(result: dict, *, name: str, version: str) -> tuple[str, str, dict]:
    image_data = result.get("image", {}) if isinstance(result.get("image"), dict) else {}
    image_id = str(image_data.get("image_id") or result.get("image_id") or "")
    push_target = str(image_data.get("address") or result.get("address") or "")
    if not push_target:
        push_target = _default_push_target(name, version)
    return image_id, push_target, image_data


def _parse_local_image_ref(
    local_image: str, name: str | None, version: str | None
) -> tuple[str, str]:
    image_ref = local_image.strip()
    if not image_ref:
        raise click.UsageError("LOCAL_IMAGE cannot be empty.")
    base = image_ref
    tag = "latest"
    last_slash = image_ref.rfind("/")
    last_colon = image_ref.rfind(":")
    if last_colon > last_slash:
        base = image_ref[:last_colon]
        tag = image_ref[last_colon + 1 :] or "latest"
    base_name = base.rsplit("/", 1)[-1] or base
    return name or base_name, version or tag


def _redact_known_values(text: str, values: list[str] | tuple[str, ...] = ()) -> str:
    redacted = redact_text(text)
    for value in values:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def _render_robot_example(example_code: str, *, local_image: str, name: str, version: str) -> str:
    rendered = str(example_code or "")
    replacements = {
        "<local-image>": local_image,
        "<image-name>": name,
        "<image-version>": version,
        "${LOCAL_IMAGE}": local_image,
        "${IMAGE_NAME}": name,
        "${IMAGE_VERSION}": version,
        "{local_image}": local_image,
        "{image_name}": name,
        "{image_version}": version,
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _split_example_commands(example_code: str) -> list[list[str]]:
    commands: list[list[str]] = []
    normalized = str(example_code or "").replace("&&", "\n").replace(";", "\n")
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if tokens and tokens[0] == "sudo":
            tokens = tokens[1:]
        if tokens:
            commands.append(tokens)
    return commands


def _parse_docker_login(tokens: list[str]) -> tuple[str, str, str] | None:
    if len(tokens) < 3 or tokens[:2] != ["docker", "login"]:
        return None
    registry = ""
    username = ""
    password = ""
    i = 2
    while i < len(tokens):
        token = tokens[i]
        if token in {"-u", "--username"} and i + 1 < len(tokens):
            username = tokens[i + 1]
            i += 2
            continue
        if token.startswith("--username="):
            username = token.split("=", 1)[1]
            i += 1
            continue
        if token in {"-p", "--password"} and i + 1 < len(tokens):
            password = tokens[i + 1]
            i += 2
            continue
        if token.startswith("--password="):
            password = token.split("=", 1)[1]
            i += 1
            continue
        if not token.startswith("-"):
            registry = token
        i += 1
    if registry and username and password:
        return registry, username, password
    return None


def _parse_docker_login_registry(tokens: list[str]) -> str:
    if len(tokens) < 3 or tokens[:2] != ["docker", "login"]:
        return ""
    registry = ""
    i = 2
    while i < len(tokens):
        token = tokens[i]
        if token in {"-u", "--username", "-p", "--password"}:
            i += 2
            continue
        if token in {"--password-stdin"}:
            i += 1
            continue
        if not token.startswith("-"):
            registry = token
        i += 1
    return registry


def _docker_ref_from_token(token: str) -> str:
    value = token.removeprefix("docker://")
    return value if "/" in value and ":" in value.rsplit("/", 1)[-1] else ""


def _parse_push_target(tokens: list[str]) -> str:
    if len(tokens) >= 3 and tokens[:2] == ["docker", "push"]:
        return _docker_ref_from_token(tokens[-1])
    if len(tokens) >= 4 and tokens[:2] == ["docker", "tag"]:
        return _docker_ref_from_token(tokens[-1])
    if len(tokens) >= 4 and tokens[:2] == ["skopeo", "copy"]:
        refs = [_docker_ref_from_token(token) for token in tokens if token.startswith("docker://")]
        refs = [ref for ref in refs if ref]
        if refs:
            return refs[-1]
    return ""


def _parse_registry_credentials(
    robot_result: dict[str, Any],
    *,
    local_image: str,
    name: str,
    version: str,
) -> _RegistryCredentials:
    robot_username = str(robot_result.get("robot_name") or robot_result.get("robotName") or "")
    robot_password = str(robot_result.get("robot_secret") or robot_result.get("robotSecret") or "")
    example_code = str(
        robot_result.get("example_code")
        or robot_result.get("exampleCode")
        or robot_result.get("example")
        or ""
    )
    rendered = _render_robot_example(
        example_code,
        local_image=local_image,
        name=name,
        version=version,
    )
    credentials = _RegistryCredentials()
    for tokens in _split_example_commands(rendered):
        login = _parse_docker_login(tokens)
        if login is not None:
            credentials.registry, credentials.username, credentials.password = login
        elif tokens[:2] == ["docker", "login"]:
            credentials.registry = _parse_docker_login_registry(tokens)
        push_target = _parse_push_target(tokens)
        if push_target:
            credentials.push_target = push_target
    if credentials.registry and not credentials.username and robot_username and robot_password:
        credentials.username = robot_username
        credentials.password = robot_password
    return credentials


def _echo_command_output(
    stdout: str,
    stderr: str,
    *,
    redact_values: list[str] | tuple[str, ...] = (),
) -> None:
    for output in (stdout, stderr):
        text = _redact_known_values(output, redact_values)
        if text:
            click.echo(text, err=True, nl=not text.endswith("\n"))


def _run_local_image_command(
    cmd: list[str],
    *,
    input_text: str | None = None,
    redact_values: list[str] | tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - explicit user-triggered local image command
        cmd,
        check=False,
        capture_output=True,
        text=True,
        input=input_text,
    )
    _echo_command_output(result.stdout or "", result.stderr or "", redact_values=redact_values)
    if result.returncode != 0:
        rendered = shlex.join(cmd)
        message = _redact_known_values(
            f"Local command failed ({result.returncode}): {rendered}\n{result.stderr or result.stdout}",
            redact_values,
        )
        raise RuntimeError(message.strip())
    return result


def _push_with_docker(
    local_image: str,
    push_target: str,
    credentials: _RegistryCredentials | None,
) -> str:
    info = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
    )
    if info.returncode != 0:
        return ""
    redact_values = credentials.redact_values if credentials is not None else []
    if credentials and credentials.registry and credentials.username and credentials.password:
        click.echo(f"Logging in to registry: {redact_text(credentials.registry)}", err=True)
        _run_local_image_command(
            [
                "docker",
                "login",
                credentials.registry,
                "-u",
                credentials.username,
                "--password-stdin",
            ],
            input_text=f"{credentials.password}\n",
            redact_values=redact_values,
        )
    click.echo(f"Tagging image for platform registry: {redact_text(push_target)}", err=True)
    _run_local_image_command(
        ["docker", "tag", local_image, push_target],
        redact_values=redact_values,
    )
    click.echo(f"Pushing image: {redact_text(push_target)}", err=True)
    _run_local_image_command(
        ["docker", "push", push_target],
        redact_values=redact_values,
    )
    return "docker"


def _skopeo_dest_tls_args() -> list[str]:
    """`--dest-tls-verify` for the push: verification unless the operator opted out.

    `docker-qb.sii.edu.cn`, the fallback registry and the one the platform's push examples name,
    serves the same publicly trusted `*.sii.edu.cn` chain as the API host, so an unverified push
    was a choice the CLI made, not a property of the registry. `docker push` - the path used when a
    daemon is available - never passed a tls flag at all, so this also brings the two paths in line.
    """
    if tls_policy.skip_verification():
        tls_policy.warn_insecure_channel("registry push")
        return ["--dest-tls-verify=false"]
    return ["--dest-tls-verify=true"]


@contextmanager
def _skopeo_dest_auth_args(credentials: _RegistryCredentials | None) -> Iterator[list[str]]:
    """`--dest-authfile` for `credentials`, with the secret in a 0600 file instead of argv.

    `--dest-creds user:secret` puts the robot secret in the process arguments, where any other
    process of the same uid can read it out of /proc and a process-log collector records it. The
    docker path never did that - it pipes the password on stdin - and skopeo has no stdin mode, so
    the documented equivalent is a credentials file (skopeo-copy(1), `--dest-authfile`). The file is
    keyed by registry host and removed as soon as both copy attempts are done.
    """
    if not credentials or not credentials.username or not credentials.password:
        yield []
        return
    hosts = {credentials.registry.strip(), credentials.push_target.split("/")[0].strip()}
    hosts = {host.removeprefix("https://").removeprefix("http://").rstrip("/") for host in hosts}
    hosts.discard("")
    if not hosts:
        # No host to key the credentials by: rather than guess, push anonymously and let the
        # registry answer 401, which is a legible failure next to wrong-host credentials.
        yield []
        return
    token = base64.b64encode(
        f"{credentials.username}:{credentials.password}".encode("utf-8")
    ).decode("ascii")
    payload = json.dumps({"auths": {host: {"auth": token} for host in sorted(hosts)}})
    fd, path = tempfile.mkstemp(prefix="inspire-skopeo-auth-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        yield ["--dest-authfile", path]
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _push_with_skopeo(
    local_image: str,
    push_target: str,
    credentials: _RegistryCredentials | None,
) -> str:
    if not shutil.which("skopeo"):
        raise RuntimeError("docker is unavailable and skopeo is not installed.")
    src = (
        f"docker-daemon:{local_image}"
        if ":" in local_image
        else f"docker-daemon:{local_image}:latest"
    )
    dest = f"docker://{push_target}"
    redact_values = credentials.redact_values if credentials is not None else []
    dest_tls = _skopeo_dest_tls_args()
    click.echo(f"Copying image with skopeo: {redact_text(push_target)}", err=True)
    with _skopeo_dest_auth_args(credentials) as dest_auth:
        first = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [
                "skopeo",
                "copy",
                "--override-arch",
                "amd64",
                src,
                dest,
                *dest_auth,
                *dest_tls,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        _echo_command_output(first.stdout or "", first.stderr or "", redact_values=redact_values)
        if first.returncode == 0:
            return "skopeo"
        fallback = [
            "skopeo",
            "copy",
            "--override-arch",
            "amd64",
            f"docker://{local_image}",
            dest,
            *dest_auth,
            *dest_tls,
        ]
        _run_local_image_command(fallback, redact_values=redact_values)
    return "skopeo"


def _push_local_image(
    local_image: str,
    push_target: str,
    credentials: _RegistryCredentials | None,
) -> str:
    method = _push_with_docker(local_image, push_target, credentials)
    if method:
        return method
    return _push_with_skopeo(local_image, push_target, credentials)


def _find_image_by_name_version(
    *,
    name: str,
    version: str,
    workspace_id: str | None,
    session,
) -> browser_api_module.CustomImageInfo | None:
    for source in ("private", "public", "personal-visible"):
        try:
            images = browser_api_module.list_images_by_source(
                source=source,
                workspace_id=workspace_id,
                session=session,
            )
        except Exception:
            continue
        for image in images:
            image_name = image.name
            image_version = image.version
            if ":" in image_name and not image_version:
                image_name, image_version = image_name.rsplit(":", 1)
            if image_name == name and image_version == version:
                return image
            if image.name == f"{name}:{version}":
                return image
    return None


# ---------------------------------------------------------------------------
# list / search
# ---------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--source",
    "-s",
    type=click.Choice(_SOURCE_CHOICES, case_sensitive=False),
    default="official",
    show_default=True,
    help="Image origin filter (provenance, NOT who can read it)",
)
@click.option(
    "--visibility",
    type=click.Choice(["public", "private"], case_sensitive=False),
    default=None,
    help="Filter by who can read the image - the axis --source does not describe. "
    "Combine with --source all to audit exposure.",
)
@pass_context
def list_images_cmd(
    ctx: Context,
    source: str,
    visibility: Optional[str],
) -> None:
    """List available Docker images.

    \b
    Examples:
        inspire image list                              # Official images
        inspire image list --source personal-visible    # Your saved/custom images
        inspire image list --source private             # Dead view (platform filter resolves to 0 rows)
        inspire image list --source all                 # All sources
        inspire image list --source all --visibility public   # What the institution can read
        inspire --json image list --source all          # JSON output
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Listing images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    results: list[dict] = []
    duplicates = 0

    # Two axes get conflated constantly, including in a written incident report by us: the `source` field on
    # an image is provenance, while who-can-read-it is `filter.visibility`, chosen by the view queried here.
    # Only the visibility-scoped views have a defensible answer, so anything else reports nothing at all -
    # absent beats invented.
    visibility_of_query = visibility or {"public": "public", "personal-visible": "private"}.get(
        source
    )

    try:
        if source == "all":
            # `all` concatenates per-source queries, and once visibility is pinned those views stop being
            # disjoint: with --visibility private the public/private/personal-visible filters resolve to
            # the same records, so the naive concatenation reported 23 rows for 20 images and a total that
            # agreed with the wrong number. Deduplicate on identity and let total follow the deduplicated
            # rows, so a count-based assertion cannot pass on inflated data.
            seen_ids: set[str] = set()
            for src_key in _IMAGE_SOURCES:
                items = browser_api_module.list_images_by_source(
                    source=src_key, session=session, visibility=visibility
                )
                rows = [_image_to_dict(img) for img in items]
                view = (
                    visibility_of_query
                    if visibility
                    else {"public": "public", "personal-visible": "private"}.get(src_key)
                )
                if view:
                    for row in rows:
                        row["visibility"] = view
                for row in rows:
                    key = str(row.get("image_id") or row.get("url") or "")
                    if key and key in seen_ids:
                        duplicates += 1
                        continue
                    if key:
                        seen_ids.add(key)
                    results.append(row)
            if duplicates and not json_output:
                # Stated rather than silent: overlapping views are expected once both axes are pinned, but a
                # reader comparing this against a single-source query should know why the numbers differ.
                # Machine readers get `duplicates_skipped` in the payload instead of a line on stderr, which
                # would otherwise corrupt the JSON stream for anything that merges the two.
                click.secho(
                    f"Skipped {duplicates} duplicate record(s): with both --source all and --visibility, "
                    "the platform's source views overlap and describe the same images.",
                    fg="yellow",
                    err=True,
                )
        else:
            items = browser_api_module.list_images_by_source(
                source=source, session=session, visibility=visibility
            )
            results = [_image_to_dict(img) for img in items]
            view = visibility_of_query
            if view:
                for row in results:
                    row["visibility"] = view
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list images: {e}", EXIT_API_ERROR)
        return

    if not results and not json_output:
        # A default view that hides custom images produced ten silent ticks in a field monitor: the saved
        # image was SUCCESS the whole time, the query simply never looked at the source it lived in, and the
        # alarm concluded MISSING. An empty scoped query must name its own scope and the wider query, because
        # "no rows" and "no such record" are different claims and only the caller can tell them apart.
        scope = f"source={source}"
        click.secho(
            f"No images in the current view ({scope}). An empty view is not evidence the image does not "
            "exist. Saved images live under `--source personal-visible` or `--source all`; `--source private` "
            "is a platform filter that resolves to nothing for many accounts, so treat its empty result as "
            "the filter, not as the inventory.",
            fg="yellow",
            err=True,
        )

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "items": results,
                    "total": len(results),
                    "source": source,
                    "visibility_of_query": visibility_of_query,
                    "duplicates_skipped": duplicates if source == "all" else 0,
                    "empty_because_scope": (not results) and source != "all",
                }
            )
        )
        return

    click.echo(human_formatter.format_image_list(results))


@click.command("search")
@click.argument("query", required=False, default="")
@click.option(
    "--source",
    "-s",
    type=click.Choice(_SOURCE_CHOICES, case_sensitive=False),
    default="all",
    show_default=True,
    help="Image source filter",
)
@click.option("--workspace-id", default=None, help="Workspace ID for picker-visible images.")
@pass_context
def search_images_cmd(
    ctx: Context,
    query: str,
    source: str,
    workspace_id: str | None,
) -> None:
    """Search images visible to create-form image pickers."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(
        ctx,
        hint=(
            "Searching images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)

    try:
        images: list[browser_api_module.CustomImageInfo] = []
        sources = _IMAGE_SOURCES if source == "all" else (source,)
        for src_key in sources:
            images.extend(
                browser_api_module.list_images_by_source(
                    source=src_key,
                    workspace_id=resolved_workspace_id,
                    session=session,
                )
            )
        results = [item for item in _dedupe_images(images) if _image_matches_query(item, query)]
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to search images: {e}", EXIT_API_ERROR)
        return

    payload = {
        "query": query,
        "source": source,
        "workspace_id": resolved_workspace_id,
        "images": results,
        "total": len(results),
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(human_formatter.format_image_list(results))


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


@click.command("detail")
@click.argument("image_id")
@pass_context
def image_detail(
    ctx: Context,
    image_id: str,
) -> None:
    """Show detailed information about an image.

    \b
    Examples:
        inspire image detail <image-id>
        inspire --json image detail <image-id>
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Image detail requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    image_id = _resolve_image_id(ctx, image_id, json_output, session)

    try:
        image = browser_api_module.get_image_detail(image_id=image_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to get image detail: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(json_formatter.format_json(_image_to_dict(image)))
        return

    click.echo(human_formatter.format_image_detail(_image_to_dict(image)))


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


@click.command("register")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Image name (lowercase, digits, dashes, dots, underscores)",
)
@click.option(
    "--version",
    "-v",
    required=True,
    help="Image version tag (e.g., v1.0)",
)
@click.option(
    "--description",
    "-d",
    default="",
    help="Image description",
)
@click.option(
    "--visibility",
    type=click.Choice(["private", "public"], case_sensitive=False),
    default="private",
    show_default=True,
    help="Image visibility",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for the image to become usable (READY or SUCCESS)",
)
@pass_context
def register_image_cmd(
    ctx: Context,
    name: str,
    version: str,
    description: str,
    visibility: str,
    wait: bool,
) -> None:
    """Register an external Docker image on the platform.

    This is for images you built outside the platform. To save a running
    notebook as an image, use 'inspire image save' instead.

    \b
    Workflow:
      1. inspire image register -n my-img -v v1.0
      2. docker tag <local-image> <registry-url>   (shown in output)
      3. docker push <registry-url>
      4. Platform detects the push and marks the image READY/SUCCESS.

    \b
    Examples:
        inspire image register -n my-pytorch -v v1.0
        inspire image register -n my-img -v v1.0 --visibility public --wait
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Registering images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    visibility_value = (
        "VISIBILITY_PUBLIC" if visibility.lower() == "public" else "VISIBILITY_PRIVATE"
    )

    try:
        result = browser_api_module.create_image(
            name=name,
            version=version,
            description=description,
            visibility=visibility_value,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to register image: {e}", EXIT_API_ERROR)
        return

    image_data = result.get("image", {})
    image_id = image_data.get("image_id", "") or result.get("image_id", "")
    registry_url = image_data.get("address", "") or result.get("address", "")

    if wait and image_id:
        if not json_output:
            click.echo(f"Image '{image_id}' registered. Waiting for it to become usable...")
        try:
            ready = browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            if not json_output:
                click.echo(f"Image '{image_id}' is usable (status: {ready.status}).")
        except (TimeoutError, ValueError) as e:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return

    if json_output:
        click.echo(json_formatter.format_json({"image_id": image_id, "result": result}))
        return

    click.echo(f"Image registered: {image_id or 'unknown'}")
    if registry_url:
        click.echo("\nTo push your image:")
        click.echo(f"  docker tag <local-image> {registry_url}")
        click.echo(f"  docker push {registry_url}")
    if not wait and image_id:
        click.echo(f"\nUse 'inspire image detail {image_id}' to check status.")


@click.command("create")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Image name (lowercase, digits, dashes, dots, underscores)",
)
@click.option("--version", "-v", required=True, help="Image version tag.")
@click.option("--description", "-d", default="", help="Image description.")
@click.option(
    "--visibility",
    type=click.Choice(["private", "public"], case_sensitive=False),
    default="private",
    show_default=True,
    help="Image visibility.",
)
@click.option("--workspace-id", default=None, help="Workspace ID. Defaults to current session.")
@pass_context
def create_image_cmd(
    ctx: Context,
    name: str,
    version: str,
    description: str,
    visibility: str,
    workspace_id: str | None,
) -> None:
    """Create a DIRECT_PUSH image record and print the registry push target."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Creating images requires web authentication.")
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)
    try:
        result = browser_api_module.create_image(
            name=name,
            version=version,
            workspace_id=resolved_workspace_id,
            description=description,
            visibility=_visibility_value(visibility),
            add_method="DIRECT_PUSH",
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to create image record: {e}", EXIT_API_ERROR)
        return

    image_id, push_target, image_data = _push_target_from_result(
        result,
        name=name,
        version=version,
    )
    payload = {
        "image_id": image_id,
        "image": image_data.get("name") or f"{name}:{version}",
        "name": name,
        "version": version,
        "workspace_id": resolved_workspace_id,
        "visibility": _visibility_value(visibility),
        "push_target": redact_text(push_target),
        "result": _redact_value(result),
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Image record created: {image_id or 'unknown'}")
    click.echo(f"Push target: {redact_text(push_target)}")


@click.command("push")
@click.argument("local_image")
@click.option("--name", "-n", default=None, help="Platform image name.")
@click.option("--version", "-v", default=None, help="Platform image version tag.")
@click.option("--description", "-d", default="", help="Image description.")
@click.option(
    "--visibility",
    type=click.Choice(["private", "public"], case_sensitive=False),
    default="private",
    show_default=True,
    help="Image visibility.",
)
@click.option("--workspace-id", default=None, help="Workspace ID. Defaults to current session.")
@pass_context
def push_image_cmd(
    ctx: Context,
    local_image: str,
    name: str | None,
    version: str | None,
    description: str,
    visibility: str,
    workspace_id: str | None,
) -> None:
    """Create or reuse a DIRECT_PUSH image record, push locally, then sync."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Pushing images requires web authentication.")
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)
    sync_workspace_id = resolved_workspace_id or DEFAULT_WORKSPACE_ID
    try:
        image_name, image_version = _parse_local_image_ref(local_image, name, version)
    except click.UsageError:
        raise

    try:
        robot_result = browser_api_module.get_image_robot_credentials(
            image_name=image_name,
            image_version=image_version,
            workspace_id=resolved_workspace_id,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to get registry credentials: {e}", EXIT_API_ERROR)
        return

    registry_credentials = _parse_registry_credentials(
        robot_result,
        local_image=local_image,
        name=image_name,
        version=image_version,
    )
    preflight_push_target = registry_credentials.push_target
    if preflight_push_target:
        click.echo(f"Registry push target: {redact_text(preflight_push_target)}", err=True)
    if not (registry_credentials.username and registry_credentials.password):
        click.echo(
            "Registry robot returned no login secret; using existing Docker credentials.",
            err=True,
        )

    registered_step = "created"
    try:
        result = browser_api_module.create_image(
            name=image_name,
            version=image_version,
            workspace_id=resolved_workspace_id,
            description=description,
            visibility=_visibility_value(visibility),
            add_method="DIRECT_PUSH",
            session=session,
        )
        image_id, push_target, image_data = _push_target_from_result(
            result,
            name=image_name,
            version=image_version,
        )
    except Exception as create_error:
        existing = _find_image_by_name_version(
            name=image_name,
            version=image_version,
            workspace_id=resolved_workspace_id,
            session=session,
        )
        if existing is None:
            _handle_error(
                ctx,
                "APIError",
                f"Failed to create image record: {create_error}",
                EXIT_API_ERROR,
            )
            return
        if existing.status.upper() in browser_api_module.IMAGE_READY_STATUSES:
            _handle_error(
                ctx,
                "ImageExists",
                (
                    f"Image {image_name}:{image_version} already exists "
                    f"with status {existing.status}."
                ),
                EXIT_VALIDATION_ERROR,
                hint="Use a different --version or delete the existing image record first.",
            )
            return
        image_id = existing.image_id
        push_target = existing.url or _default_push_target(image_name, image_version)
        image_data = _image_to_dict(existing)
        result = {"image": image_data, "reused": True}
        registered_step = "reused"

    if not push_target:
        _handle_error(
            ctx,
            "APIError",
            "Image record did not include a registry push target.",
            EXIT_API_ERROR,
        )
        return

    click.echo(
        f"Image record {registered_step}: {image_id or 'unknown'} -> {redact_text(push_target)}",
        err=True,
    )
    if registry_credentials.push_target:
        push_target = registry_credentials.push_target
    try:
        push_method = _push_local_image(local_image, push_target, registry_credentials)
    except Exception as e:
        _handle_error(ctx, "PushError", str(e), EXIT_API_ERROR)
        return

    try:
        sync_result = browser_api_module.sync_image(
            workspace_id=sync_workspace_id,
            image_name=image_name,
            image_version=image_version,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Image pushed but sync failed: {e}", EXIT_API_ERROR)
        return

    payload = {
        "image_id": image_id,
        "image": image_data.get("name") or f"{image_name}:{image_version}",
        "name": image_name,
        "version": image_version,
        "workspace_id": resolved_workspace_id,
        "push_target": redact_text(push_target),
        "push_method": push_method,
        "registry_credentials": "robot" if registry_credentials.username else "not_provided",
        "status": "pushed",
        "result": _redact_value(result),
        "sync_result": _redact_value(sync_result),
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(json_formatter.format_json(payload))


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


_NOTEBOOK_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _resolve_save_target(ctx: Context, session: Any, target: str) -> str:
    """Turn whatever the user typed into the notebook id `/mirror/save` demands.

    The endpoint resolves notebooks by id and answers `100003 记录不存在` for anything else, which reads like
    "this notebook does not exist" and sends readers to look for a missing record, a wrong workspace or an
    unprovisioned image service. Two people each burned a submission on that wording, because the name printed
    by `inspire notebook list` was forwarded verbatim.

    A value that already looks like a UUID is used as-is - no extra platform call. Anything else goes through
    the same resolver `notebook exec`/`doctor` use, so names and unambiguous prefixes work here the way they
    already work elsewhere in this CLI, with one code path deciding what is ambiguous.
    """
    value = (target or "").strip()
    if not value:
        raise ValueError("a notebook id or name is required")
    if _NOTEBOOK_ID_RE.match(value):
        return value
    from inspire.cli.commands.notebook.notebook_lookup import _resolve_notebook_id

    resolved, _name = _resolve_notebook_id(
        ctx,
        session=session,
        config=load_config(ctx),
        base_url=get_base_url(),
        identifier=value,
        json_output=False,
    )
    if not resolved:
        raise ValueError(f"could not resolve {value!r} to a notebook id")
    return str(resolved)


@click.command("save")
@click.option(
    "--accessible",
    type=click.Choice(["personal", "public"]),
    default="personal",
    show_default=True,
    help="Who can see the saved image. Omitting this on the platform used to file the image as PUBLIC, so "
    "the narrow choice is the default and the wide one is deliberate.",
)
@click.argument("notebook_id")
@click.option(
    "--name",
    "-n",
    required=True,
    help=(
        "Name for the saved image - the name alone, no tag. The tag belongs to --version; the platform "
        "composes `name:version` itself, and a colon inside the name is rejected as an invalid name."
    ),
)
@click.option(
    "--version",
    "-v",
    default="v1",
    show_default=True,
    help="Image version tag",
)
@click.option(
    "--description",
    "-d",
    default="",
    help="Image description",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for the image to become usable (READY or SUCCESS)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve the notebook and print the platform request (secrets redacted) without saving an image",
)
@pass_context
def save_image_cmd(
    ctx: Context,
    notebook_id: str,
    name: str,
    version: str,
    description: str,
    wait: bool,
    accessible: str,
    dry_run: bool = False,
) -> None:
    """Save a running notebook as a custom Docker image.

    \b
    Examples:
        inspire image save <notebook-id> -n my-saved-image
        inspire image save <notebook-id> -n my-img -v v2 --wait
        inspire image save <notebook-id> -n my-img --dry-run
    """
    # A tag belongs to --version, and saying so here beats forwarding a name the platform rejects with error
    # 400001 "invalid image name or version" - a message naming neither field nor fix, which stalled the field
    # team's migration off a removed --image-name flag. Fail locally, name both fields, propose the split.
    if ":" in name:
        head, _, tail = name.partition(":")
        raise click.UsageError(
            f"--name {name!r} contains a colon. Pass the name alone to --name and the tag to --version: "
            f"--name {head} --version {tail}"
        )

    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Saving images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    try:
        resolved_id = _resolve_save_target(ctx, session, notebook_id)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - resolution problems are validation, not API failures
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return

    if dry_run:
        preview = {
            "kind": "image_save",
            "mutation": "skipped",
            "intent": {
                "notebook": notebook_id,
                "name": name,
                "version": version,
                "accessible": accessible,
            },
            "resolved": {"notebook_id": resolved_id},
            "platform_request": _redact_value(
                {
                    "notebook_id": resolved_id,
                    "accessible": 2 if accessible == "public" else 1,
                    "name": name,
                    "version": version,
                    "description": description,
                }
            ),
            "warnings": [],
        }
        if json_output:
            click.echo(json_formatter.format_json(preview))
        else:
            click.echo("Image Save Preview\n")
            click.echo(f"Notebook:   {resolved_id}")
            click.echo(f"Name:       {name}")
            click.echo(f"Version:    {version}")
            click.echo(f"Accessible: {accessible}")
            click.echo("Mutation:   skipped")
            click.echo("No image was saved.")
        return

    try:
        result = browser_api_module.save_notebook_as_image(
            notebook_id=resolved_id,
            accessible=2 if accessible == "public" else 1,
            name=name,
            version=version,
            description=description,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to save notebook as image: {e}", EXIT_API_ERROR)
        return

    image_id = result.get("image", {}).get("image_id", "") or result.get("image_id", "")

    if wait and image_id:
        if not json_output:
            click.echo(f"Image '{image_id}' is being saved. Waiting for it to become usable...")
        try:
            ready = browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            if not json_output:
                click.echo(f"Image '{image_id}' is usable (status: {ready.status}).")
        except (TimeoutError, ValueError) as e:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return

    if json_output:
        click.echo(json_formatter.format_json({"image_id": image_id, "result": result}))
        return

    click.echo(f"Notebook saved as image: {image_id or 'unknown'}")
    if not wait and image_id:
        click.echo(f"Use 'inspire image detail {image_id}' to check build status.")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@click.command("delete")
@click.argument("image_id")
@click.option(
    "--yes",
    "--force",
    "force",
    is_flag=True,
    help="Skip confirmation prompt (alias: --yes)",
)
@pass_context
def delete_image_cmd(
    ctx: Context,
    image_id: str,
    force: bool,
) -> None:
    """Delete a custom Docker image.

    \b
    Examples:
        inspire image delete <image-id>
        inspire image delete <image-id> --force
    """
    if not confirmed_or_error(ctx, assume_yes=force, subject=f"delete image {image_id}"):
        raise click.Abort()

    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Deleting images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    image_id = _resolve_image_id(ctx, image_id, json_output, session)

    try:
        result = browser_api_module.delete_image(image_id=image_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to delete image: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {"image_id": image_id, "status": "deleted", "result": result}
            )
        )
        return

    click.echo(f"Image '{image_id}' has been deleted.")


# ---------------------------------------------------------------------------
# sync / preheat
# ---------------------------------------------------------------------------


@click.command("sync")
@click.argument("image_id")
@click.option("--workspace-id", default=None, help="Workspace ID. Defaults to current session.")
@pass_context
def sync_image_cmd(
    ctx: Context,
    image_id: str,
    workspace_id: str | None,
) -> None:
    """Trigger platform image synchronization."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Syncing images requires web authentication.")
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)
    if not resolved_workspace_id:
        _handle_error(ctx, "ValidationError", "--workspace-id is required", EXIT_VALIDATION_ERROR)
        return

    image_id = _resolve_image_id(ctx, image_id, json_output, session)
    try:
        image = browser_api_module.get_image_detail(image_id=image_id, session=session)
        image_name, image_version = _image_name_version(image)
        result = browser_api_module.sync_image(
            workspace_id=resolved_workspace_id,
            image_name=image_name,
            image_version=image_version,
            session=session,
        )
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", f"Failed to sync image: {e}", EXIT_API_ERROR)
        return

    payload = {
        "image_id": image_id,
        "image": image.name,
        "workspace_id": resolved_workspace_id,
        "status": "sync_requested",
        "result": result,
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Image sync requested: {image.name} ({resolved_workspace_id})")


@click.command("preheat")
@click.argument("image_id")
@click.option(
    "--logic-compute-group-id",
    "--lcg-id",
    required=True,
    help="Target logic compute group ID.",
)
@pass_context
def preheat_image_cmd(
    ctx: Context,
    image_id: str,
    logic_compute_group_id: str,
) -> None:
    """Preheat/precache an image on a compute group."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Preheating images requires web authentication.")
    image_id = _resolve_image_id(ctx, image_id, json_output, session)
    try:
        image = browser_api_module.get_image_detail(image_id=image_id, session=session)
        image_name, image_version = _image_name_version(image)
        result = browser_api_module.preheat_image(
            name=image_name,
            version=image_version,
            logic_compute_group_id=logic_compute_group_id,
            address=image.url,
            session=session,
        )
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", f"Failed to preheat image: {e}", EXIT_API_ERROR)
        return

    payload = {
        "image_id": image_id,
        "image": image.name,
        "logic_compute_group_id": logic_compute_group_id,
        "status": "preheat_requested",
        "result": result,
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Image preheat requested: {image.name} ({logic_compute_group_id})")


# ---------------------------------------------------------------------------
# set-default
# ---------------------------------------------------------------------------


@click.command("set-default")
@click.option(
    "--job",
    "job_image",
    default=None,
    help="Set default image for jobs (written to [job].image in .inspire/config.toml)",
)
@click.option(
    "--notebook",
    "notebook_image",
    default=None,
    help="Set default image for notebooks (written to [notebook].image in .inspire/config.toml)",
)
@pass_context
def set_default_image_cmd(
    ctx: Context,
    job_image: Optional[str],
    notebook_image: Optional[str],
) -> None:
    """Save image preferences to .inspire/config.toml.

    \b
    Examples:
        inspire image set-default --job my-pytorch-image
        inspire image set-default --notebook my-notebook-image
        inspire image set-default --job img1 --notebook img2
    """
    json_output = resolve_json_output(ctx, False)

    if not job_image and not notebook_image:
        _handle_error(
            ctx,
            "ValidationError",
            "Specify at least one of --job or --notebook.",
            EXIT_VALIDATION_ERROR,
        )
        return

    # Locate the existing project config (walks up directory tree).
    # Fall back to CWD-relative path if no project config exists yet.
    from inspire.config.toml import _find_project_config

    existing_config = _find_project_config()
    config_path = existing_config if existing_config else Path(".inspire") / "config.toml"

    # Read existing config if present. A file that cannot be parsed must abort the command:
    # treating it as empty here would replace the user's config with only the image keys.
    # The document is kept as a tomlkit tree rather than a plain dict so the user's comments,
    # section order, and formatting survive the rewrite untouched.
    try:
        document = load_toml_document(config_path)
    except Exception as e:
        _handle_error(
            ctx,
            "ConfigError",
            f"Failed to read {config_path}: {e}. Fix the file or remove it; refusing to overwrite.",
            EXIT_CONFIG_ERROR,
        )
        return

    # Update the relevant sections
    updated: dict[str, str] = {}
    for section, image in (("job", job_image), ("notebook", notebook_image)):
        if not image:
            continue
        table = document.get(section)
        if table is None:
            table = tomlkit.table()
            document[section] = table
        if not isinstance(table, dict):
            _handle_error(
                ctx,
                "ConfigError",
                f"[{section}] in {config_path} is not a table; refusing to overwrite it.",
                EXIT_CONFIG_ERROR,
            )
            return
        table["image"] = image
        updated[f"{section}.image"] = image

    # Write back
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_toml(config_path, document)
    except Exception as e:
        _handle_error(ctx, "ConfigError", f"Failed to write config: {e}", EXIT_CONFIG_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json({"updated": updated, "config_path": str(config_path)})
        )
        return

    for key, value in updated.items():
        click.echo(f"Set {key} = {value!r} in {config_path}")


__all__ = [
    "delete_image_cmd",
    "image_detail",
    "list_images_cmd",
    "register_image_cmd",
    "save_image_cmd",
    "set_default_image_cmd",
]
