"""Authenticated platform API and console-page escape hatches.

Raw requests keep the caller's path and JSON body. Browser evaluation goes further: the CLI supplies
an authenticated page, and the caller owns the JavaScript, requests, and result. Neither mode needs
a domain wrapper for a newly added or changed console API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
import requests

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.launch_preview import redact_structure
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.platform.web.browser_api import agent_page
from inspire.platform.web.browser_api.raw_api import (
    HTTP_METHODS,
    normalize_method,
    normalize_path,
    raw_request,
)
from inspire.platform.web.session import SessionAuthenticationError, SessionExpiredError
from inspire.platform.web.session.secrets import known_secret_values

_METHOD_AND_PATH_HINT = (
    f"METHOD is one of {', '.join(HTTP_METHODS)}; "
    "PATH is a full /api/... path or a path under the configured browser-API prefix"
)


def _read_argument(ctx: Context, name: str, raw: str) -> str:
    """Read an inline argument, an @file, or @- from stdin."""
    if not raw.startswith("@"):
        return raw
    source = raw[1:]
    try:
        if source == "-":
            return sys.stdin.read()
        return Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _handle_error(
            ctx,
            "ValidationError",
            f"Cannot read --{name} from {source!r}: {exc}",
            EXIT_VALIDATION_ERROR,
            hint=f"Pass inline content, --{name} @readable-file.json, or --{name} @- for stdin.",
        )
        raise  # pragma: no cover


def _parse_json(ctx: Context, name: str, raw: str) -> Any:
    content = _read_argument(ctx, name, raw)
    try:
        return json.loads(content)
    except ValueError as exc:
        _handle_error(
            ctx,
            "ValidationError",
            f"--{name} is not valid JSON: {exc}",
            EXIT_VALIDATION_ERROR,
            hint=f"Pass a JSON object or array inline, or use --{name} @file.json / @-.",
        )
        raise  # pragma: no cover


def _parse_json_object(ctx: Context, name: str, raw: str) -> dict[str, Any]:
    value = _parse_json(ctx, name, raw)
    if not isinstance(value, dict):
        _handle_error(
            ctx,
            "ValidationError",
            f"--{name} must be a JSON object, not {type(value).__name__}",
            EXIT_VALIDATION_ERROR,
            hint=f"Pass a JSON object, for example --{name} '{{\"page_num\": 1}}'.",
        )
    return value


def _parse_json_body(ctx: Context, raw: str) -> dict[str, Any] | list[Any]:
    value = _parse_json(ctx, "data", raw)
    if not isinstance(value, (dict, list)):
        _handle_error(
            ctx,
            "ValidationError",
            f"--data must be a JSON object or array, not {type(value).__name__}",
            EXIT_VALIDATION_ERROR,
            hint="For arbitrary request bodies and headers, use `inspire api eval` with fetch.",
        )
    return value


def _emit_payload(
    ctx: Context,
    payload: Any,
    session: Any,
    *,
    extra_secrets: tuple[str, ...] = (),
) -> None:
    secrets = extra_secrets + known_secret_values(session)
    safe_payload = redact_structure(payload, secrets)
    if resolve_json_output(ctx, False):
        click.echo(json_formatter.format_json(safe_payload))
        return
    click.echo(json.dumps(safe_payload, indent=2, ensure_ascii=False))


def _redacted_error(session: Any, exc: Exception, *, extra_secrets: tuple[str, ...] = ()) -> str:
    secrets = extra_secrets + known_secret_values(session)
    return str(redact_structure(str(exc), secrets))


@click.command("api")
@click.argument("method")
@click.argument("path")
@click.option("--params", "params_raw", default=None, help="Query parameters as a JSON object")
@click.option(
    "--data",
    "data_raw",
    default=None,
    help="JSON object/array request body; @FILE or @- reads the JSON from a file/stdin",
)
@click.option("--url", "console_url", default=None, help="Initial same-origin page for eval")
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="HTTP or asynchronous browser evaluation budget in seconds",
)
@pass_context
def api(
    ctx: Context,
    method: str,
    path: str,
    params_raw: str | None,
    data_raw: str | None,
    console_url: str | None,
    timeout: int,
) -> None:
    """Use an authenticated raw API request or evaluate JavaScript in the console.

    METHOD is GET, POST, PUT, DELETE, or eval. For the first four, PATH is a full "/api/..." path
    sent without route rewriting, or a shorthand under the configured browser-API prefix. The same
    web session, auth refresh, and bounded 429 retry policy as wrapped commands is used.

    eval treats PATH as JavaScript, @FILE, or @- for stdin. It runs in a temporary Playwright page
    carrying the CLI login state. JavaScript can use fetch, read the DOM, and compose requests
    freely; neither mode adds a read-only guard or business validation. --url selects the initial
    same-origin console page. The browser closes after each invocation.

    \b
    Examples:
        inspire api POST /api/v2/train?Action=ListJobs --data '{"page_num": 1, "page_size": 10, "workspace_id": "ws-..."}'
        inspire api eval '(async () => ({url: location.href}))()'
        inspire api eval --url https://console.example/jobs @inspect-console.js
        inspire --json api eval --url 'https://qz.sii.edu.cn/jobs/spacesOverview?spaceId=ws-...' @inspect.js
        inspire --json api eval @- < inspect-console.js

    Some training Actions, including ListJobs, are served only from the training UI. Pass
    --url https://qz.sii.edu.cn/jobs/spacesOverview?spaceId=ws-... so fetch starts there; the chat
    SPA at / can 404 the same Action.

    Raw requests and eval can create, modify, or delete resources. Neither mode is a dry run, and
    raw DELETE requests and eval writes have no automatic --yes confirmation gate. Use writes only
    with explicit intent. After a timeout or disconnect, inspect platform state before repeating a
    request: it may already have been sent. Required Action fields belong in --data; ListJobs returns
    .data.Result.jobs/.data.Result.total. Raw rows are the complete platform record, not the wrapped
    command's narrow projection, and redaction covers only credential-shaped keys and known session
    values -- never print a whole payload. Raw response bodies and JavaScript return values are
    printed with that limited redaction. Under --json the value is in .data.
    """
    if method.strip().lower() == "eval":
        if params_raw is not None or data_raw is not None:
            _handle_error(
                ctx,
                "ValidationError",
                "--params and --data belong to raw METHOD PATH requests, not eval",
                EXIT_VALIDATION_ERROR,
                hint="Pass request options directly to fetch in the evaluated JavaScript.",
            )
            return
        source = _read_argument(ctx, "eval", path)
        if not source.strip():
            _handle_error(
                ctx,
                "ValidationError",
                "JavaScript source must not be empty",
                EXIT_VALIDATION_ERROR,
                hint="Pass an expression, @script.js, or @- for stdin.",
            )
            return
        session = require_web_session(ctx, hint="inspire api eval requires web authentication.")
        initial_secrets = known_secret_values(session)
        try:
            payload = agent_page.evaluate_page(
                source,
                console_url=console_url,
                timeout_seconds=timeout,
                session=session,
            )
        except agent_page.InvalidConsoleUrl as exc:
            _handle_error(
                ctx,
                "ValidationError",
                str(exc),
                EXIT_VALIDATION_ERROR,
                hint="Pass an absolute http(s) URL on the configured console origin.",
            )
            return
        except agent_page.PageEvaluationTimeout as exc:
            _handle_error(
                ctx,
                "Timeout",
                _redacted_error(session, exc, extra_secrets=initial_secrets),
                EXIT_TIMEOUT,
                hint=(
                    "The browser may already have sent requests. Inspect platform state before "
                    "repeating the evaluation."
                ),
            )
            return
        except (SessionAuthenticationError, SessionExpiredError) as exc:
            _handle_error(
                ctx,
                "AuthenticationError",
                _redacted_error(session, exc, extra_secrets=initial_secrets),
                EXIT_AUTH_ERROR,
                hint="Run `inspire init` to re-authenticate the web session.",
            )
            return
        except Exception as exc:  # noqa: BLE001
            _handle_error(
                ctx,
                "APIError",
                _redacted_error(session, exc, extra_secrets=initial_secrets),
                EXIT_API_ERROR,
            )
            return
        _emit_payload(ctx, payload, session, extra_secrets=initial_secrets)
        return

    if console_url is not None:
        _handle_error(
            ctx,
            "ValidationError",
            "--url requires eval",
            EXIT_VALIDATION_ERROR,
            hint="Use `inspire api eval [--url URL] <JS>` for browser evaluation.",
        )
        return

    params = _parse_json_object(ctx, "params", params_raw) if params_raw is not None else None
    data = _parse_json_body(ctx, data_raw) if data_raw is not None else None

    try:
        resolved_method = normalize_method(method)
        resolved_path = normalize_path(path)
    except ValueError as exc:
        _handle_error(
            ctx,
            "ValidationError",
            str(exc),
            EXIT_VALIDATION_ERROR,
            hint=_METHOD_AND_PATH_HINT,
        )
        return

    session = require_web_session(ctx, hint="inspire api requires web authentication.")

    try:
        payload = raw_request(
            resolved_method,
            resolved_path,
            params=params,
            body=data,
            session=session,
            timeout=timeout,
        )
    except (SessionAuthenticationError, SessionExpiredError) as exc:
        _handle_error(
            ctx,
            "AuthenticationError",
            _redacted_error(session, exc),
            EXIT_AUTH_ERROR,
            hint="Run `inspire init` to re-authenticate the web session.",
        )
        return
    except requests.Timeout as exc:
        _handle_error(
            ctx,
            "Timeout",
            _redacted_error(session, exc),
            EXIT_TIMEOUT,
            hint=(
                "The request may already have been sent. Inspect platform state before "
                "repeating the request."
            ),
        )
        return
    except requests.ConnectionError as exc:
        _handle_error(
            ctx,
            "APIError",
            _redacted_error(session, exc),
            EXIT_API_ERROR,
            hint=(
                "The connection dropped; the request may already have been sent. Inspect platform "
                "state before repeating it."
            ),
        )
        return
    except (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    ) as exc:
        _handle_error(
            ctx,
            "APIError",
            _redacted_error(session, exc),
            EXIT_API_ERROR,
            hint=(
                "The response was cut off after the request was sent; it may already have taken "
                "effect. Inspect platform state before repeating it."
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", _redacted_error(session, exc), EXIT_API_ERROR)
        return

    _emit_payload(ctx, payload, session)


__all__ = ["api"]
