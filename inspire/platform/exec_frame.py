"""Shell framing that reports a remote command's exit code faithfully.

Both remote exec paths used to frame the caller's command as `<command>` then `EC=$?`. `$?` is the *last*
stage of a pipeline, so `pytest -q | tail -20` reported success through `job exec` on a failed suite
(issue #52). Three framings look obvious and are all wrong here:

* a bare `set -o pipefail` trades the false success for a false failure on the most common pipe there is:
  measured under bash, `seq 100000 | head -1` goes from 0 to 141 (SIGPIPE) because `head` stops reading;
* a `( … )` wrapper around the command drops `cd` and `export` side effects, which the reused
  `PlatformWsSession` relies on - `job upload` runs `cd`/`export` in one command and the build in the next;
* `set -o pipefail` is a hard error in POSIX `sh` (dash), where it kills the script rather than being
  ignored, and the detached recorder runs under `/bin/sh`.

So the frame asks the shell for its per-stage list instead of changing its behaviour: bash publishes
`PIPESTATUS` for the pipeline it just ran, and the frame reports the last stage, or - when the last stage
succeeded and the list describes a pipeline - the first earlier stage that failed with something other than
SIGPIPE. Nothing runs in a subshell and no shell option is touched, so side effects and the caller's own
`pipefail` setting survive.

The list is only read when it holds two or more stages, because `PIPESTATUS` outlives the pipeline it
describes. After a compound command it holds the last pipeline executed *inside* the construct, which for
`while`, `until` and `if` is the condition test that ended it - a `while [ $i -lt N ]` loop therefore
"fails" with the status of the test that stopped it, under a shell whose own `$?` for the loop is 0. A
single-element list says nothing the `$?` does not already say, so it is dropped rather than consulted, and
the loop's verdict is the loop's own.
A shell without `PIPESTATUS` (dash, busybox ash) takes the other branch of the `case` and gets exactly the
old `$?`: a pipeline whose last stage succeeded still looks successful there, which is the behaviour we
had before, rather than a broken command.

The `case` guard is deliberate, and so is its position: the stage list must be read in the statement
immediately after the command, because any command in between - including the `[ … ]` of an `if` - replaces
`PIPESTATUS` with its own single-element list.
"""

from __future__ import annotations

#: A stage killed by SIGPIPE. `cmd | head -1` ends this way on the writer and means "the reader had enough",
#: not "the command failed", so it is not reportable as a failure.
SIGPIPE_STATUS = 141

#: Variables the frame owns. The `__inspire_` prefix is the same namespace the detached recorder already
#: uses (`__inspire_rc`), chosen so a payload is unlikely to collide with them by accident.
STATUS_VAR = "__inspire_status"
STAGES_VAR = "__inspire_stages"
STAGE_VAR = "__inspire_stage"
STAGE_N_VAR = "__inspire_stage_n"


def frame_exit_code(command: str, *, result_var: str = STATUS_VAR) -> str:
    """Return shell text that runs `command` and leaves its exit code in `$<result_var>`.

    POSIX-safe text: it runs unchanged under bash (where pipelines are reported faithfully) and under a
    shell with no `PIPESTATUS` (where it degrades to the plain `$?` of the command). The command is
    emitted verbatim, so a payload that calls `exit` still exits the shell it was given - callers that
    must survive that wrap the frame in a subshell themselves.
    """
    body = command.rstrip("\r\n")
    return "\n".join(
        [
            f"{result_var}=",
            body,
            # Read the stage list in the statement right after the command; the `case` word itself runs no
            # command, so PIPESTATUS still belongs to the payload. `${BASH_VERSION+set}` is a POSIX
            # expansion, so a shell that is not bash takes the fallback arm and never expands PIPESTATUS.
            (
                f'case "${{BASH_VERSION+set}}" in set) {STAGES_VAR}="${{PIPESTATUS[*]}}|$?";; '
                f'*) {STAGES_VAR}="|$?";; esac'
            ),
            # `$?` travelled inside the stage list, after the `|`: an assignment on its own would have
            # replaced it before the list could be read.
            f"{result_var}=${{{STAGES_VAR}##*|}}",
            # How many stages that list actually describes. One element is not a statement about a
            # pipeline: after a compound command PIPESTATUS still holds the last pipeline run *inside* it,
            # and for a `while`/`until`/`if` that is the test that ended the construct, not the payload.
            f"{STAGE_N_VAR}=0; for {STAGE_VAR} in ${{{STAGES_VAR}%|*}}; do {STAGE_N_VAR}=$(({STAGE_N_VAR}+1)); done",
            (
                f'if [ "${result_var}" = 0 ] && [ "${{{STAGE_N_VAR}}}" -gt 1 ]; then '
                f"for {STAGE_VAR} in ${{{STAGES_VAR}%|*}}; do "
                f'case "${STAGE_VAR}" in 0|{SIGPIPE_STATUS}) ;; *) {result_var}=${STAGE_VAR}; break;; '
                "esac; done; fi"
            ),
            # Never hand back something the exit-code parser cannot read: a payload that clobbered our
            # variables degrades to the plain `$?` rather than to a missing exit code.
            f"case ${result_var} in ''|*[!0-9]*) {result_var}=${{{STAGES_VAR}##*|}} ;; esac",
        ]
    )


__all__ = [
    "STAGES_VAR",
    "STAGE_N_VAR",
    "STAGE_VAR",
    "STATUS_VAR",
    "frame_exit_code",
]
