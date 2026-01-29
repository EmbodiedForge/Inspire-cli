"""Log file operations for Inspire CLI.

Handles reading logs from the shared filesystem.
"""

import glob
import os
import time
from pathlib import Path
from typing import Callable, Iterator, List, Optional


class LogNotFoundError(Exception):
    """Log file not found."""
    pass


class LogReader:
    """Read and stream log files from the shared filesystem."""

    def __init__(self, target_dir: str, pattern: str = "training_master_*.log"):
        """Initialize log reader.

        Args:
            target_dir: Base directory on shared filesystem (INSPIRE_TARGET_DIR)
            pattern: Glob pattern for finding log files
        """
        self.target_dir = Path(target_dir)
        self.pattern = pattern

    def find_logs(self, job_id: Optional[str] = None) -> List[Path]:
        """Find log files matching pattern.

        Args:
            job_id: Optional job ID to filter by (looks for job ID in path)

        Returns:
            List of matching log file paths, sorted by mtime (newest first)
        """
        # Search for logs in target directory
        search_pattern = str(self.target_dir / "**" / self.pattern)
        matches = glob.glob(search_pattern, recursive=True)

        # Convert to Path objects
        paths = [Path(m) for m in matches]

        # Filter by job_id if specified, but fall back to all logs if
        # none contain the job ID (e.g., when log filenames do not
        # embed the job identifier).
        if job_id:
            filtered = [p for p in paths if job_id in str(p)]
            if filtered:
                paths = filtered

        # Sort by modification time (newest first)
        paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        return paths

    def find_latest_log(self, job_id: Optional[str] = None) -> Optional[Path]:
        """Find the most recent log file.

        Args:
            job_id: Optional job ID to filter by

        Returns:
            Path to most recent log file, or None if not found
        """
        logs = self.find_logs(job_id)
        return logs[0] if logs else None

    def read_full(self, log_path: Path) -> str:
        """Read entire log file.

        Args:
            log_path: Path to log file

        Returns:
            Full log content

        Raises:
            LogNotFoundError: If file doesn't exist
        """
        if not log_path.exists():
            raise LogNotFoundError(f"Log file not found: {log_path}")

        return log_path.read_text(encoding="utf-8", errors="replace")

    def read_tail(self, log_path: Path, lines: int = 100) -> List[str]:
        """Read last N lines of log file efficiently.

        Args:
            log_path: Path to log file
            lines: Number of lines to read from end

        Returns:
            List of last N lines

        Raises:
            LogNotFoundError: If file doesn't exist
        """
        if not log_path.exists():
            raise LogNotFoundError(f"Log file not found: {log_path}")

        # Read from end efficiently
        with open(log_path, "rb") as f:
            # Seek to end
            f.seek(0, 2)
            size = f.tell()

            if size == 0:
                return []

            # Read in chunks from end
            block_size = 4096
            blocks = []
            pos = size

            # Estimate bytes needed (assume ~100 bytes per line)
            bytes_needed = lines * 200

            while pos > 0 and sum(len(b) for b in blocks) < bytes_needed:
                read_size = min(block_size, pos)
                pos -= read_size
                f.seek(pos)
                blocks.append(f.read(read_size))

            # Combine and decode
            content = b"".join(reversed(blocks))
            text = content.decode("utf-8", errors="replace")

            # Split and return last N lines
            all_lines = text.splitlines()
            return all_lines[-lines:]

    def follow(
        self,
        log_path: Path,
        callback: Callable[[str], None],
        poll_interval: float = 0.5
    ) -> Iterator[str]:
        """Stream new lines as they appear (like tail -f).

        Args:
            log_path: Path to log file
            callback: Function to call with each new line
            poll_interval: Seconds between polls for new content

        Yields:
            New lines as they appear

        Raises:
            LogNotFoundError: If file doesn't exist
        """
        if not log_path.exists():
            raise LogNotFoundError(f"Log file not found: {log_path}")

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # Go to end
            f.seek(0, 2)

            while True:
                line = f.readline()
                if line:
                    line = line.rstrip("\n\r")
                    callback(line)
                    yield line
                else:
                    time.sleep(poll_interval)

    def get_log_path_for_job(
        self,
        job_id: str,
        timestamp: Optional[str] = None,
    ) -> Optional[Path]:
        """Find the best matching log path for a job.

        Uses existing discovery logic and prefers logs that contain the
        given job ID (and optionally timestamp) in the path.

        Args:
            job_id: Job identifier
            timestamp: Optional timestamp (YYYYMMDD_HHMMSS format)

        Returns:
            Path to log file if found, None otherwise
        """
        logs = self.find_logs(job_id)
        if not logs:
            return None

        if timestamp:
            for log in logs:
                if timestamp in log.name or timestamp in str(log):
                    return log

        return logs[0]
