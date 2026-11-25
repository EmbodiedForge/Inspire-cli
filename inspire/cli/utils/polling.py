"""Polling honesty: a poll that cannot read has to say BLIND (issue #59).

The vocabulary this module is built on:

* A poll that **cannot read** is not a poll that **read nothing**. "The status call raised" is a
  statement about our instruments; "the job is still queuing" is a statement about the job. Looping
  on the first while printing the second tells a watcher their job is quiet at exactly the moment
  their eyes have been put out.
* BLIND means *no observation was possible*. It is never evidence of quiet, never evidence the job
  is healthy, and never a timeout. It marks the whole window it covers as unobserved, and the
  reader has to be able to see where that window starts.

A tracker converts N consecutive failed reads into a :class:`BlindError`, so a caller announces the
loss of sight instead of sleeping on a dead network until its own timeout expires.
"""

from __future__ import annotations

import time


class BlindError(RuntimeError):
    """Raised when `limit` consecutive polls failed to observe anything.

    Carries the streak, the last error, and when the loop last actually saw something, so the caller
    can report how much of the window is unobserved instead of handing over a silent gap.
    """

    def __init__(
        self,
        *,
        consecutive_failures: int,
        last_ok_at: float | None,
        last_error: Exception,
    ) -> None:
        super().__init__(
            f"{consecutive_failures} consecutive reads failed; last error: {last_error!r}"
        )
        self.consecutive_failures = consecutive_failures
        self.last_ok_at = last_ok_at
        self.last_error = last_error


class BlindTracker:
    """Counts consecutive failed reads so "unobservable" can be reported as itself.

    A single failure is a hiccup and stays a warning; a streak is blindness and becomes a
    :class:`BlindError`. ``last_ok_at`` stays ``None`` until the first successful read, because "we
    never saw it" and "we last saw it at T" are different claims.
    """

    def __init__(self, limit: int = 3) -> None:
        if limit < 1:
            raise ValueError("BlindTracker limit must be at least 1")
        self.limit = limit
        self.consecutive_failures = 0
        self.last_error: Exception | None = None
        self.last_ok_at: float | None = None

    def record_success(self) -> None:
        """A read came back: the streak clears and the observation timestamp moves."""
        self.consecutive_failures = 0
        self.last_error = None
        self.last_ok_at = time.time()

    def record_failure(self, exc: Exception) -> None:
        """Count one failed read, and go blind once the streak reaches the limit."""
        self.consecutive_failures += 1
        self.last_error = exc
        if self.consecutive_failures >= self.limit:
            raise BlindError(
                consecutive_failures=self.consecutive_failures,
                last_ok_at=self.last_ok_at,
                last_error=exc,
            )

    def reset(self) -> None:
        """Stop counting without claiming an observation.

        Used when a loop deliberately carries on despite not being able to see: the streak restarts
        so a continuing outage raises again after another `limit` failures, and ``last_ok_at`` stays
        put because nothing was actually observed since then.
        """
        self.consecutive_failures = 0
        self.last_error = None


__all__ = ["BlindError", "BlindTracker"]
