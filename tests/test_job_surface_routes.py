"""The job-detail surfaces behind hpc/ray/model commands (#108).

Eight v1 endpoints were still being called after the v1 tree retired, and every one of them
answered the gateway's ``404 page not found``: hpc events/instances/logs, ray events/instances,
serving logs, and - found only after the coverage test learned to read templated paths -
``DELETE /hpc_jobs/{id}`` and ``GET /inference_servings/{id}/versions``.

The Actions below are the ones the console itself calls, read out of its own bundles
(``assets/hpcService.TbdH5hUr.js``, ``assets/constant.Cl3Yx5fQ.js``,
``assets/inferenceService.SJo_CjTf.js``), and every v1 body here was measured live against the
v2 service on 2026-11-05 - the bodies were accepted and only the domain answer differed
("job not found" / ``AccessForbidden`` for jobs this account may not read).  See the
measurement table on #108.

Like ``tests/test_user_api_key_routes.py``, these tests go through the real ``_request_json``
so they name the ``(method, url)`` that actually leaves the process.  A test that patches the
caller's own transport would have stayed green through all eight of these bugs.
"""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import core as core_module
from inspire.platform.web.browser_api import hpc_jobs as hpc_module
from inspire.platform.web.browser_api import inference_servings as servings_module
from inspire.platform.web.browser_api import ray_jobs as ray_module
from inspire.platform.web.browser_api import v2 as v2_module

_BASE_URL = "https://example.invalid"


class FakeSession:
    workspace_id = "ws-test"
    storage_state = {"cookies": []}


def _record_wire_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the HTTP call the module layer performs, one level below the route table."""
    calls: list[dict[str, Any]] = []

    def fake_request_json(session, method, url, **kwargs):  # noqa: ANN001
        calls.append({"method": method, "url": url, "body": kwargs.get("body")})
        return {
            "code": 0,
            "data": {"items": []},
            "message": "",
            "ResponseMetadata": {"Action": "probe", "Service": "job"},
            "Result": {"items": [], "events": [], "logs": [], "total": "0"},
        }

    for module in (core_module, servings_module):
        monkeypatch.setattr(module, "_get_base_url", lambda: _BASE_URL)
    monkeypatch.setattr(core_module, "request_json", fake_request_json)
    monkeypatch.setattr(v2_module.web_session_module, "request_json", fake_request_json)
    return calls


def _only_call(calls: list[dict[str, Any]], method: str, action: str) -> dict[str, Any]:
    assert [(call["method"], call["url"]) for call in calls] == [
        (method, f"{_BASE_URL}/api/v2/{action}")
    ]
    return calls[0]


def test_hpc_job_events_reach_the_hpc_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_wire_calls(monkeypatch)

    hpc_module.list_hpc_job_events("job-1", session=FakeSession())

    call = _only_call(calls, "POST", "hpc?Action=ListJobEvents")
    # The body the console's own job-detail page sends, which is also what the retired v1
    # caller sent - so this route needs no adapter.
    assert call["body"] == {
        "pageNum": -1,
        "pageSize": 200,
        "filter": {"object_ids": ["job-1"], "object_type": "HPC_JOB"},
        "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
    }


def test_hpc_job_instances_reach_the_hpc_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_wire_calls(monkeypatch)

    hpc_module.list_hpc_job_instances("job-1", page_size=10, session=FakeSession())

    call = _only_call(calls, "POST", "hpc?Action=ListJobInstances")
    assert call["body"] == {"jobId": "job-1", "page_num": 1, "page_size": 10}


def test_hpc_job_logs_reach_the_hpc_action_with_the_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_wire_calls(monkeypatch)

    hpc_module.list_hpc_job_logs(
        pod_names=["pod-0"],
        start_timestamp_ms=1_700_000_000_000,
        end_timestamp_ms=1_700_000_060_000,
        page_size=50,
        job_id="job-1",
        session=FakeSession(),
    )

    call = _only_call(calls, "POST", "hpc?Action=GetJobLog")
    assert call["body"] == {
        "job_id": "job-1",
        "page_size": 50,
        "filter": {
            "podNames": ["pod-0"],
            # The proto declares both timestamps as strings: an int is an InvalidParameter.
            "start_timestamp_ms": "1700000000000",
            "end_timestamp_ms": "1700000060000",
        },
    }


def test_delete_hpc_job_puts_the_id_in_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_wire_calls(monkeypatch)

    hpc_module.delete_hpc_job("job-1", session=FakeSession())

    call = _only_call(calls, "POST", "hpc?Action=DeleteJob")
    assert call["body"] == {"job_id": "job-1"}


def test_ray_job_events_reach_the_ray_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_wire_calls(monkeypatch)

    ray_module.list_ray_job_events("ray-1", page_size=-1, session=FakeSession())

    call = _only_call(calls, "POST", "ray?Action=ListJobEvents")
    assert call["body"] == {
        "ray_job_id": "ray-1",
        "page_num": 1,
        "page_size": -1,
        "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
    }


def test_ray_job_instances_reach_the_ray_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_wire_calls(monkeypatch)

    ray_module.list_ray_job_instances("ray-1", page_size=10, session=FakeSession())

    call = _only_call(calls, "POST", "ray?Action=ListJobInstances")
    assert call["body"] == {"ray_job_id": "ray-1", "page_num": 1, "page_size": 10}


def test_inference_serving_versions_reach_the_serving_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_wire_calls(monkeypatch)

    servings_module.get_inference_serving_versions("serving-1", session=FakeSession())

    call = _only_call(calls, "POST", "inference_serving?Action=ListServingVersions")
    assert call["body"] == {"inference_serving_id": "serving-1"}


def test_inference_serving_logs_reach_the_serving_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_wire_calls(monkeypatch)
    monkeypatch.setattr(
        servings_module,
        "list_inference_serving_instances",
        lambda serving_id, page_size=100, session=None: {"items": [{"name": "serve-0"}]},
    )

    servings_module.get_inference_serving_logs("serving-1", page_size=20, session=FakeSession())

    call = _only_call(calls, "POST", "inference_serving?Action=GetServingLog")
    assert call["body"] == {
        "inference_serving_id": "serving-1",
        "page_size": 20,
        "filter": {"podNames": ["serve-0"]},
        "sorter": [
            {"field": "time", "sort": "descend"},
            {"field": "log-id.keyword", "sort": "descend"},
        ],
    }
