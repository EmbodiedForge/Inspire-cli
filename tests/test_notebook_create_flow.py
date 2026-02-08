"""Tests for notebook create flow resource spec resolution."""

from __future__ import annotations

from inspire.cli.commands.notebook.notebook_create_flow import resolve_notebook_resource_spec_price


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
