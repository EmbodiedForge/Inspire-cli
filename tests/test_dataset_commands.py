"""`inspire dataset` - the two-platform contract a caller cannot see for itself.

A dataset name means two different things on two different hosts: the data square says a code
exists, the compute platform says it will mount in *this* workspace. Everything the CLI asserts
about "is this dataset usable" therefore has to be split the same way, and the pieces that do the
splitting are pinned here:

* the pure helpers - ``CODE@VERSION`` parsing, the backend's ``code: N, message: ...`` string, the
  mount-path convention, camelCase -> snake_case - because a wrong one of those is a wrong answer
  that still exits 0;
* the exit-code mapping, because the contract's difference between 11 (no session), 12 (bad input)
  and 13 (platform failure) is the only thing an agent can branch on;
* that a *rejected* dataset version is a result, not an error: the platform validates each pair
  independently, so a failure of one item must not read as a failed command.

The platform layer is stubbed throughout - these are CLI-shape tests, not transport tests.
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.context import (
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.main import main as cli_main
from inspire.platform.web.browser_api import datasets as compute_datasets
from inspire.platform.web.datasquare import client as datasquare_client
from inspire.platform.web.datasquare import datasets as datasquare_datasets
from inspire.platform.web.datasquare import session as datasquare_session
from inspire.platform.web.session.models import WebSession

dataset_cmd_module = importlib.import_module("inspire.cli.commands.dataset")
dataset_utils = importlib.import_module("inspire.cli.utils.datasets")


class FakeSession:
    workspace_id = "ws-default"
    storage_state: dict[str, Any] = {"cookies": []}


DATASQUARE_ROW: dict[str, Any] = {
    "datasetId": 417,
    "datasetCode": "PointWorld-BEHAVIOR",
    "projectName": "PointWorld",
    "director": "Zhang San",
    "maintenance": "Li Si",
    "super": "S2",
    "superSub": "robot manipulation",
    "ownerName": "sii-data",
    "description": "A large manipulation corpus " * 4,
    "starCount": 12,
    "viewCount": 340,
    "createdAt": "2026-08-01T10:00:00Z",
    "updatedAt": "2026-09-02T10:00:00Z",
    "state": "published",
    "hasPermission": True,
    "tags": [
        {"tagId": 9, "tagName": "manipulation", "tagPath": "/task/manipulation", "categoryId": 3}
    ],
}


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PointWorld@v1", ("PointWorld", "v1")),
        ("  PointWorld@v1  ", ("PointWorld", "v1")),
        # The version never carries `@`, so the LAST separator is the meaningful one.
        ("obs@team@v3", ("obs@team", "v3")),
        ("PointWorld@v1@extra", ("PointWorld@v1", "extra")),
    ],
)
def test_parse_dataset_target_accepts_only_a_complete_reference(
    raw: str, expected: tuple[str, str]
) -> None:
    assert dataset_utils.parse_dataset_target(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "no-separator",
        "@v1",
        "PointWorld@",
        "PointWorld@   ",
        "",
    ],
)
def test_parse_dataset_target_rejects_an_incomplete_reference_with_the_syntax(raw: str) -> None:
    """Every consumer of a bad reference reports 12, and the message has to teach the syntax."""
    with pytest.raises(ValueError) as excinfo:
        dataset_utils.parse_dataset_target(raw)

    assert "DATASET_CODE@VERSION" in str(excinfo.value)


@pytest.mark.parametrize(
    ("message", "code", "cleaned"),
    [
        ("code: 2001, message: 版本不存在", 2001, "版本不存在"),
        ("code: 2000, message: dataset not found", 2000, "dataset not found"),
        ("code: 2005, message: 无访问权限", 2005, "无访问权限"),
        ("版本不存在", None, "版本不存在"),
        ("", None, ""),
        (None, None, ""),
    ],
)
def test_backend_error_message_keeps_code_and_message_separate(
    message: Any, code: int | None, cleaned: str
) -> None:
    """The code is the machine-readable half; an agent must not have to re-parse the string."""
    assert dataset_utils.parse_backend_error(message) == (code, cleaned)


def test_mount_path_follows_the_platform_convention() -> None:
    assert (
        dataset_utils.dataset_mount_path("PointWorld-BEHAVIOR", "v2")
        == "/inspire/dataset/PointWorld-BEHAVIOR/v2"
    )


def test_catalogue_row_is_mapped_to_snake_case() -> None:
    mapped = dataset_cmd_module._map_dataset_fields(DATASQUARE_ROW)

    assert mapped["dataset_id"] == 417
    assert mapped["dataset_code"] == "PointWorld-BEHAVIOR"
    assert mapped["maintainer"] == "Li Si"
    assert mapped["level"] == "S2"
    assert mapped["level_sub"] == "robot manipulation"
    assert mapped["has_permission"] is True
    assert mapped["tags"] == [
        {
            "tag_id": 9,
            "tag_name": "manipulation",
            "tag_path": "/task/manipulation",
            "category_id": 3,
        }
    ]
    assert "maintenance" not in mapped
    assert "super" not in mapped


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"qb":"active"}', {"qb": "active"}),
        ("not-json{", "not-json{"),
        ("", ""),
        (None, None),
        ({"qb": "active"}, {"qb": "active"}),
    ],
)
def test_replica_status_is_parsed_when_it_is_json_and_kept_when_it_is_not(
    raw: Any, expected: Any
) -> None:
    """The portal double-encodes this field; an unexpected shape must survive, not vanish."""
    assert dataset_cmd_module._parse_replica_status(raw) == expected


def test_detail_versions_carry_storage_fields_and_mount_path() -> None:
    detail = dataset_cmd_module._map_dataset_detail(
        {
            **DATASQUARE_ROW,
            "versions": [
                {
                    "versionId": 88,
                    "versionCode": "v2",
                    "filePath": "downloader-1/huggingface/nvidia/PointWorld-BEHAVIOR",
                    "filesCount": 12,
                    "filesSize": 4096,
                    "versionState": "ready",
                    "replicaStrategy": "qb",
                    "replicaStatus": '{"qb":"active"}',
                    "updateTime": "2026-09-02T10:00:00Z",
                    "description": "second",
                }
            ],
        }
    )

    assert detail["versions"] == [
        {
            "version_id": 88,
            "version_code": "v2",
            "file_path": "downloader-1/huggingface/nvidia/PointWorld-BEHAVIOR",
            "files_count": 12,
            "files_size": 4096,
            "version_state": "ready",
            "replica_strategy": "qb",
            "replica_status": {"qb": "active"},
            "update_time": "2026-09-02T10:00:00Z",
            "description": "second",
            "mount_path": "/inspire/dataset/PointWorld-BEHAVIOR/v2",
        }
    ]


# --------------------------------------------------------------------------- CLI: sessions and exits


def test_dataset_group_is_registered() -> None:
    result = CliRunner().invoke(cli_main, ["dataset", "--help"])

    assert result.exit_code == 0
    for name in ("list", "versions", "get", "check", "mount-path"):
        assert name in result.output


def test_missing_datasquare_session_exits_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda account=None: None)

    result = CliRunner().invoke(cli_main, ["--account", "main", "--json", "dataset", "list"])

    assert result.exit_code == EXIT_AUTH_ERROR
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "AuthenticationError"
    assert "login_web_session.mjs --portal aip --account main" in error["hint"]
    assert "--account-key" in error["hint"]
    assert "--password-key" in error["hint"]


def test_expired_datasquare_session_exits_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _expired(*args: Any, **kwargs: Any) -> Any:
        raise datasquare_client.DatasquareSessionExpired("401 from aip")

    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(datasquare_datasets, "list_datasets", _expired)

    result = CliRunner().invoke(cli_main, ["--account", "main", "--json", "dataset", "list"])

    assert result.exit_code == EXIT_AUTH_ERROR
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "AuthenticationError"
    assert "login_web_session.mjs --portal aip --account main" in error["hint"]


def test_unauthorized_body_code_is_authentication_not_platform_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aip answers 200 with `code: 7` when the cookie died - still exit 11, not 13."""

    def _unauthorized(*args: Any, **kwargs: Any) -> Any:
        raise datasquare_client.DatasquareAPIError(7, "登录失效", path="/api/datasets/x")

    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(datasquare_datasets, "list_datasets", _unauthorized)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "list"])

    assert result.exit_code == EXIT_AUTH_ERROR
    assert json.loads(result.stderr)["error"]["code"] == EXIT_AUTH_ERROR


def test_api_error_exits_platform_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise datasquare_client.DatasquareAPIError(500, "boom", path="/api/datasets/x")

    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(datasquare_datasets, "list_datasets", _boom)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "list"])

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.stderr)["error"]["type"] == "APIError"


def test_list_rejects_out_of_range_paging_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets,
        "list_datasets",
        lambda *a, **k: calls.append(a) or {},
    )

    for argv in (["--page", "0"], ["--page-size", "0"], ["--page-size", "-1"]):
        result = CliRunner().invoke(cli_main, ["dataset", "list", *argv])
        assert result.exit_code == EXIT_VALIDATION_ERROR, argv
    assert calls == []


def test_check_without_at_separator_exits_validation_without_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_session(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("bad input must be caught before any session is opened")

    monkeypatch.setattr(dataset_cmd_module, "require_web_session", _no_session)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "missing-at"])

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "DATASET_CODE@VERSION" in json.loads(result.stderr)["error"]["hint"]


# --------------------------------------------------------------------------- CLI: list / versions / get


def test_list_maps_rows_and_reports_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _list(session: Any, keyword: str | None = None, page: int = 1, page_size: int = 20):
        calls.append({"keyword": keyword, "page": page, "page_size": page_size})
        return {"list": [DATASQUARE_ROW], "total": 1, "page": page, "pageSize": page_size}

    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(datasquare_datasets, "list_datasets", _list)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "list", "point", "--page", "2"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert calls == [{"keyword": "point", "page": 2, "page_size": 20}]
    assert data["total"] == 1
    assert data["page"] == 2
    assert data["query"] == "point"
    assert data["items"][0]["dataset_code"] == "PointWorld-BEHAVIOR"
    assert data["items"][0]["maintainer"] == "Li Si"


def test_list_text_mode_keeps_one_line_per_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets,
        "list_datasets",
        lambda *a, **k: {"list": [DATASQUARE_ROW], "total": 1},
    )

    result = CliRunner().invoke(cli_main, ["dataset", "list"])

    assert result.exit_code == 0
    line = result.stdout.strip()
    assert line.startswith(
        "PointWorld-BEHAVIOR  id=417  level=S2  state=published  permission=yes  stars=12  "
    )
    assert len(line.splitlines()) == 1
    assert "…" in line  # the long description was truncated to fit the row


def test_versions_prefers_the_exact_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets,
        "get_datasets_with_versions",
        lambda session, keyword: [
            {"datasetCode": "Point", "versions": [{"versionId": 1, "versionCode": "v0"}]},
            {
                "datasetCode": "PointWorld",
                "versions": [
                    {"versionId": 2, "versionCode": "v1"},
                    {"versionId": 3, "versionCode": "v2"},
                ],
            },
        ],
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "versions", "PointWorld"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["total"] == 2
    assert "matches" not in data
    assert data["items"][1] == {
        "version_id": 3,
        "version_code": "v2",
        "mount_path": "/inspire/dataset/PointWorld/v2",
    }


def test_versions_without_an_exact_code_returns_every_match_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets,
        "get_datasets_with_versions",
        lambda session, keyword: [
            {"datasetCode": "PointA", "versions": [{"versionId": 1, "versionCode": "v1"}]},
            {"datasetCode": "PointB", "versions": [{"versionId": 2, "versionCode": "v2"}]},
        ],
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "versions", "Point"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["matches"] == ["PointA", "PointB"]
    assert [item["mount_path"] for item in data["items"]] == [
        "/inspire/dataset/PointA/v1",
        "/inspire/dataset/PointB/v2",
    ]


def test_get_by_code_resolves_the_numeric_id_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def _find(session: Any, dataset_id: int):
        calls.append(dataset_id)
        return {**DATASQUARE_ROW, "versions": [], "licenseName": "Apache-2.0"}

    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets,
        "list_datasets",
        lambda session, keyword=None, page=1, page_size=20: {
            "list": [
                {"datasetId": 417, "datasetCode": "PointWorld-BEHAVIOR"},
                {"datasetId": 999, "datasetCode": "PointWorld-BEHAVIOR-EXTRA"},
            ]
        },
    )
    monkeypatch.setattr(datasquare_datasets, "find_dataset", _find)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "get", "PointWorld-BEHAVIOR"])

    assert result.exit_code == 0, result.output
    assert calls == [417]
    data = json.loads(result.stdout)["data"]
    assert data["dataset_id"] == 417
    assert data["license_name"] == "Apache-2.0"


def test_get_by_id_skips_the_catalogue_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def _find(session: Any, dataset_id: int):
        calls.append(dataset_id)
        assert dataset_id == 417
        return {**DATASQUARE_ROW, "versions": []}

    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(datasquare_datasets, "find_dataset", _find)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "get", "417"])

    assert result.exit_code == 0, result.output
    assert calls == [417]


def test_get_unknown_code_exits_validation_with_a_search_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets,
        "list_datasets",
        lambda session, keyword=None, page=1, page_size=20: {
            "list": [{"datasetId": 1, "datasetCode": "SomethingElse"}]
        },
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "get", "PointWorld"])

    assert result.exit_code == EXIT_VALIDATION_ERROR
    error = json.loads(result.stderr)["error"]
    assert "inspire dataset list" in error["hint"]


# --------------------------------------------------------------------------- CLI: check


def _stub_check(monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]) -> list[Any]:
    calls: list[Any] = []

    def _validate(pairs: list[dict[str, Any]], workspace_id: str, session: Any = None):
        calls.append({"pairs": pairs, "workspace_id": workspace_id})
        return results

    monkeypatch.setattr(
        dataset_cmd_module, "require_web_session", lambda ctx, *, hint: FakeSession()
    )
    monkeypatch.setattr(compute_datasets, "validate_datasets", _validate)
    return calls


def test_check_keeps_exit_zero_when_a_version_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_check(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld",
                "version_id": "v1",
                "success": True,
                "error_message": "",
                "path": "downloader-1/huggingface/nvidia/PointWorld",
            },
            {
                "dataset_id": "PointWorld",
                "version_id": "v9",
                "success": False,
                "error_message": "code: 2001, message: 版本不存在",
                "path": "",
            },
        ],
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "dataset", "check", "PointWorld@v1", "PointWorld@v9"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "pairs": [
                {"dataset_id": "PointWorld", "version_id": "v1"},
                {"dataset_id": "PointWorld", "version_id": "v9"},
            ],
            "workspace_id": "ws-default",
        }
    ]
    data = json.loads(result.stdout)["data"]
    assert data["all_ok"] is False
    assert data["workspace_id"] == "ws-default"
    ok, failed = data["items"]
    # Every verdict is stamped with the moment it was checked (#81): the platform is
    # eventually consistent about replicas, so the timestamp travels with the item.
    assert ok["checked_at"] == failed["checked_at"] == data["checked_at"]
    del ok["checked_at"]
    del failed["checked_at"]
    assert ok == {
        "dataset": "PointWorld",
        "version": "v1",
        "ok": True,
        "error_code": None,
        "error_message": "",
        "backend_path": "downloader-1/huggingface/nvidia/PointWorld",
        "mount_path": "/inspire/dataset/PointWorld/v1",
    }
    assert failed["ok"] is False
    assert failed["error_code"] == 2001
    assert failed["error_message"] == "版本不存在"
    assert failed["backend_path"] is None


def test_check_reports_a_missing_verdict_as_a_failed_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_check(monkeypatch, [])

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "PointWorld@v1"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["all_ok"] is False
    assert data["items"][0]["ok"] is False
    assert data["items"][0]["error_code"] is None


def test_check_reads_the_dataset_case_insensitively_but_the_version_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_check(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld",
                "version_id": "V1",
                "success": True,
                "error_message": "",
                "path": "p",
            }
        ],
    )

    lower_dataset = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "pointworld@V1"])
    lower_version = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "pointworld@v1"])

    assert json.loads(lower_dataset.stdout)["data"]["all_ok"] is True
    assert json.loads(lower_version.stdout)["data"]["all_ok"] is False


def test_parse_dataset_target_rejects_the_squares_numeric_ids() -> None:
    """``1761@2409`` is what the square's UI shows, and the mount API refuses it (#81)."""
    for raw in ("1761@2409", "pointworld@2409"):
        with pytest.raises(ValueError, match="datasetId/versionId are not the mount reference"):
            dataset_utils.parse_dataset_target(raw)


def _stub_check_with_catalogue(
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict[str, Any]],
    catalogue_row: dict[str, Any] | None,
) -> None:
    _stub_check(monkeypatch, results)
    monkeypatch.setattr(
        dataset_cmd_module,
        "get_datasquare_session",
        lambda account=None: FakeSession() if catalogue_row is not None else None,
    )
    monkeypatch.setattr(
        datasquare_datasets,
        "list_datasets",
        lambda *a, **k: {"list": [catalogue_row] if catalogue_row else [], "total": 1},
    )


@pytest.mark.parametrize(
    ("row_overrides", "expected_diagnosis"),
    [
        (
            {
                "hasPermission": True,
                "versions": [{"versionCode": "v9", "versionState": "downloading"}],
            },
            "registered but downloading; the platform has no replica yet - re-check later (ask Li Si)",
        ),
        (
            {"hasPermission": False, "versions": [{"versionCode": "v9", "versionState": "active"}]},
            "no access grant; request it from the maintainer (ask Li Si)",
        ),
        (
            {"hasPermission": True, "versions": []},
            "dataset registered, no version created (state=published)",
        ),
    ],
)
def test_check_joins_the_catalogue_to_explain_a_failed_verdict(
    monkeypatch: pytest.MonkeyPatch,
    row_overrides: dict[str, Any],
    expected_diagnosis: str,
) -> None:
    """One 2001 string covers a typo, an unreplicated version, and a versionless dataset (#81).

    The catalogue record the square already holds separates them; the raw platform string stays
    in the payload verbatim and the diagnosis is added next to it.
    """
    row = dict(DATASQUARE_ROW)
    row.update(row_overrides)
    _stub_check_with_catalogue(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld-BEHAVIOR",
                "version_id": "v9",
                "success": False,
                "error_message": "code: 2001, message: 版本不存在",
                "path": "",
            }
        ],
        row,
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "PointWorld-BEHAVIOR@v9"])

    assert result.exit_code == 0, result.output
    item = json.loads(result.stdout)["data"]["items"][0]
    assert item["error_message"] == "版本不存在"
    assert item["diagnosis"] == expected_diagnosis
    assert item["catalogue_state"] == "published"
    assert item["maintainer"] == "Li Si"


def test_check_leaves_a_genuinely_absent_version_to_the_raw_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the version truly is not in the catalogue, 版本不存在 is finally the true answer."""
    row = dict(DATASQUARE_ROW)
    row.update(
        {"hasPermission": True, "versions": [{"versionCode": "v1", "versionState": "active"}]}
    )
    _stub_check_with_catalogue(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld-BEHAVIOR",
                "version_id": "nope-v9",
                "success": False,
                "error_message": "code: 2001, message: 版本不存在",
                "path": "",
            }
        ],
        row,
    )

    result = CliRunner().invoke(
        cli_main, ["--json", "dataset", "check", "PointWorld-BEHAVIOR@nope-v9"]
    )

    assert result.exit_code == 0, result.output
    item = json.loads(result.stdout)["data"]["items"][0]
    assert "diagnosis" not in item
    assert item["error_message"] == "版本不存在"


def test_check_skips_the_catalogue_join_without_a_square_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict needs only the compute session; a missing square session must not fail check."""
    _stub_check_with_catalogue(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld-BEHAVIOR",
                "version_id": "v9",
                "success": False,
                "error_message": "code: 2001, message: 版本不存在",
                "path": "",
            }
        ],
        None,
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "PointWorld-BEHAVIOR@v9"])

    assert result.exit_code == 0, result.output
    item = json.loads(result.stdout)["data"]["items"][0]
    assert "diagnosis" not in item
    assert "maintainer" not in item


def test_check_platform_error_exits_platform_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(pairs: list[dict[str, Any]], workspace_id: str, session: Any = None):
        raise ValueError("ValidateDataset failed: 500")

    monkeypatch.setattr(
        dataset_cmd_module, "require_web_session", lambda ctx, *, hint: FakeSession()
    )
    monkeypatch.setattr(compute_datasets, "validate_datasets", _boom)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "PointWorld@v1"])

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.stderr)["error"]["type"] == "APIError"


def test_check_needs_a_workspace_from_somewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoWorkspace(FakeSession):
        workspace_id = None

    monkeypatch.setattr(
        dataset_cmd_module, "require_web_session", lambda ctx, *, hint: _NoWorkspace()
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "PointWorld@v1"])

    assert result.exit_code == EXIT_VALIDATION_ERROR


def test_check_text_mode_prints_one_verdict_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_check(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld",
                "version_id": "v1",
                "success": True,
                "error_message": "",
                "path": "downloader-1/p",
            },
            {
                "dataset_id": "PointWorld",
                "version_id": "v9",
                "success": False,
                "error_message": "code: 2000, message: 数据集不存在",
                "path": "",
            },
        ],
    )

    result = CliRunner().invoke(cli_main, ["dataset", "check", "PointWorld@v1", "PointWorld@v9"])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "PointWorld@v1  ok  path=downloader-1/p",
        "PointWorld@v9  FAILED  数据集不存在",
    ]


# --------------------------------------------------------------------------- CLI: mount-path


def test_mount_path_needs_no_session_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_session(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("mount-path is a local naming convention")

    monkeypatch.setattr(dataset_cmd_module, "require_web_session", _no_session)
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", _no_session)

    result = CliRunner().invoke(
        cli_main, ["--json", "dataset", "mount-path", "PointWorld@v1", "Other@v2"]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert [item["mount_path"] for item in data["items"]] == [
        "/inspire/dataset/PointWorld/v1",
        "/inspire/dataset/Other/v2",
    ]


def test_mount_path_text_mode_is_bare_paths_for_scripting() -> None:
    result = CliRunner().invoke(cli_main, ["dataset", "mount-path", "PointWorld@v1"])

    assert result.exit_code == 0
    assert result.stdout == "/inspire/dataset/PointWorld/v1\n"


def test_mount_path_rejects_an_incomplete_reference() -> None:
    result = CliRunner().invoke(cli_main, ["--json", "dataset", "mount-path", "PointWorld"])

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert json.loads(result.stderr)["error"]["code"] == EXIT_VALIDATION_ERROR


# --------------------------------------------------------------------------- platform layer


def test_validate_datasets_sends_the_rpc_body_to_the_compute_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _request(session: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, **kwargs})
        return {
            "ResponseMetadata": {"Action": "ValidateDataset", "Service": "dataset"},
            "Result": {"datasets_result": [{"dataset_id": "A", "success": True}]},
        }

    monkeypatch.setattr(compute_datasets, "_request_json", _request)

    result = compute_datasets.validate_datasets(
        [{"dataset_id": "A", "version_id": "v1"}],
        "ws-1",
        session=FakeSession(),
    )

    assert result == [{"dataset_id": "A", "success": True}]
    assert calls[0]["method"] == "POST"
    # A /api/ path is used verbatim: no /api/v1 prefix is glued in front of a v2 endpoint.
    assert calls[0]["path"] == "/api/v2/dataset?Action=ValidateDataset"
    assert calls[0]["referer"].endswith("/jobs/distributedTraining")
    assert calls[0]["body"] == {
        "datasets": [{"dataset_id": "A", "version_id": "v1"}],
        "workspace_id": "ws-1",
    }


@pytest.mark.parametrize(
    ("payload", "expected_in_message"),
    [
        (
            {"ResponseMetadata": {"Error": {"Code": "InvalidParameter", "Message": "bad ws"}}},
            "InvalidParameter",
        ),
        ({"ResponseMetadata": {"Action": "ValidateDataset"}}, "datasets_result"),
        (
            {"ResponseMetadata": {"Action": "ValidateDataset"}, "Result": {"datasets_result": "x"}},
            "datasets_result",
        ),
    ],
)
def test_validate_datasets_refuses_an_envelope_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], expected_in_message: str
) -> None:
    """A platform-level rejection must not come back looking like an empty success.

    The platform's own code has to survive into the message: it is the only thing the caller of
    exit 13 gets, and "Result is not an object" would send them looking in the wrong place.
    """
    monkeypatch.setattr(compute_datasets, "_request_json", lambda *a, **k: payload)

    with pytest.raises(ValueError) as raised:
        compute_datasets.validate_datasets(
            [{"dataset_id": "A", "version_id": "v1"}], "ws-1", session=FakeSession()
        )

    assert expected_in_message in str(raised.value)


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _FakeHTTP:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response

    def close(self) -> None:
        pass


def _patch_http(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> _FakeHTTP:
    http = _FakeHTTP(response)
    monkeypatch.setattr(datasquare_client, "build_requests_session", lambda *_a, **_k: http)
    return http


def test_datasquare_request_unwraps_the_envelope_and_keeps_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _patch_http(
        monkeypatch, _FakeResponse(200, {"code": 0, "data": {"list": []}, "msg": "ok"})
    )

    data = datasquare_client.datasquare_request_json(
        FakeSession(), "GET", "/api/datasets/getDatasetsList", params={"page": 2}
    )

    assert data == {"list": []}
    call = http.calls[0]
    assert call["url"] == "https://aip.sii.edu.cn/api/datasets/getDatasetsList"
    assert call["params"] == {"page": 2}
    # A GET carries no body, whatever the caller passed.
    assert call["json"] is None


def test_datasquare_request_401_is_session_expired_not_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_http(monkeypatch, _FakeResponse(401, {"code": 7, "msg": "登录失效"}))

    with pytest.raises(datasquare_client.DatasquareSessionExpired):
        datasquare_client.datasquare_request_json(FakeSession(), "GET", "/api/datasets/x")


def test_datasquare_request_non_zero_code_carries_code_and_msg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_http(monkeypatch, _FakeResponse(200, {"code": 500, "msg": "内部错误"}))

    with pytest.raises(datasquare_client.DatasquareAPIError) as raised:
        datasquare_client.datasquare_request_json(FakeSession(), "GET", "/api/datasets/x")

    assert raised.value.code == 500
    assert raised.value.msg == "内部错误"


def test_datasquare_http_error_is_a_plain_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_http(monkeypatch, _FakeResponse(502, "bad gateway"))

    with pytest.raises(ValueError):
        datasquare_client.datasquare_request_json(FakeSession(), "GET", "/api/datasets/x")


def test_datasquare_session_cache_is_account_scoped_and_separate_from_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspire.platform.web.session.models as session_models

    monkeypatch.setattr(session_models, "SESSION_CACHE_DIR", tmp_path)

    default_file = datasquare_session.get_datasquare_session_cache_file()
    scoped_file = datasquare_session.get_datasquare_session_cache_file("project user")

    assert default_file.name == "datasquare_session.json"
    assert scoped_file.name == "datasquare_session-project_user.json"
    assert not str(default_file).endswith("web_session.json")

    session = WebSession(
        storage_state={"cookies": [{"name": "datasets-session", "value": "jwt"}]},
        created_at=time.time(),
        account_alias="project user",
    )
    written = datasquare_session.save_datasquare_session(session)

    assert written == scoped_file
    restored = datasquare_session.get_datasquare_session(account="project user")
    assert restored is not None
    assert restored.storage_state["cookies"][0]["name"] == "datasets-session"
    # A different account must not pick up this cookie.
    assert datasquare_session.get_datasquare_session(account="someone-else") is None


def test_expired_datasquare_session_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspire.platform.web.session.models as session_models

    monkeypatch.setattr(session_models, "SESSION_CACHE_DIR", tmp_path)
    session = WebSession(
        storage_state={"cookies": [{"name": "datasets-session", "value": "jwt"}]},
        created_at=time.time() - datasquare_session.DATASQUARE_SESSION_TTL - 1,
    )
    datasquare_session.save_datasquare_session(session)

    assert datasquare_session.get_datasquare_session() is None
    assert datasquare_session.get_datasquare_session(allow_expired=True) is not None


def test_clearing_the_platform_cache_also_clears_the_data_square(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `clear` must not leave a live second session on disk."""
    import inspire.platform.web.session as web_session_module

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    platform_session = cache_dir / "web_session.json"
    datasquare = cache_dir / "datasquare_session-lab.json"
    unrelated = cache_dir / "project_list.json"
    for path in (platform_session, datasquare, unrelated):
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web_session_module, "SESSION_CACHE_FILE", platform_session)

    web_session_module.clear_session_cache()

    assert not platform_session.exists()
    assert not datasquare.exists()
    assert unrelated.exists()


def test_output_never_carries_a_credential_shaped_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction is the contract's, not this command's decoration: pin it on the real path."""
    row = {
        **DATASQUARE_ROW,
        "description": "see https://aip.sii.edu.cn/x?token=url-secret",
        "ownerName": "bearer Authorization: raw-secret",
    }
    monkeypatch.setattr(dataset_cmd_module, "get_datasquare_session", lambda **kw: FakeSession())
    monkeypatch.setattr(
        datasquare_datasets, "list_datasets", lambda *a, **k: {"list": [row], "total": 1}
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "list"])

    assert result.exit_code == 0
    assert "url-secret" not in result.stdout
    assert "raw-secret" not in result.stdout


def test_check_session_expiry_on_the_platform_is_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform session re-auths once; a report after that is an auth finding, not a 13."""
    from inspire.platform.web.session.models import SessionExpiredError

    def _expired(pairs: list[dict[str, Any]], workspace_id: str, session: Any = None):
        raise SessionExpiredError("Session remained invalid after re-authentication")

    monkeypatch.setattr(
        dataset_cmd_module, "require_web_session", lambda ctx, *, hint: FakeSession()
    )
    monkeypatch.setattr(compute_datasets, "validate_datasets", _expired)

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "check", "PointWorld@v1"])

    assert result.exit_code == EXIT_AUTH_ERROR
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "AuthenticationError"
    assert "inspire init" in error["hint"]


# ----------------------------------------------------------- shared validate-then-create helper


def _stub_validate_datasets(
    monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _validate(pairs: list[dict[str, Any]], workspace_id: str, session: Any = None):
        calls.append({"pairs": pairs, "workspace_id": workspace_id})
        return results

    monkeypatch.setattr(compute_datasets, "validate_datasets", _validate)
    return calls


def test_validate_dataset_targets_fills_the_backend_path_and_keeps_the_request_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the platform resolved is what a create body mounts, in the order the caller listed."""
    calls = _stub_validate_datasets(
        monkeypatch,
        [
            {
                "dataset_id": "OpenX",
                "version_id": "v3",
                "success": True,
                "path": "downloader/openx",
            },
            {
                "dataset_id": "PointWorld",
                "version_id": "v1",
                "success": True,
                "path": "downloader/pointworld",
            },
        ],
    )

    resolved = dataset_utils.validate_dataset_targets(
        [("PointWorld", "v1"), ("OpenX", "v3")], "ws-1", session=FakeSession()
    )

    assert calls == [
        {
            "pairs": [
                {"dataset_id": "PointWorld", "version_id": "v1"},
                {"dataset_id": "OpenX", "version_id": "v3"},
            ],
            "workspace_id": "ws-1",
        }
    ]
    assert resolved == [
        {"dataset_id": "PointWorld", "version_id": "v1", "path": "downloader/pointworld"},
        {"dataset_id": "OpenX", "version_id": "v3", "path": "downloader/openx"},
    ]


def test_validate_dataset_targets_assembles_nothing_and_calls_nothing_without_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dataset requested is not a question worth asking the platform."""
    calls = _stub_validate_datasets(monkeypatch, [])

    assert dataset_utils.validate_dataset_targets([], "ws-1", session=FakeSession()) == []
    assert calls == []


def test_validate_dataset_targets_refuses_the_whole_set_on_one_rejected_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single rejection stops the create, and the platform's own message is the explanation.

    The code and the wording are both kept: the wording says what to fix, and the caller's exit 13
    carries nothing else.
    """
    _stub_validate_datasets(
        monkeypatch,
        [
            {
                "dataset_id": "PointWorld",
                "version_id": "v1",
                "success": True,
                "path": "p",
            },
            {
                "dataset_id": "PointWorld",
                "version_id": "v9",
                "success": False,
                "error_message": "code: 2001, message: 版本不存在",
                "path": "",
            },
        ],
    )

    with pytest.raises(dataset_utils.DatasetValidationError) as raised:
        dataset_utils.validate_dataset_targets(
            [("PointWorld", "v1"), ("PointWorld", "v9")], "ws-1", session=FakeSession()
        )

    assert raised.value.dataset_code == "PointWorld"
    assert raised.value.version_code == "v9"
    assert raised.value.error_code == 2001
    assert raised.value.error_message == "版本不存在"
    assert "PointWorld@v9" in str(raised.value)
    assert "版本不存在" in str(raised.value)


def test_validate_dataset_targets_treats_a_missing_verdict_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform answers every pair; silence about one is not a pass."""
    _stub_validate_datasets(monkeypatch, [])

    with pytest.raises(dataset_utils.DatasetValidationError) as raised:
        dataset_utils.validate_dataset_targets(
            [("PointWorld", "v1")], "ws-1", session=FakeSession()
        )

    assert raised.value.error_code is None
    assert raised.value.error_message == dataset_utils.NO_VERDICT_MESSAGE


def test_validate_dataset_targets_lets_a_transport_failure_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure is not a dataset verdict: the caller maps it, this layer must not swallow it."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("500 from ValidateDataset")

    monkeypatch.setattr(compute_datasets, "validate_datasets", _boom)

    with pytest.raises(RuntimeError, match="500 from ValidateDataset"):
        dataset_utils.validate_dataset_targets([("A", "v1")], "ws-1", session=FakeSession())
