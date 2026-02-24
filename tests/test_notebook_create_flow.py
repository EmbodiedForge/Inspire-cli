"""Tests for notebook create flow resource spec resolution."""

from __future__ import annotations

from types import SimpleNamespace

from inspire.cli.commands.notebook import notebook_create_flow as flow_module
from inspire.cli.commands.notebook.notebook_create_flow import resolve_notebook_resource_spec_price
from inspire.cli.context import Context


def test_cpu_resource_spec_keeps_requested_cpu_from_quota() -> None:
    resource_prices = [
        {
            "gpu_count": 0,
            "cpu_count": 55,
            "memory_size_gib": 220,
            "quota_id": "quota-55",
            "cpu_info": {"cpu_type": "cpu-type-large"},
            "gpu_info": {},
        },
        {
            "gpu_count": 0,
            "cpu_count": 4,
            "memory_size_gib": 16,
            "quota_id": "quota-4",
            "cpu_info": {"cpu_type": "cpu-type-small"},
            "gpu_info": {},
        },
    ]

    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=resource_prices,
        gpu_count=0,
        selected_gpu_type="",
        gpu_pattern="CPU",
        logic_compute_group_id="lcg-cpu",
        quota_id="quota-4",
        cpu_count=4,
        memory_size=16,
        requested_cpu_count=4,
    )

    assert resolved_quota == "quota-4"
    assert resolved_cpu == 4
    assert resolved_mem == 16
    assert spec["gpu_count"] == 0
    assert spec["cpu_count"] == 4
    assert spec["memory_size_gib"] == 16
    assert spec["quota_id"] == "quota-4"
    assert spec["cpu_type"] == "cpu-type-small"


def test_cpu_resource_spec_exists_without_resource_prices() -> None:
    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=[],
        gpu_count=0,
        selected_gpu_type="",
        gpu_pattern="CPU",
        logic_compute_group_id="lcg-cpu",
        quota_id="quota-4",
        cpu_count=4,
        memory_size=16,
        requested_cpu_count=4,
    )

    assert resolved_quota == "quota-4"
    assert resolved_cpu == 4
    assert resolved_mem == 16
    assert spec["gpu_count"] == 0
    assert spec["cpu_count"] == 4
    assert spec["memory_size_gib"] == 16
    assert spec["quota_id"] == "quota-4"


def test_gpu_resource_spec_prefers_matching_resource_prices() -> None:
    resource_prices = [
        {
            "gpu_count": 1,
            "cpu_count": 20,
            "memory_size_gib": 80,
            "quota_id": "quota-h100",
            "cpu_info": {"cpu_type": "cpu-type-gpu"},
            "gpu_info": {"gpu_type": "NVIDIA_H100"},
        },
        {
            "gpu_count": 8,
            "cpu_count": 64,
            "memory_size_gib": 512,
            "quota_id": "quota-other",
            "cpu_info": {"cpu_type": "cpu-type-other"},
            "gpu_info": {"gpu_type": "NVIDIA_H100"},
        },
    ]

    spec, resolved_quota, resolved_cpu, resolved_mem = resolve_notebook_resource_spec_price(
        resource_prices=resource_prices,
        gpu_count=1,
        selected_gpu_type="NVIDIA_H100",
        gpu_pattern="H100",
        logic_compute_group_id="lcg-h100",
        quota_id="",
        cpu_count=10,
        memory_size=40,
        requested_cpu_count=None,
    )

    assert resolved_quota == "quota-h100"
    assert resolved_cpu == 20
    assert resolved_mem == 80
    assert spec["gpu_count"] == 1
    assert spec["gpu_type"] == "NVIDIA_H100"
    assert spec["cpu_count"] == 20
    assert spec["memory_size_gib"] == 80
    assert spec["quota_id"] == "quota-h100"


def _configure_create_happy_path(
    monkeypatch, *, wait_result: bool
) -> tuple[Context, dict[str, object]]:  # noqa: ANN001
    ctx = Context()
    calls: dict[str, object] = {}

    config = SimpleNamespace(
        notebook_resource="1xH100",
        project_order=None,
        job_project_id="project-1111",
        notebook_image=None,
        job_image="img-default",
        shm_size=32,
        job_priority=9,
    )

    selected_project = SimpleNamespace(
        project_id="project-1111",
        name="Project One",
        priority_name="6",
    )
    selected_image = SimpleNamespace(
        image_id="img-1111",
        url="docker://image",
        name="Image One",
    )

    monkeypatch.setattr(flow_module, "resolve_json_output", lambda _ctx, _json: False)
    monkeypatch.setattr(flow_module, "require_web_session", lambda _ctx, hint: object())
    monkeypatch.setattr(flow_module, "load_config", lambda _ctx: config)
    monkeypatch.setattr(flow_module, "parse_resource_string", lambda _resource: (1, "H100", None))
    monkeypatch.setattr(
        flow_module, "resolve_notebook_workspace_id", lambda *_args, **_kwargs: "ws-1111"
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_compute_group",
        lambda *_args, **_kwargs: ("lcg-1111", "NVIDIA_H100", "H100", "1xH100"),
    )
    monkeypatch.setattr(flow_module, "_fetch_notebook_schedule", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_quota",
        lambda *_args, **_kwargs: ("quota-1111", 20, 80, "NVIDIA_H100", "1xH100"),
    )
    monkeypatch.setattr(flow_module, "_fetch_resource_prices", lambda **_kwargs: [])
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_resource_spec_price",
        lambda **_kwargs: ({"gpu_count": 1}, "quota-1111", 20, 80),
    )
    monkeypatch.setattr(
        flow_module,
        "_fetch_workspace_projects",
        lambda *_args, **_kwargs: [selected_project],
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_project",
        lambda *_args, **_kwargs: selected_project,
    )
    monkeypatch.setattr(
        flow_module,
        "_fetch_notebook_images",
        lambda *_args, **_kwargs: [selected_image],
    )
    monkeypatch.setattr(
        flow_module,
        "resolve_notebook_image",
        lambda *_args, **_kwargs: selected_image,
    )

    def fake_create_notebook_and_report(*_args, **kwargs):  # noqa: ANN001
        calls["task_priority"] = kwargs["task_priority"]
        calls["resource_spec_price"] = kwargs["resource_spec_price"]
        return "nb-1111"

    monkeypatch.setattr(flow_module, "create_notebook_and_report", fake_create_notebook_and_report)

    def fake_wait_for_running(*_args, **_kwargs):  # noqa: ANN001
        calls["wait_called"] = True
        return wait_result

    monkeypatch.setattr(flow_module, "maybe_wait_for_running", fake_wait_for_running)

    def fake_keepalive(*_args, **kwargs):  # noqa: ANN001
        calls["keepalive_called"] = True
        calls["keepalive_gpu_count"] = kwargs["gpu_count"]

    monkeypatch.setattr(flow_module, "maybe_start_keepalive", fake_keepalive)
    return ctx, calls


def test_run_notebook_create_orchestrates_happy_path(monkeypatch) -> None:  # noqa: ANN001
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=True)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        resource=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        auto=False,
        wait=True,
        keepalive=True,
        json_output=False,
        priority=None,
        project_explicit=False,
    )

    # Priority should be capped to the selected project's max priority.
    assert calls["task_priority"] == 6
    assert calls["resource_spec_price"] == {"gpu_count": 1}
    assert calls["wait_called"] is True
    assert calls["keepalive_called"] is True
    assert calls["keepalive_gpu_count"] == 1


def test_run_notebook_create_skips_keepalive_when_wait_fails(monkeypatch) -> None:  # noqa: ANN001
    ctx, calls = _configure_create_happy_path(monkeypatch, wait_result=False)

    flow_module.run_notebook_create(
        ctx,
        name=None,
        workspace=None,
        workspace_id=None,
        resource=None,
        project=None,
        image=None,
        shm_size=None,
        auto_stop=True,
        auto=False,
        wait=True,
        keepalive=True,
        json_output=False,
        priority=None,
        project_explicit=False,
    )

    assert calls["wait_called"] is True
    assert "keepalive_called" not in calls
