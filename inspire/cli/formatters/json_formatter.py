"""JSON output formatter for CLI commands.

Provides structured JSON output for machine-readable parsing.
"""

import json
import sys
from typing import Any, Dict, Optional


def format_json(data: Any, success: bool = True) -> str:
    """Format data as JSON output.

    Args:
        data: Data to format (dict, list, or other JSON-serializable)
        success: Whether the operation was successful

    Returns:
        JSON string with standard wrapper
    """
    output = {
        "success": success,
        "data": data
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def format_json_error(
    error_type: str,
    message: str,
    code: int = 1,
    hint: Optional[str] = None
) -> str:
    """Format an error as JSON output.

    Args:
        error_type: Type of error (e.g., "ConfigError", "AuthenticationError")
        message: Error message
        code: Exit code
        hint: Optional hint for fixing the error

    Returns:
        JSON string with error details
    """
    error_data: Dict[str, Any] = {
        "type": error_type,
        "code": code,
        "message": message,
    }
    if hint:
        error_data["hint"] = hint

    output = {
        "success": False,
        "error": error_data
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def print_json(data: Any, success: bool = True) -> None:
    """Print data as JSON to stdout.

    Args:
        data: Data to format
        success: Whether the operation was successful
    """
    print(format_json(data, success))


def print_json_error(
    error_type: str,
    message: str,
    code: int = 1,
    hint: Optional[str] = None
) -> None:
    """Print an error as JSON to stderr.

    Args:
        error_type: Type of error
        message: Error message
        code: Exit code
        hint: Optional hint
    """
    print(format_json_error(error_type, message, code, hint), file=sys.stderr)
