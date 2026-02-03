"""Display helpers for resource configuration selection."""

from __future__ import annotations

from inspire.api.openapi_models import ComputeGroup, ResourceSpec


def display_available_resources(
    *,
    resource_specs: list[ResourceSpec],
    compute_groups: list[ComputeGroup],
) -> None:
    """Print all available resource configurations."""
    print("\n📊 Available Resource Configurations:")
    print("=" * 60)

    print("\n🖥️  GPU Spec Configurations:")
    for spec in resource_specs:
        print(f"  • {spec.description}")
        print(f"    Spec ID: {spec.spec_id}")

    print("\n🏢 Compute Groups:")
    for group in compute_groups:
        print(f"  • {group.name} ({group.location})")
        print(f"    Compute Group ID: {group.compute_group_id}")

    print("\n💡 Usage Examples:")
    print("  • --resource 'H200'     -> 1x H200 GPU")
    print("  • --resource '4xH200'   -> 4x H200 GPU")
    print("  • --resource '8 H200'   -> 8x H200 GPU")
    print("  • --resource 'H100'     -> 1x H100 GPU")
    print("=" * 60)


__all__ = ["display_available_resources"]
