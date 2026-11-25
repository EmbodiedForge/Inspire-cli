"""Config check command – validates environment and authentication."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_error, emit_info, emit_success, emit_warning
from inspire.config.audit import audit_config_files
from inspire.config import (
    CONFIG_OPTIONS,
    Config,
    ConfigError,
    SOURCE_DEFAULT,
    SOURCE_ENV,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLACEHOLDER_HOSTS = {
    "api.example.com",
    "example.com",
    "example.org",
    "example.net",
}
_PLACEHOLDER_HOST_SUFFIXES = (
    ".example.com",
    ".example.org",
    ".example.net",
)
_HOST_VALIDATION_FIELDS = (
    ("base_url", "INSPIRE_BASE_URL"),
    ("gitea_server", "INSP_GITEA_SERVER"),
    ("github_server", "INSP_GITHUB_SERVER"),
    ("docker_registry", "INSPIRE_DOCKER_REGISTRY"),
    ("rtunnel_download_url", "INSPIRE_RTUNNEL_DOWNLOAD_URL"),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_precedence(prefer_source: str) -> str:
    if prefer_source == "toml":
        return "project TOML wins on conflict"
    return "env vars win on conflict (default)"


def _extract_hostname(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text or text.startswith("/"):
        return None

    if "://" in text:
        parsed = urlsplit(text)
        return parsed.hostname.lower() if parsed.hostname else None

    if text.startswith("//"):
        parsed = urlsplit(f"https:{text}")
        return parsed.hostname.lower() if parsed.hostname else None

    candidate = text.split("/", 1)[0].strip()
    if not candidate or " " in candidate:
        return None
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    if "." not in candidate:
        return None
    return candidate.lower()


def _is_placeholder_host(host: str) -> bool:
    if host in _PLACEHOLDER_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _PLACEHOLDER_HOST_SUFFIXES)


def _find_placeholder_host_issues(cfg: Config, sources: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for field_name, env_var in _HOST_VALIDATION_FIELDS:
        raw_value = getattr(cfg, field_name, None)
        if raw_value in (None, ""):
            continue

        value = str(raw_value)
        host = _extract_hostname(value)
        if not host:
            continue
        if not _is_placeholder_host(host):
            continue

        issues.append(
            {
                "field": field_name,
                "env_var": env_var,
                "value": value,
                "host": host,
                "source": sources.get(field_name, SOURCE_DEFAULT),
            }
        )
    return issues


# Fields whose effective value follows the environment, and which are scoped to
# one account or one cluster path. A variable exported by an older account - or
# by a shell that a terminal multiplexer has been recycling for weeks - overrides
# config files with nothing in the files to show for it, so report the
# provenance instead of leaving the operator to diff their own environment.
_ENV_OVERRIDE_WATCHLIST: tuple[tuple[str, str], ...] = (
    ("job_workspace_id", "INSPIRE_WORKSPACE_ID"),
    ("notebook_workspace_id", "INSPIRE_NOTEBOOK_WORKSPACE_ID"),
    ("default_workspace_id", "INSPIRE_DEFAULT_WORKSPACE_ID"),
    ("setup_script", "INSPIRE_SETUP_SCRIPT"),
    # What we download, install, run and trust. An exported path outranks every config file,
    # so without these the files read clean while the environment decides which rtunnel the
    # notebook runs (rtunnel_bin), where a missing rtunnel is fetched from
    # (rtunnel_download_url), which package source installs the in-notebook SSH server
    # (*_deb_dir, apt_mirror_url), and which host keys we believe (host_key_dir). That is a
    # supply-chain surface, not just a convenience override.
    ("rtunnel_bin", "INSPIRE_RTUNNEL_BIN"),
    ("rtunnel_download_url", "INSPIRE_RTUNNEL_DOWNLOAD_URL"),
    ("sshd_deb_dir", "INSPIRE_SSHD_DEB_DIR"),
    ("dropbear_deb_dir", "INSPIRE_DROPBEAR_DEB_DIR"),
    ("apt_mirror_url", "INSPIRE_APT_MIRROR_URL"),
    ("host_key_dir", "INSPIRE_SSH_HOST_KEY_DIR"),
    ("username", "INSPIRE_USERNAME"),
    ("account_alias", "INSPIRE_ACCOUNT"),
)

_SECRET_MASK = "***"


def _describe_env_override_value(field_name: str, value: str) -> str:
    """Mask secret-typed options and keep long cluster paths printable."""
    option = next((opt for opt in CONFIG_OPTIONS if opt.field_name == field_name), None)
    if option is not None and option.secret:
        return _SECRET_MASK
    if len(value) > 48:
        return value[:45] + "..."
    return value


def _find_env_overrides(cfg: Config, sources: dict[str, str]) -> list[dict[str, str]]:
    """List watched fields whose winning value came from the environment."""
    overrides: list[dict[str, str]] = []
    for field_name, env_var in _ENV_OVERRIDE_WATCHLIST:
        if sources.get(field_name) != SOURCE_ENV:
            continue
        raw_value = getattr(cfg, field_name, None)
        if raw_value in (None, ""):
            continue
        overrides.append(
            {
                "field": field_name,
                "env_var": env_var,
                "value": _describe_env_override_value(field_name, str(raw_value)),
            }
        )
    return overrides


def _format_env_override_message(overrides: list[dict[str, str]]) -> str:
    lines = ["Configuration values are being overridden by environment variables:"]
    for item in overrides:
        lines.append(f"  - {item['field']} = {item['value']}  (from ${item['env_var']})")
    lines.append(
        "Environment beats config files. Clear them with: unset "
        + " ".join(item["env_var"] for item in overrides)
    )
    return "\n".join(lines)


def _format_placeholder_issue_message(issues: list[dict[str, str]]) -> str:
    lines = ["Placeholder host values detected in configuration:"]
    for issue in issues:
        lines.append(
            f"  - {issue['env_var']} ({issue['field']}): "
            f"{issue['value']} [source: {issue['source']}]"
        )
    lines.append("Use real host values in config files or environment variables.")
    lines.append("Path-only defaults such as /auth/token are allowed.")
    return "\n".join(lines)


def _validate_required_credentials(cfg: Config) -> None:
    if not cfg.username:
        raise ConfigError(
            "Missing username configuration.\n"
            "Set INSPIRE_USERNAME env var or add to config.toml:\n"
            "  [auth]\n"
            "  username = 'your_username'"
        )
    if not cfg.password:
        raise ConfigError(
            "Missing password configuration.\n"
            "Set INSPIRE_PASSWORD env var or add an account password in config.toml:\n"
            '  [accounts."your_username"]\n'
            "  password = 'your_password'"
        )


def _validate_required_registry(cfg: Config) -> None:
    if not cfg.docker_registry:
        raise ConfigError(
            "Missing docker registry configuration.\n"
            "Set INSPIRE_DOCKER_REGISTRY env var or add to config.toml:\n"
            "  [api]\n"
            "  docker_registry = 'your-registry.example.com'"
        )


def _validate_project_base_url_shape(project_path: Path | None) -> None:
    if not project_path or not project_path.exists():
        return

    try:
        project_raw = Config._load_toml(project_path)
    except Exception as e:
        raise ConfigError(f"Failed to read project config at {project_path}: {e}") from e

    if "base_url" in project_raw:
        raise ConfigError(
            f"Invalid project config at {project_path}.\n"
            "Found top-level `base_url`; this key must be under [api].\n"
            "Use:\n"
            "  [api]\n"
            "  base_url = 'https://your-inspire-host'"
        )


def _build_base_url_resolution(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
) -> dict[str, object]:
    env_base_url = os.environ.get("INSPIRE_BASE_URL")
    return {
        "value": cfg.base_url,
        "source": sources.get("base_url", SOURCE_DEFAULT),
        "prefer_source": getattr(cfg, "prefer_source", "env"),
        "precedence": _describe_precedence(getattr(cfg, "prefer_source", "env")),
        "env_present": bool(env_base_url),
        "global_config_path": str(global_path) if global_path else None,
        "project_config_path": str(project_path) if project_path else None,
    }


def _describe_password_shape(password: str | None) -> str | None:
    value = password or ""
    if not value:
        return None

    if not any(not ch.isalnum() for ch in value):
        return "Configured password shape check: missing a special character."
    return None


def _build_auth_failure_hint(auth_error: str | None, password: str | None) -> str | None:
    if not auth_error:
        return None

    normalized = auth_error.lower()
    if "invalid_grant" not in normalized and "invalid user credentials" not in normalized:
        return None

    password_shape = _describe_password_shape(password)
    hint = (
        "OpenAPI password-token login rejected these credentials. If this account can "
        "log in through the web/CAS flow, reset the password to a platform-compliant "
        "complex value that includes special characters, then retry."
    )
    if password_shape:
        hint = f"{hint} {password_shape}"
    return (
        f"{hint} If it still fails, ask an administrator to verify that the account is "
        "allowed to use /auth/token."
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _build_web_session_failure_hint(error: str | None) -> str | None:
    if not error:
        return None

    normalized = error.lower()
    if "captcha" in normalized or "lock" in normalized:
        return (
            "CAS web-session login was blocked by the platform (captcha or account "
            "lock). Log in once through the web UI, then retry; use "
            "'inspire init --discover' to rebuild the cached session."
        )
    return (
        "The CAS web session is the platform's current auth surface (every CLI "
        "command uses the /api/v2 Action APIs, #82). Check [auth] username/password for "
        "the selected account, then retry. A stale cache can be cleared by deleting "
        "~/.cache/inspire-cli/web_session-<alias>.json, or rebuilt with "
        "'inspire init --discover'."
    )


def _probe_web_session_auth(config: Config) -> dict[str, Any]:
    """Validate the auth surface the CLI actually uses.

    The browser API rides the CAS web session, which replaced ``/auth/token`` as
    the supported login path. Verify one authorized ``user?Action=GetUserDetail``
    call rather than trusting a cached session file.
    """
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web.session import get_web_session

    try:
        session = get_web_session(account=config.account_alias)
    except Exception as exc:  # noqa: BLE001
        return {"probed": True, "ok": False, "error": str(exc), "identity": None}

    try:
        detail = browser_api_module.get_current_user(session)
    except Exception as exc:  # noqa: BLE001
        return {"probed": True, "ok": False, "error": str(exc), "identity": None}

    identity = None
    if isinstance(detail, dict):
        for key in ("username", "name", "id"):
            value = str(detail.get(key) or "").strip()
            if value:
                identity = value
                break
    return {"probed": True, "ok": True, "error": None, "identity": identity}


def _probe_legacy_openapi_auth(config: Config) -> dict[str, Any]:
    """Probe the legacy ``/auth/token`` + ``/openapi/v1/*`` client.

    No CLI command uses this path anymore, so it is only reachable behind
    ``--require-openapi`` for accounts that still want to gate on it.
    """
    try:
        AuthManager.get_api(config)
    except Exception as exc:  # noqa: BLE001
        return {"probed": True, "ok": False, "error": str(exc), "identity": None}
    return {"probed": True, "ok": True, "error": None, "identity": None}


def _run_check_impl(ctx: Context, *, require_openapi: bool = False) -> None:
    """Run config validation logic without going through Click wrappers."""
    effective_json = resolve_json_output(ctx, False)

    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
        )
        global_path, project_path = Config.get_config_paths()
        _validate_project_base_url_shape(project_path)

        placeholder_issues = _find_placeholder_host_issues(cfg, sources)
        if placeholder_issues:
            raise ConfigError(_format_placeholder_issue_message(placeholder_issues))

        env_overrides = _find_env_overrides(cfg, sources)
        ignored_keys = [
            {
                "key": item.dotted,
                "file": item.source,
                "suggestion": item.suggestion,
            }
            for item in audit_config_files([global_path, project_path])
        ]

        _validate_required_credentials(cfg)
        _validate_required_registry(cfg)

        auth_ok = True
        auth_error = None

        web_auth = _probe_web_session_auth(cfg)
        if require_openapi:
            legacy_auth = _probe_legacy_openapi_auth(cfg)
        else:
            legacy_auth = {
                "probed": False,
                "ok": None,
                "error": None,
                "identity": None,
                "reason": "no CLI command uses /auth/token; pass --require-openapi to gate on it",
            }

        blocking: tuple[str, str | None] | None = None
        if not web_auth["ok"]:
            blocking = ("web_session", web_auth["error"])
        elif require_openapi and legacy_auth["ok"] is False:
            blocking = ("legacy_openapi", legacy_auth["error"])

        auth_ok = blocking is None
        auth_error = blocking[1] if blocking else None

        base_url_resolution = _build_base_url_resolution(cfg, sources, global_path, project_path)
        default_base_url_hint = None
        if base_url_resolution["source"] == SOURCE_DEFAULT:
            default_base_url_hint = (
                "Base URL is using default fallback. Set [api] base_url in "
                "./.inspire/config.toml or export INSPIRE_BASE_URL."
            )
        auth_hint = None
        if blocking:
            if blocking[0] == "legacy_openapi":
                auth_hint = _build_auth_failure_hint(auth_error, cfg.password)
            else:
                auth_hint = _build_web_session_failure_hint(auth_error)

        result = {
            "username": cfg.username,
            "base_url": cfg.base_url,
            "target_dir": cfg.target_dir,
            "timeout": cfg.timeout,
            "max_retries": cfg.max_retries,
            "retry_delay": cfg.retry_delay,
            "auth_ok": auth_ok,
            "auth_method": "web_session",
            "auth": {"web_session": web_auth, "legacy_openapi": legacy_auth},
            "base_url_resolution": base_url_resolution,
            "validation": {
                "placeholder_host_issues": placeholder_issues,
                "env_overrides": env_overrides,
                "ignored_keys": ignored_keys,
                "base_url_default_hint": default_base_url_hint,
            },
        }
        if auth_error:
            result["auth_error"] = auth_error
        if auth_hint:
            result["auth_hint"] = auth_hint

        if effective_json:
            if auth_ok:
                emit_success(ctx, payload=result)
            else:
                label = (
                    "legacy /auth/token"
                    if blocking and blocking[0] == "legacy_openapi"
                    else "platform web session"
                )
                emit_error(
                    ctx,
                    error_type="AuthenticationError",
                    message=f"Authentication failed ({label})",
                    exit_code=EXIT_AUTH_ERROR,
                    hint=auth_hint,
                    data=result,
                )
        else:
            if auth_ok:
                emit_success(ctx, payload=result, text="Configuration looks good")
            else:
                label = (
                    "legacy /auth/token"
                    if blocking and blocking[0] == "legacy_openapi"
                    else "platform web session"
                )
                emit_error(
                    ctx,
                    error_type="AuthenticationError",
                    message=f"Authentication failed ({label})",
                    exit_code=EXIT_AUTH_ERROR,
                    hint=auth_hint,
                )

            emit_info(ctx, f"\nUsername:     {cfg.username}")
            emit_info(ctx, f"Base URL:     {cfg.base_url}")
            emit_info(
                ctx,
                "  Auth (web session /api/v1): "
                + ("OK" if web_auth["ok"] else "FAILED")
                + (f" ({web_auth['identity']})" if web_auth.get("identity") else ""),
            )
            if legacy_auth["probed"]:
                emit_info(
                    ctx,
                    "  Auth (legacy /auth/token):  "
                    + ("OK" if legacy_auth["ok"] else "FAILED")
                    + (f" ({legacy_auth['error']})" if not legacy_auth["ok"] else ""),
                )
            else:
                emit_info(
                    ctx,
                    "  Auth (legacy /auth/token):  not probed "
                    "(no CLI command uses it; --require-openapi to gate on it)",
                )
            emit_info(ctx, f"Target dir:   {cfg.target_dir or '(not set)'}")
            emit_info(ctx, f"Timeout:      {cfg.timeout}s")
            emit_info(ctx, f"Max retries:  {cfg.max_retries}")
            emit_info(ctx, f"Retry delay:  {cfg.retry_delay}s")
            emit_info(ctx, "\nBase URL resolution:")
            emit_info(ctx, f"  Value:                {base_url_resolution['value']}")
            emit_info(ctx, f"  Source:               {base_url_resolution['source']}")
            emit_info(ctx, f"  Precedence:           {base_url_resolution['precedence']}")
            emit_info(
                ctx,
                f"  INSPIRE_BASE_URL set: {'yes' if base_url_resolution['env_present'] else 'no'}",
            )
            emit_info(
                ctx,
                f"  Global config:        {base_url_resolution['global_config_path'] or '(not found)'}",
            )
            emit_info(
                ctx,
                f"  Project config:       {base_url_resolution['project_config_path'] or '(not found)'}",
            )

            if default_base_url_hint:
                emit_warning(ctx, f"  Note: {default_base_url_hint}")

            if env_overrides:
                emit_warning(ctx, _format_env_override_message(env_overrides))

            if ignored_keys:
                # A key the loader does not know is dropped silently, which is indistinguishable from
                # never having configured the thing (issue #10). Say it, and where.
                emit_warning(ctx, f"{len(ignored_keys)} config key(s) are not recognised:")
                for item in ignored_keys:
                    suffix = f" - did you mean {item['suggestion']}?" if item["suggestion"] else ""
                    emit_warning(ctx, f"  {item['key']} in {item['file']}{suffix}")

            if auth_error:
                emit_info(ctx, f"\nDetails: {auth_error}")

        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("check")
@click.option(
    "--require-openapi",
    is_flag=True,
    help="Also gate on the legacy /auth/token password-token login.",
)
@pass_context
def check_config(ctx: Context, require_openapi: bool) -> None:
    """Check environment configuration and platform authentication.

    Verifies configuration (from files and environment) and authenticates
    through the CAS web session used by every CLI command. The legacy
    ``/auth/token`` password grant is only probed with ``--require-openapi``.
    """
    _run_check_impl(ctx, require_openapi=require_openapi)
