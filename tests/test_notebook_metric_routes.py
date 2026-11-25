"""The two routes `notebook metrics` needed to reach its Action.

`inspire notebook metrics <nb> --interval 60` answered the gateway's ``404 page not found``.
Two routes were involved, and both were read out of the console's own bundles rather than
guessed at:

* ``POST /run_index/list`` was forwarded verbatim to a retired URL.  The console reads the same
  list through its notebook service module - ``assets/interactiveModel.7ubd16HF.js`` offset 1599
  spells ``async function v(o){return e(`${n}?Action=ListRunIndex`,{method:t.POST,
  params:{notebook_id:o}})}`` with ``const n="/api/v2/notebook"`` at offset 55 - and the live
  Action takes the v1 body and answers ``Result: {list: [{index, start_time, end_time}], total}``.
* The metric call went to ``train?Action=GetTaskMetric``, the fallback for an unrecognised task
  type, which answers ``InvalidParameter: 参数错误`` for ``interactive_modeling``.  The same bundle
  builds the notebook's metrics call as ``${n}?Action=GetTaskMetric`` (builder at offset 3193,
  literal at 3389), and the
  notebook detail page calls it with ``taskType:"interactive_modeling"`` and the compute group off
  ``start_config`` (``assets/InteractiveDetail.B9Bbw0_y.js``, offset 47330).

Like ``tests/test_job_surface_routes.py``, these tests sit below the route table and name the
``(method, url)`` that actually leaves the process: tests that patched the caller's own transport
stayed green through every one of these bugs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

import pytest

from inspire.platform.web.browser_api import core as core_module
from inspire.platform.web.browser_api import resource_metrics
from inspire.platform.web.browser_api import v2 as v2_module

_BASE_URL = "https://example.invalid"
_NOTEBOOK_ID = "notebook-1"
_HOUR = 3600


class FakeSession:
    workspace_id = "ws-test"
    storage_state = {"cookies": []}


def _iso(epoch_seconds: int) -> str:
    """A run window as the platform spells it - and as an instant, not as a wall-clock guess.

    The live `ListRunIndex` answers ``2026-11-05 16:18:42`` (local, no offset); the offset is
    written here so the fixture means the same instant on a UTC runner as on this one.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(sep=" ")[:-6] + "+00:00"


def _record_wire_calls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_started_seconds_ago: int = _HOUR,
    gpu_count: int = 0,
) -> dict[str, Any]:
    """Capture the HTTP calls the module layer performs, answering them the way the platform did.

    Returns the clock the fixture is anchored to, so expectations stay exact without depending on
    how long this test takes to run.
    """
    calls: list[dict[str, Any]] = []
    now = int(time.time())
    started = now - run_started_seconds_ago

    def fake_request_json(session, method, url, **kwargs):  # noqa: ANN001
        body = kwargs.get("body")
        calls.append({"method": method, "url": url, "body": body})
        action = url.rsplit("Action=", 1)[-1]

        def envelope(result: dict[str, Any]) -> dict[str, Any]:
            # The fake sits below the route table, so it answers the way the gateway answers: a v2
            # Action envelope, which `call_v2_route` then rewraps as the v1 `{code, data}` the
            # browser_api modules parse.  Answering `{code, data}` here would test the translation
            # layer by feeding it a shape the platform never sends.
            return {"ResponseMetadata": {"Action": action, "Service": "notebook"}, "Result": result}

        if action == "GetNotebook":
            return envelope(
                {
                    "notebook_id": _NOTEBOOK_ID,
                    "name": "pi-scale-big",
                    "status": "RUNNING",
                    "created_at": str(started * 1000),
                    "workspace": {"id": "ws-test"},
                    "start_config": {
                        "logic_compute_group_id": "lcg-from-detail",
                        "gpu_count": gpu_count,
                    },
                }
            )
        if action == "ListRunIndex":
            # A run that has not ended: `end_time` is empty while the notebook is RUNNING.
            return envelope(
                {
                    "list": [{"index": 0, "start_time": _iso(started), "end_time": ""}],
                    "total": 1,
                }
            )
        return envelope(
            {
                "time_seris_metric_groups": [
                    {
                        "group_name": "pi-scale-big--5721aca4673c-iem246jgwh",
                        "metric_type": (body or {}).get("metric_types", [""])[0],
                        "resource_name": "CPU",
                        "time_series": [{"timestamp": str(now), "data": 0.2}],
                    }
                ]
            }
        )

    for module in (core_module, resource_metrics):
        monkeypatch.setattr(module, "_get_base_url", lambda: _BASE_URL)
    monkeypatch.setattr(core_module, "request_json", fake_request_json)
    monkeypatch.setattr(v2_module.web_session_module, "request_json", fake_request_json)
    return {"calls": calls, "now": now, "started": started}


def _actions(calls: list[dict[str, Any]]) -> list[str]:
    return [call["url"].rsplit("Action=", 1)[-1] for call in calls]


def _metric_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [call for call in calls if call["url"].endswith("GetTaskMetric")]


def test_notebook_metrics_never_posts_the_retired_run_index_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _record_wire_calls(monkeypatch)

    result = resource_metrics.get_notebook_resource_metrics(
        _NOTEBOOK_ID,
        metric_types=["cpu_usage_rate"],
        interval_seconds=300,
        session=FakeSession(),
    )

    calls = fixture["calls"]
    retired = f"{_BASE_URL}/api/v2/run_index/list"
    assert [call["url"] for call in calls if call["url"] == retired] == []
    assert _actions(calls) == ["GetNotebook", "ListRunIndex", "GetTaskMetric"]
    assert result["metrics"]["cpu_usage_rate"][0]["time_series"] == [
        {"timestamp": str(fixture["now"]), "data": 0.2}
    ]


def test_run_index_list_reaches_the_notebook_service_with_the_v1_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _record_wire_calls(monkeypatch)

    runs = resource_metrics.list_notebook_run_indexes(_NOTEBOOK_ID, session=FakeSession())

    calls = fixture["calls"]
    assert [(call["method"], call["url"]) for call in calls] == [
        ("POST", f"{_BASE_URL}/api/v2/notebook?Action=ListRunIndex")
    ]
    assert calls[0]["body"] == {"notebook_id": _NOTEBOOK_ID}
    assert [run["index"] for run in runs] == [0]
    assert runs[0]["end_time"] == ""


def test_notebook_metric_reaches_the_notebook_service_and_not_train(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body is the one the notebook detail page sends, on the service it sends it to."""
    fixture = _record_wire_calls(monkeypatch)

    result = resource_metrics.get_notebook_resource_metrics(
        _NOTEBOOK_ID,
        metric_types=["cpu_usage_rate"],
        interval_seconds=300,
        session=FakeSession(),
    )

    call = _metric_calls(fixture["calls"])[0]
    assert len(_metric_calls(fixture["calls"])) == 1
    assert (call["method"], call["url"]) == (
        "POST",
        f"{_BASE_URL}/api/v2/notebook?Action=GetTaskMetric",
    )
    assert call["body"] == {
        "metric_types": ["cpu_usage_rate"],
        "filter": {
            # The compute group the notebook detail carries.  Not one read off a task dimension:
            # `cluster?Action=ListTaskDimension` answers `AccessForbidden` to an ordinary account,
            # and the console's notebook page never calls it.
            "logic_compute_group_id": "lcg-from-detail",
            "task_type": "interactive_modeling",
            "task_id": _NOTEBOOK_ID,
        },
        "time_range": {
            "start_timestamp": fixture["started"],
            "end_timestamp": pytest.approx(fixture["now"], abs=5),
            "interval_second": 300,
        },
    }
    assert result["logic_compute_group_id"] == "lcg-from-detail"
    assert result["task_type"] == "interactive_modeling"
    assert "window_clamped" not in result


def test_cpu_only_notebook_asks_for_cpu_series_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default metric types are GPU series, which a 40 CPU notebook answers empty.

    Measured 2026-11-06: `gpu_usage_rate` returns no groups for a notebook whose
    `start_config.gpu_count` is 0, and the console's panel removes every GPU chart in that case.
    """
    fixture = _record_wire_calls(monkeypatch)

    result = resource_metrics.get_notebook_resource_metrics(
        _NOTEBOOK_ID,
        interval_seconds=300,
        session=FakeSession(),
    )

    assert result["metric_types"] == ["cpu_usage_rate", "memory_usage_rate"]
    assert [
        call["body"]["metric_types"]
        for call in fixture["calls"]
        if call["url"].endswith("GetTaskMetric")
    ] == [["cpu_usage_rate"], ["memory_usage_rate"]]


def test_gpu_notebook_still_defaults_to_gpu_series(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _record_wire_calls(monkeypatch, gpu_count=8)

    result = resource_metrics.get_notebook_resource_metrics(
        _NOTEBOOK_ID,
        interval_seconds=300,
        session=FakeSession(),
    )

    assert result["metric_types"] == ["gpu_usage_rate", "gpu_memory_usage_rate"]
    assert _actions(fixture["calls"])[-2:] == ["GetTaskMetric", "GetTaskMetric"]


def test_a_sixty_second_interval_asks_for_the_newest_five_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported command, `notebook metrics pi-scale-big --interval 60`, on a day-long run.

    The platform refuses 60 s over a day (`InternalError: 当前时间区间下，时间间隔过小`) and the
    console pairs a minute with five hours (`assets/Search.DLufsIGr.js`, `gIe` at offset
    3151881).  So the interval stays, the window moves, and the move is reported.
    """
    fixture = _record_wire_calls(monkeypatch, run_started_seconds_ago=24 * _HOUR)

    result = resource_metrics.get_notebook_resource_metrics(
        _NOTEBOOK_ID,
        interval_seconds=60,
        session=FakeSession(),
    )

    window = _metric_calls(fixture["calls"])[0]["body"]["time_range"]
    assert window["interval_second"] == 60
    assert window["end_timestamp"] - window["start_timestamp"] == 5 * _HOUR
    assert result["window_clamped"] == {
        "interval_seconds": 60,
        "max_span_seconds": 5 * _HOUR,
        "requested_start": fixture["started"],
    }
    assert result["metrics"]["cpu_usage_rate"]


def test_an_interval_the_platform_does_not_offer_is_refused_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything but 1/5/30 minutes or an hour is an `InternalError` from the platform."""
    fixture = _record_wire_calls(monkeypatch)

    with pytest.raises(ValueError, match="must be one of 60, 300, 1800, 3600"):
        resource_metrics.get_notebook_resource_metrics(
            _NOTEBOOK_ID,
            interval_seconds=120,
            session=FakeSession(),
        )

    assert _actions(fixture["calls"]) == ["GetNotebook", "ListRunIndex"]
