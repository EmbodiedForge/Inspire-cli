"""Built-in resource spec catalog for the Inspire OpenAPI client."""

from __future__ import annotations

from inspire.api.openapi_models import GPUType, ResourceSpec


def build_default_resource_specs() -> list[ResourceSpec]:
    return [
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=1,
            cpu_cores=15,
            memory_gb=200,
            gpu_memory_gb=141,
            spec_id="4dd0e854-e2a4-4253-95e6-64c13f0b5117",
            description="1 × NVIDIA H200 (141GB) + 15 CPU cores + 200GB RAM",
        ),
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=4,
            cpu_cores=60,
            memory_gb=800,
            gpu_memory_gb=141,
            spec_id="45ab2351-fc8a-4d50-a30b-b39a5306c906",
            description="4 × NVIDIA H200 (141GB) + 60 CPU cores + 800GB RAM",
        ),
        ResourceSpec(
            gpu_type=GPUType.H200,
            gpu_count=8,
            cpu_cores=120,
            memory_gb=1600,
            gpu_memory_gb=141,
            spec_id="b618f5cb-c119-4422-937e-f39131853076",
            description="8 × NVIDIA H200 (141GB) + 120 CPU cores + 1600GB RAM",
        ),
    ]


__all__ = ["build_default_resource_specs"]
