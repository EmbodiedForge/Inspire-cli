"""Resource parsing and matching for the Inspire OpenAPI client."""

import re
from typing import List, Optional, Tuple

from inspire.api.openapi_models import ComputeGroup, GPUType, ResourceSpec
from inspire.compute_groups import load_compute_groups_from_config


class ResourceManager:
    """Resource manager - handles resource spec and compute group matching."""

    def __init__(self, compute_groups_raw: Optional[list[dict]] = None):
        # Define available resource specs
        self.resource_specs = [
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

        # Define available compute groups from config
        compute_groups_tuples = load_compute_groups_from_config(compute_groups_raw or [])
        self.compute_groups = [
            ComputeGroup(
                name=group.name,
                compute_group_id=group.compute_group_id,
                gpu_type=GPUType(group.gpu_type),
                location=group.location,
            )
            for group in compute_groups_tuples
        ]

    def parse_resource_request(self, resource_str: str) -> Tuple[GPUType, int]:
        """
        Parse natural language resource request.

        Args:
            resource_str: Resource description string, e.g., "H200", "4xH200", "8 H100"

        Returns:
            (GPU type, GPU count) tuple

        Raises:
            ValueError: When resource request cannot be parsed
        """
        if not resource_str:
            raise ValueError("Resource description cannot be empty")

        # Clean up and convert to uppercase
        resource_str = resource_str.upper().strip()

        # Match patterns: number + x/X + GPU type, or number + space + GPU type, or just GPU type
        patterns = [
            r"^(\d+)[xX]?(H100|H200)$",  # "4xH200", "4H200", "4 H200"
            r"^(H100|H200)[xX]?(\d+)?$",  # "H200", "H200x4", "H200 4"
            r"^(\d+)\s+(H100|H200)$",  # "4 H200"
        ]

        gpu_count = 1  # Default count
        gpu_type_str = None

        for pattern in patterns:
            match = re.match(pattern, resource_str.replace(" ", ""))
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    # 可能是 (数字, GPU类型) 或 (GPU类型, 数字)
                    if groups[0].isdigit():
                        gpu_count = int(groups[0])
                        gpu_type_str = groups[1]
                    elif groups[1] and groups[1].isdigit():
                        gpu_type_str = groups[0]
                        gpu_count = int(groups[1])
                    else:
                        gpu_type_str = groups[0] if not groups[0].isdigit() else groups[1]
                break

        # If no number+GPU pattern matched, try to match GPU type directly
        if not gpu_type_str:
            if "H200" in resource_str:
                gpu_type_str = "H200"
            elif "H100" in resource_str:
                gpu_type_str = "H100"

        if not gpu_type_str:
            raise ValueError(f"Unrecognized GPU type: {resource_str}")

        try:
            gpu_type = GPUType(gpu_type_str)
        except ValueError as e:
            raise ValueError(
                f"Unsupported GPU type: {gpu_type_str}, supported types: H100, H200"
            ) from e

        if gpu_count <= 0:
            raise ValueError(f"GPU count must be positive: {gpu_count}")

        return gpu_type, gpu_count

    def find_matching_specs(self, gpu_type: GPUType, gpu_count: int) -> List[ResourceSpec]:
        """
        Find matching resource specs.

        Args:
            gpu_type: GPU type
            gpu_count: Required GPU count

        Returns:
            List of matching resource specs
        """
        matching_specs = []

        for spec in self.resource_specs:
            # For H100, since spec_id is the same, H200 specs can be used
            if spec.gpu_type == gpu_type or (
                gpu_type == GPUType.H100 and spec.gpu_type == GPUType.H200
            ):
                if spec.gpu_count >= gpu_count:
                    matching_specs.append(spec)

        # Sort by GPU count, prefer configurations closest to requirements
        matching_specs.sort(key=lambda x: x.gpu_count)
        return matching_specs

    def find_compute_groups(self, gpu_type: GPUType) -> List[ComputeGroup]:
        """
        Find matching compute groups.

        Args:
            gpu_type: GPU type

        Returns:
            List of matching compute groups
        """
        return [group for group in self.compute_groups if group.gpu_type == gpu_type]

    def get_recommended_config(
        self, resource_str: str, prefer_location: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Get recommended configuration.

        Args:
            resource_str: Resource description string
            prefer_location: Preferred datacenter location

        Returns:
            (spec_id, compute_group_id) tuple

        Raises:
            ValueError: When no matching configuration is found
        """
        gpu_type, gpu_count = self.parse_resource_request(resource_str)

        # Find matching specs
        matching_specs = self.find_matching_specs(gpu_type, gpu_count)
        if not matching_specs:
            available_configs = [
                f"{spec.gpu_count}x{spec.gpu_type.value}" for spec in self.resource_specs
            ]
            raise ValueError(
                f"No configuration found matching {gpu_count}x{gpu_type.value}. "
                f"Available configurations: {', '.join(available_configs)}"
            )

        # Select the most suitable spec (smallest that meets requirements)
        selected_spec = matching_specs[0]

        # Find matching compute groups
        matching_groups = self.find_compute_groups(gpu_type)
        if not matching_groups:
            raise ValueError(f"No compute group found supporting {gpu_type.value}")

        # Select compute group (consider location preference)
        selected_group = matching_groups[0]  # Default to first one

        if prefer_location:
            matched = False

            # Step 1: Try substring match
            for group in matching_groups:
                if prefer_location.lower() in group.location.lower():
                    selected_group = group
                    matched = True
                    break

            # Step 2: Try number-based semantic match
            if not matched:
                numbers = re.findall(r"\d+", prefer_location)
                if numbers:
                    for num in numbers:
                        for group in matching_groups:
                            if num in group.location:
                                selected_group = group
                                matched = True
                                break
                        if matched:
                            break

            # Step 3: Error if nothing matched
            if not matched:
                available_locations = [g.location for g in matching_groups]
                raise ValueError(
                    f"Location '{prefer_location}' not found for {gpu_type.value}. "
                    f"Available locations: {', '.join(available_locations)}"
                )

        return selected_spec.spec_id, selected_group.compute_group_id

    def display_available_resources(self) -> None:
        """Display all available resource configurations."""
        print("\n📊 Available Resource Configurations:")
        print("=" * 60)

        print("\n🖥️  GPU Spec Configurations:")
        for spec in self.resource_specs:
            print(f"  • {spec.description}")
            print(f"    Spec ID: {spec.spec_id}")

        print("\n🏢 Compute Groups:")
        for group in self.compute_groups:
            print(f"  • {group.name} ({group.location})")
            print(f"    Compute Group ID: {group.compute_group_id}")

        print("\n💡 Usage Examples:")
        print("  • --resource 'H200'     -> 1x H200 GPU")
        print("  • --resource '4xH200'   -> 4x H200 GPU")
        print("  • --resource '8 H200'   -> 8x H200 GPU")
        print("  • --resource 'H100'     -> 1x H100 GPU")
        print("=" * 60)
