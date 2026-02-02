#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Legacy Inspire API Control script.

This module is kept for backward compatibility.
New code should import from `inspire.api`.
"""

import argparse
import json
import logging
import os

from inspire.api import (
    AuthenticationError,
    DEFAULT_SHM_ENV_VAR,
    InspireAPI,
    InspireAPIError,
    InspireConfig,
    JobCreationError,
    ResourceManager,
    ValidationError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def get_credentials() -> tuple[str, str]:
    """Get credentials from environment variables."""
    username = os.getenv("INSPIRE_USERNAME")
    password = os.getenv("INSPIRE_PASSWORD")

    if not username:
        raise ValidationError(
            "❌ Username not found. Please set INSPIRE_USERNAME environment variable.\n"
            "   Example: export INSPIRE_USERNAME='your_username'"
        )

    if not password:
        raise ValidationError(
            "❌ Password not found. Please set INSPIRE_PASSWORD environment variable.\n"
            "   Example: export INSPIRE_PASSWORD='your_password'"
        )

    return username, password


def main() -> int:
    """Main function - provides command line interface."""
    parser = argparse.ArgumentParser(
        description="🚀 Inspire Platform API Smart Control Tool",
        epilog=(
            "Credentials provided via environment variables: INSPIRE_USERNAME and INSPIRE_PASSWORD\n"
            "Use --show-resources to view all available resource configurations"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global options
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://api.example.com",
        help="API base URL (default: https://api.example.com)",
    )
    parser.add_argument(
        "--show-resources",
        action="store_true",
        help="Show all available resource configurations and exit",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Smart training job creation
    create_parser = subparsers.add_parser(
        "create", help="🎯 Smart distributed training job creation"
    )
    create_parser.add_argument("--name", required=True, type=str, help="Training job name")
    create_parser.add_argument("--start-command", required=True, type=str, help="Start command")
    create_parser.add_argument(
        "--resource",
        required=True,
        type=str,
        help='Resource configuration (e.g., "H200", "4xH200", "8 H200", "H100")',
    )
    create_parser.add_argument(
        "--framework",
        type=str,
        default="pytorch",
        help="Training framework (default: pytorch)",
    )
    create_parser.add_argument(
        "--location",
        type=str,
        help='Preferred datacenter location (e.g., "Room1", "Room2")',
    )
    create_parser.add_argument(
        "--priority", type=int, default=8, help="Task priority 1-10 (default: 8)"
    )
    create_parser.add_argument("--image", type=str, help="Custom image name (optional)")
    create_parser.add_argument(
        "--instances", type=int, default=1, help="Instance count (default: 1)"
    )
    default_shm_size = InspireAPI.DEFAULT_SHM_SIZE
    create_parser.add_argument(
        "--shm-size",
        type=int,
        default=default_shm_size,
        help=(
            f"Shared memory size (Gi) (default: {default_shm_size}, "
            f"overridable via {DEFAULT_SHM_ENV_VAR})"
        ),
    )
    create_parser.add_argument(
        "--max-time-hours",
        type=float,
        default=100.0,
        help="Max running time (hours) (default: 100)",
    )
    create_parser.add_argument("--project-id", type=str, help="Project ID (optional, uses default)")
    create_parser.add_argument(
        "--workspace-id", type=str, help="Workspace ID (optional, uses default)"
    )
    create_parser.add_argument(
        "--auto-fault-tolerance", action="store_true", help="Enable auto fault tolerance"
    )
    create_parser.add_argument(
        "--enable-notification", action="store_true", help="Enable notifications"
    )
    create_parser.add_argument(
        "--enable-troubleshoot", action="store_true", help="Enable troubleshooting"
    )

    # Query job details
    detail_parser = subparsers.add_parser("detail", help="📋 Query training job details")
    detail_parser.add_argument("--job-id", required=True, type=str, help="Job ID")

    # Stop training job
    stop_parser = subparsers.add_parser("stop", help="🛑 Stop training job")
    stop_parser.add_argument("--job-id", required=True, type=str, help="Job ID")

    # List cluster nodes
    list_parser = subparsers.add_parser("list-nodes", help="🖥️  List cluster nodes")
    list_parser.add_argument("--page", type=int, default=1, help="Page number (default: 1)")
    list_parser.add_argument("--size", type=int, default=10, help="Page size (default: 10)")
    list_parser.add_argument(
        "--pool",
        type=str,
        choices=["online", "backup", "fault", "unknown"],
        help="Resource pool filter",
    )

    args = parser.parse_args()

    # Show resource configuration and exit
    if args.show_resources:
        resource_manager = ResourceManager()
        resource_manager.display_available_resources()
        return 0

    # Set log level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("🐛 Debug mode enabled")

    try:
        # Get credentials from environment variables
        username, password = get_credentials()

        # Create API client
        config = InspireConfig(base_url=args.base_url)
        api = InspireAPI(config)

        # Authenticate
        logger.info("🔐 Authenticating with Inspire API...")
        api.authenticate(username, password)

        # Execute corresponding operation based on command
        if args.command == "create":
            # Convert hours to milliseconds
            max_time_ms = str(int(args.max_time_hours * 3600 * 1000))

            result = api.create_training_job_smart(
                name=args.name,
                command=args.start_command,
                resource=args.resource,
                framework=args.framework,
                prefer_location=args.location,
                project_id=args.project_id,
                workspace_id=args.workspace_id,
                image=args.image,
                task_priority=args.priority,
                instance_count=args.instances,
                shm_gi=args.shm_size,
                max_running_time_ms=max_time_ms,
                auto_fault_tolerance=args.auto_fault_tolerance,
                enable_notification=args.enable_notification,
                enable_troubleshoot=args.enable_troubleshoot,
            )

            print("\n✅ Creation result:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

        elif args.command == "detail":
            result = api.get_job_detail(args.job_id)
            print("\n📋 Job details:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

        elif args.command == "stop":
            api.stop_training_job(args.job_id)
            print("🛑 Job stopped")

        elif args.command == "list-nodes":
            result = api.list_cluster_nodes(
                page_num=args.page,
                page_size=args.size,
                resource_pool=args.pool,
            )
            print("\n🖥️  Node list:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

        else:
            parser.print_help()
            print("\n💡 Tip: Use --show-resources to view all available resource configurations")
            return 1

        return 0

    except (ValidationError, AuthenticationError, JobCreationError, InspireAPIError) as e:
        logger.error(f"❌ Error: {str(e)}")
        return 1
    except KeyboardInterrupt:
        logger.info("⏹️  Operation cancelled by user")
        return 1
    except Exception as e:
        logger.error(f"💥 Unexpected error: {str(e)}")
        if args.debug:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":  # pragma: no cover
    exit(main())
