from inspire.platform.openapi.models import GPUType
from inspire.platform.openapi.resources import ResourceManager


def test_resource_manager_ignores_compute_groups_without_supported_gpu_type() -> None:
    manager = ResourceManager(
        [
            {"name": "CPU", "id": "lcg-cpu", "gpu_type": ""},
            {"name": "4090", "id": "lcg-4090", "gpu_type": "4090"},
            {"name": "H100", "id": "lcg-h100", "gpu_type": "h100"},
        ]
    )

    assert len(manager.compute_groups) == 1
    assert manager.compute_groups[0].compute_group_id == "lcg-h100"
    assert manager.compute_groups[0].gpu_type == GPUType.H100


def test_resource_manager_ignores_compute_group_without_id() -> None:
    manager = ResourceManager([{"name": "H200 missing id", "gpu_type": "H200"}])

    assert manager.compute_groups == []


def test_resource_manager_accepts_discovered_gpu_type_labels() -> None:
    manager = ResourceManager(
        [
            {"name": "H200-1", "id": "lcg-h200-1", "gpu_type": "NVIDIA H200 (141GB)"},
            {"name": "H100-1", "id": "lcg-h100-1", "gpu_type": "NVIDIA H100 (80GB)"},
        ]
    )

    ids_to_types = {group.compute_group_id: group.gpu_type for group in manager.compute_groups}

    assert ids_to_types["lcg-h200-1"] == GPUType.H200
    assert ids_to_types["lcg-h100-1"] == GPUType.H100
