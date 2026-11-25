---
name: job
version: 1.7.3
description: "Inspire training jobs: the platform behaviour a job record hides - why a job is really queuing, what fault tolerance and keep-alive do to quota, what survives termination, how the exec channel fails, what --dataset attaches and refuses to. Use when submitting training, debugging a stuck or failing run, reading logs, or running a command inside a job. NOT for interactive instances (see notebook skill), inference servings (see model skill), building/preheating images (see image skill), or catalogue dataset semantics (see dataset skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire job --help"
---

# job

**Read the shared skill first (`inspire skills read shared`): envelope, exit codes, the `--yes` gate, ID
resolution, and the `inspire api eval` authenticated-page escape hatch.** When the console has moved
past a job wrapper, use that escape hatch rather than guessing fields or inventing another planner.
Nothing below repeats the shared rules. Commands appear only to apply a rule; `--help` lists flags better.

## Platform Rules (what --help cannot tell you)

1. **The status word is a sample of one instant, never progress.** `job status` echoes what the platform just
   returned, and it is not monotonic: a stopped job can move back to `job_queuing`, and attempts can fail before
   any container becomes ready. Re-read status, instances, and events instead of remembering an earlier phase.

2. **Why a job is not running lives in events, and it is withdrawn once anything starts.** `status` can quote
   `not_running_reason` for `job_queuing|pending|creating|restarting`, but any instance in
   `instance_running|succeeded|reserving` suppresses it. An absent field on a sick job means "not applicable", not
   "nothing wrong"; fall back to `inspire job events <id>`.

3. **`Unschedulable` is a state inside queueing, not a refusal.** Do not treat it as terminal: a later attempt can
   still reach `instance_running`. A positive instance row outranks every event, so stopping a job needs the
   instance check, never the event alone.

4. **An attempt that was never placed leaves no logs at all.** The log store is keyed by pod name, so a request
   that fitted no node logged nothing and its cause exists only in events. Keep "no node, no `started_at`" (change
   the request or the pool) apart from "ran, then died" (read that attempt's logs).

5. **The timeline is the queue record, and `run` proves nothing.** Measure queue wait as
   `resource_prepared − created` when both exist. `run` can be stamped before `resource_prepared` or after
   `finished` on a retry that produced no instance; sparse `resource_prepared` means unreported, not zero wait.

6. **Termination wipes the forensics.** The platform does not retain a stopped job's last node and
   runtime attribution (measured — no payload field carries them after stop), so the terminal
   record no longer says where the work happened. Snapshot `job status --json`, `job instances`,
   and the failing attempt's logs *before* stopping.

7. **Nothing tells you why a job ended.** No `end_reason` and no effective-limit field has been
   found in any payload (measured), so `job_stopped` conflates your stop, a policy stop, and a
   fault-tolerance bypass; the `StopJob` event is the only signal of who ended it and when. Elapsed
   runs from *scheduling*, not container start. Low-utilization stops are explained by the
   utilization-reclamation rule (platform skill), not an undisclosed per-job timer; its exact clock
   basis (created vs container start) is deliberately unverified.

8. **`--fault-tolerance-max-retry` counts platform RUNS, not retries.** Its help says "Maximum platform runs";
   `1` means die on first failure. Set it to the runs you would accept; `job status` prints attempts, retries,
   and the current round. Check the current help for defaults and limits rather than remembering them.

9. **Low priority silently turns the restart loop on.** Fault tolerance auto-enables for a LOW project or priority
   below 4 unless you pass `--no-fault-tolerant`, and low-priority work is preemptible. It is judged on the priority
   that actually gets submitted, so a request the selected offer caps down into 1-3 gets the loop too. A deterministic bug can
   otherwise look like an infrastructure failure across many retry rounds.

10. **Priority is capped by the project and by the Scheduling Offer you selected; the reported value is
    the one that ran.** `--priority` runs 1-10 and is capped at the project's own priority, with both numbers
    reported. A fair-scheduling space also restricts by resource row (measured 2026-11-08: a space declaring
    `low` took 1-3 and refused 4+ at `1xH100`), and the CLI caps to the ceiling of the **offer actually
    selected** in one submit, a `--priority` you typed included, saying the value asked for, the ceiling and
    the evidence it came from. That evidence is only ever a declaration covering this offer: the selected
    group+quota row; else a generic quota row naming no group; else a row in the same group with the identical
    `gpu,cpu,mem` shape; else the group's rows, and only when they all declare the same levels. So `unknown`
    is neither `any` nor unlimited, disagreeing rows come back as `conflict` with no invented ceiling, and one
    group's `low` in the same space never caps an H200 offer. Where the selected offer declares nothing, the
    CLI submits what you asked for: an earlier acceptance is fallback evidence after a refusal, not an upper
    bound. Trust the reported value, and re-issue explicitly if the capped value is not the one you meant. Read the evidence before submitting with
    `inspire job availability`: its rows carry `priority_evidence`, `priority_effective` and
    `priority_ceiling` for the offer they describe, and `inspire job offers` prints the matching
    `--group`/`--quota` selector. `inspire job quota` is a deprecated compatibility view and no longer states
    a space-wide ceiling — a number from another group or a space-wide union was never yours to apply.
    Budgets are live values: re-read `project list` before submission. A below-zero sample was observed in
    `面向跨本体泛化的机器人自主学习方法研究` in `昇腾卡公共空间`, which is unrelated to 4090 placement and
    does not pin 4090 project selection. The only project observed bound to 可上网GPU资源 is `OpenETA`;
    verify that binding live rather than inferring a project from capacity or an unrelated project's budget.

11. **A finished-but-reserving round still holds the quota.** Keep-alive spellings (`job_success_reserving`,
    `succeeded_retaining`, …) collapse onto SUCCEEDED/FAILED in the canonical table, so the job-level status field
    hides the one state where `job stop` still buys capacity back; status says so when a round still holds its
    card. When the record states its keep-alive windows, `attempt_accounting.reserving_until` is UTC under
    `--json`. Absence means unreported, not imminent.

12. **`job list --active` means "still running or still holding quota".** It drops terminal
    FAILED/CANCELLED/SUCCEEDED states, but a finished-but-reserving round stays listed because stopping it returns
    the cards now. For a stricter view, use `--status RUNNING`. Under `--json`, `total` describes this filtered
    result; list statuses are at `.data.items[*].status` and retain platform spellings such as `job_running`.

13. **Exec has two streams and a pipeline-aware exit code.** `job exec` runs through a pod-side `execd` that
    keeps stdout and stderr as two files, so `stream_model` is `"separate"` and `stdout`/`stderr` are real
    values with their own byte offsets — branch on stderr freely, and read `--workers` output the same way:
    one pod's stderr can never land in another pod's stdout. **The other exec verbs are not like this**:
    `job upload` and `bridge exec` run over a browser PTY and answer `stream_model: "pty-merged"` with
    `stderr: null`, and `notebook exec` answers `"pty-merged"` when its record says the command got a
    far-side terminal and `"merged"` when it ran on a plain pipe — one file either way, both streams in it,
    so do not carry the separation assumption across verbs
    (ws_exec `as_dict`, notebook_shell, exec_cmd; JOB-EXEC-BENCHMARK, #51). `notebook exec --detach` always
    answers `"merged"` — one file both streams were redirected into, no PTY, and still no stderr to
    branch on. The exit code is still framed from
    the pipeline's per-stage list, so a failing stage before the last one is reported (`pytest -q | tail -20`
    fails the way pytest did) and a reader that stops early — `… | head -1` — still reports its own verdict
    instead of the writer's SIGPIPE. A loop or a branch is judged by the shell's own verdict, not by the
    test that ended it, so `while [ $i -lt N ]; do …; done` that ran to completion is a success; to ask for
    a non-zero exit, end on the command that fails. The framing never sets `pipefail`, so a payload that sets it keeps that
    choice; where no shell lists pipeline stages (a `/bin/sh` pod with no bash installed — execd prefers bash
    and falls back) the last stage's status is all there is (execd agent `frame_exit_code`; #52).

14. **`--timeout` is your watching budget, not the command's — and it has no default.** Nothing in
    `job exec` stops a command on a timer: the exec lives in the pod's `execd`, outliving your terminal.
    Leave `--timeout` out and the exec is followed to its terminal state and its **own** exit code comes
    back, however long it takes — a bare `job exec <id> -- make` is not supposed to know about
    `--detach` to get its answer, and the old 60 s default made it. Name it when you would rather come
    back for the rest: on expiry the CLI exits `124` carrying `exec_id` and both output offsets so you can
    resume with the printed `next` command, and a non-positive value is refused rather than read as "no
    limit". Either way the watch is a page at a time (each read waits on the pod for at most 30 s), so an
    unnamed budget never means one endless call. `--attach --follow` takes the same rule; a single
    `--attach` is one read, so `--timeout` beside it is refused. `Ctrl-C` is the same shape and exits
    `130`: it detaches the watcher and never cancels anything. **So `124` and `130` are not verdicts on the command** — the run is
    still going, and re-sending it starts a second one; resuming is what the offsets are for. Stopping the
    command is a separate call you have to mean: `--cancel EXEC_ID` kills a direct run's whole process group
    (`TERM`, then `KILL` after a 5 s grace) or, inside a `--session`, only the processes carrying that exec's
    `INSPIRE_EXECD_EXEC_ID` — the session shell is never killed — and the record survives so `--status` still
    explains the run. Waiting for a runnable instance stays a separate budget: `--wait-timeout` (default `0` =
    one lookup) expires with `14` and nothing sent, so `14` (never reached the pod) and `124` (stopped
    watching) cannot be read as each other. Losing contact has never meant "did not run": `exec_id` is
    generated before the request, so retrying the same id returns the record that already exists, while a new
    id is a deliberate second execution — send anything that writes, installs or launches via `--detach` and
    read it back with `--status` (issues #27, #50, #54). **A dead channel is not the end of the read either.**
    The platform closes an exec channel on a lifetime of its own — 330 s measured, whatever the route or the
    traffic (rule 15) — and the exec outlives it, so a channel
    that dies mid-watch is reopened and resumed from the byte each stream last reached, stdout and stderr
    separately, and you hear about it once on stderr: `channel lost after 331s: exec ex-… reconnected,
    resuming from stdout 8123 / stderr 17`. Nothing else changes — same `exec_id`, same offsets, same exit
    code, no byte twice — and `--attach … --after … --follow`, the command a `124` tells you to run, reads
    on exactly those terms rather than on a weaker reader of its own. Three failed reconnects in a row is
    where this reader stops, exit `13`, and the error carries the exact `--attach EXEC_ID --after
    STDOUT:STDERR` for the byte it stopped at. A `--timeout` that expires while the channel is down is
    still `124`: the budget is yours and it still bounds the watching.

15. **One exec carries a batch; one session carries a debugging run; a sweep is N execs.** Repeating
    `-c/--command CMD` is **one** exec on the pod — one `exec_id`, one shell, the commands run in order with no
    `set -e`, so cwd and exports set by an earlier `-c` still hold for a later one, and there is one
    establishment and one record instead of one invocation per step. The reported exit code is the **last
    non-zero** command's, or `0` when every command succeeds (not the last command's, as `a; b; c` would
    report) — the per-command statuses are kept in the pod-side record and are not printed, so a batch that
    must report each step belongs in one payload that writes them itself. A payload that runs `exit` ends the
    batch there, with that code recorded. Across invocations, state survives only in a `--session` (rule 22),
    never in a plain exec. `--workers SPEC` is the other direction: it fans out to **one exec per pod**, each
    with its own `exec_id`, stdout, stderr and exit code, `--parallel` capping how many pods hold a command
    session at once (default 4), `--summary-only` dropping output bodies while keeping every verdict. A sweep's
    own exit code ranks uncertainty above failure: any pod it stopped watching (`no_answer`) exits `124`,
    otherwise the largest failing command code, otherwise `13` — and a pod with no instance at that index is
    a verdict (`absent` / `not_schedulable` / `unreachable`), never a failed command. The transport is still
    the flaky part — sessions lost the
    channel repeatedly one morning while one session carried twelve commands 2 s apart (83 in another run), and
    a back-to-back loop degraded into 4 m 41 s of continuous failure on a provably alive pod — pace any loop,
    and after a suspicious failure pull the job's own record: "channel broke" and "pod died" print identically,
    and `StopJob`/`SuccessfulDeletePod` in events say which (ws_exec `PlatformWsSession`; execd agent
    `build_command_frames`).

16. **The command is no longer a PTY line, but it is still a command, not a data pipe.** `job exec` frames the
    command as JSON over a pod-side RPC, so the terminal's `MAX_CANON` line limit is gone: a 70 000-byte command
    runs, and a 64 KiB one-line script that used to come back with no exit code now comes back with one. The
    ceiling moved from the terminal driver to us — 1 MiB per command (`COMMAND_MAX_BYTES`), refused locally with
    exit `12` and nothing sent. `--stdin` reads the **command text** from stdin verbatim, with no shell quoting
    step (a TTY as stdin, and empty stdin, are both refused) — it does NOT wire stdin to the remote process,
    whose stdin is `/dev/null`, so an interactive or stdin-reading program is not served here. Send a script,
    never data; anything bigger than a command belongs on shared storage, or uploaded and then exec'd by path
    (job_exec `COMMAND_MAX_BYTES`, agent `MAX_PAYLOAD_BYTES`; JOB-EXEC-BENCHMARK).

17. **Logs are a paged search over a pod name that must already exist.** Between creation and scheduling there are
    no instance rows, so `job logs` can fail with "No instances found" rather than answering empty. Re-poll with
    `--tail`, or `--head` for crash-on-start. JSON includes `selected` / `instance_name` for the attempt.

18. **Memory metrics are ratios whose numerator is not RSS.** A high `memory_usage_rate` is not necessarily RSS
    pressure — the numerator is believed to include page cache, and converting it needs that container's own
    `/sys/fs/cgroup/memory.max`. Do not substitute the offer's requested memory for the container measurement.

19. **Image pull time is invisible from the job side and preheat gives no receipt.** `started_at`, phase age and
    elapsed all run from *scheduling*, so image pulling can be reported as time in phase, and `image preheat`
    answers only `preheat_requested` with no job-side state — judge a slow start from events and instance rows.

20. **A job pod is not a notebook, and egress is pool-specific.** `/dev/shm` is a *creation-time
    parameter*, not a fixed budget: jobs take `shm_gi` in the payload (`--shm`, then
    `INSPIRE_JOB_SHM_SIZE` / `[job].shm_size`, then `INSPIRE_SHM_SIZE` / `[defaults].shm_size`; the
    CLI default is 200 GiB capped to the selected quota memory, and is always sent) and notebooks
    take `shm_size` → `shared_memory_size`. Observed platform defaults vary by image and over time,
    so run `df -h /dev/shm` inside before trusting it, and raise `shm_gi` explicitly for
    Chromium-style batch rendering or DataLoader with many workers; remount from inside is refused.
    Do not infer reachability from GPU class: inspect the target pool with
    `workspace list --pools` and follow the workspace skill. Internal mirrors front neither GitHub nor
    public object storage, so upload assets and override the package index when using an internal pool.

21. **The account and the resource pool are shared.** Another research team works under this same account, so
    "my job" and "my GPU" are one pool. `inspire job list` lists training-job history from
    `ListJobs`, not notebooks, inference servings, or the console live task view.
    `inspire notebook list` covers your notebooks; its `--all` flag is rejected as an
    unsupported scope because `ListNotebooks` is user-scoped. `inspire job list --all-users`
    remains accepted as a compatibility no-op. Neither listing is the console's all-user live task
    view (`ListTaskDimension`). Use them before concluding something is orphaned.
    `created_by_id` is the
    usable owner identity; `created_by_name` is redacted. Before stop/preempt, compare
    `created_by_id` with `.data.user.id` from `inspire --json user whoami`. If either ID is absent or
    the check fails, stop and ask the account owner. A raw `GetJob` that returns `created_by.id` is a
    fallback for display, but never infer ownership from a name, list membership, workspace, or
    permission flag.

22. **`--session NAME` keeps one shell alive on the pod; the pod keeps it, not the platform.** A named
    session is a persistent `bash --norc --noprofile` under the pod's `/tmp/.inspire-execd`, fed through a FIFO,
    so cwd, exports and background jobs survive across separate `job exec` invocations — which a plain exec
    never does (each is a fresh shell; a `-c` batch only reaches across one call). Use it when a debug session
    spans several calls: cd once, export once, then run probes. Costs and limits worth knowing: **one run at a
    time** per session (a second concurrent run is refused `session-busy`, and a half-written submission
    poisons the session until its shell is replaced), so a session serialises rather than parallelises; its
    output log is shared and each record reads its own slice, so offsets are per-run, not per-log; if the
    session's shell dies mid-run the record is `outcome_uncertain` and the next run says it restarted rather
    than pretending the state is the same shell. The session dies with its pod, and so does everything else —
    **state does NOT survive a fault-tolerance retry round** (pod-local `/tmp`, and a retry round is a
    different pod), so cross-round state belongs on shared storage. With `--workers` each pod keeps its own
    session under the same name. `--cancel` inside a session kills the run's processes and never the shell.
    `--cleanup EXEC_ID` removes one **terminal** record's own files and nothing else — running records are
    never removable and nothing is ever garbage-collected, so a long debug session accumulates records you
    clean up yourself. Session names are 1-64 of `A-Za-z0-9._-`, starting with a letter or digit. Stdin is
    `/dev/null` and there is no PTY, so full-screen interactive programs belong to neither this nor a plain
    exec (execd agent sessions; #55).

23. **`--dataset` attaches a published dataset version; there is no path to pass instead.** `job create
    --dataset CODE@VERSION` (repeatable) validates every version against the target workspace *before*
    submitting, then adds a `dataset_info` entry per version to the v1 create body and reads the created job
    back with `GetJob` to confirm the mount took. Either step failing is a hard stop (exit 13; a read-back
    mismatch reports `DatasetMountNotApplied`), because a job that starts on missing input reveals itself
    hours later as a FileNotFoundError. Inside the container the version sits at
    `/inspire/dataset/<code>/<version>` — a platform-chosen path, not a mount point you pick, and not under
    any storage pool. Browsing and pre-checking live in the **dataset** skill, on a second portal session.

24. **Capacity and placement are part of submission, not a post-queue surprise.** `job availability` is the
    planning view: one workspace-scoped plan that pairs live capacity with each offer's priority evidence, and
    narrows with `--group` / `--resource` / `--quota` / `--priority`. `job offers` is the shapes-only view of the
    same rows and prints a copyable `--group`/`--quota` selector; a `resources specs` cache can omit an entire
    GPU-shape category and cannot prove current capacity. Choose the compute group and its complete `gpu,cpu,mem`
    row together, from one row. Preemptible capacity is counted by the **effective** priority after the caps: 1–3
    can plan only on idle GPUs, while 4+ may also count capacity held by preemptible 1–3 work but can still queue;
    with no `--priority` requested a plan reports `priority_effective` and `preemptible_usable` as null instead of
    choosing one for you. Multiply per-instance GPUs by `--nodes` and require the total within one group. Read
    `coverage` and `failures` before calling a group empty — a group that failed to answer is a gap, not zero
    capacity, and a group you named that cannot be priced is an error, not an empty table. Finish with a `--json`
    dry-run, verify the group, quota row, shape, priority and instance count describe one offer row, then submit
    with **those same values**: submission does not re-select what the preview resolved.

25. **Free GPUs are not the only admission condition.** When a job is PENDING with no scheduler
    objection, `job status` reads `GetProjectQuotaJobs` and puts its evidence in `pending_hint`: a
    limit of `-1` is unlimited, while `0` or `used >= limit` means the project has no room. The same
    block is visible with `inspire job quota-usage --project ...`; the console maps create codes
    301029/301030 to 「配额不足提醒，任务无法提交」. If the quota block is unlimited or still has
    room, task-count/parallel admission or node fit is the next suspect: compare
    `inspire job list --active` and `inspire notebook list`, then try another authorized project.
    Measured 2026-11-18: 前沿课题探索 GPU `73 used / 0 limit` stayed PENDING 15 min while 公共科研项目
    (unlimited, `558 used`) scheduled the same 1×H200 shape in ~2 min. A 1×H200 notebook is the
    minimum 1 GPU / 20 CPU / 200 GiB shape, not a whole node.

26. **Aggregate free GPUs are retired; node totals are still not fit.** `job availability`
    signals come from `workspace?Action=ListNodeDimension` (the console node view). If that probe
    fails, the signal is `source: unavailable`; no aggregate fallback is invented.
    `signals[].gpu_available` is a node-derived group total, not a per-request fit, and
    `inspire resources nodes` is the place to inspect node-level counts. The optional
    `gpu_low_priority_used` is an advisory console count (`low_priority_source: aggregate_advisory`),
    never free capacity. `ListNodeJobs` is the job service only: a node with 0 training jobs can
    still hold a notebook, serving, or other task.
    Measured 2026-11-18: aggregate said `8 free` for 开发区-H200-3号机房 while the console showed
    `GPU 160/160`, every node at 0 free; the scheduler was right and the aggregate was stale.

## Applying the rules

**why-is-it-queuing** (1-5: an instance row outranks every event)

```bash
inspire --json job status <id> | jq '.data | {status, not_running_reason, attempt_accounting}'
inspire job instances <id>               # never_placed vs failed_on_node — opposite remedies
inspire job events <id> | tail -20       # Unschedulable is a phase of queueing, not a refusal
```

**safe-restart-after-failure** (4, 7, 8, 14: never retry blind, cap runs before resubmitting)

```bash
inspire job instances <id>; inspire job logs <id> --attempt <N> --tail 200
inspire --json job exec <id> --timeout 20 -- 'test -f /work/run.rc && cat /work/run.rc'   # did the lost command run?
inspire --json job exec <id> --detach -- python prep.py           # side effects: detach, keep the exec_id
inspire --json job exec <id> --status ex-... | jq '.data | {state, exit_code, offsets}'   # ...it is still there
inspire --json job exec <id> --attach ex-... --after 0:0 --follow # ...and its output resumes by offset
inspire job create <same args> --no-fault-tolerant          # deterministic bug: die on first failure (never combine with --fault-tolerance-max-retry)
```

**forensic-capture-before-stop** (6, 7, 11: the record degrades the moment the job ends)

```bash
inspire --json job status <id> > pre-stop-<id>.json; inspire job instances <id> >> pre-stop-<id>.json
inspire job logs <id> --attempts; inspire job events <id> > pre-stop-<id>-events.txt
inspire job stop <id> --yes    # also returns a reserving round's quota instead of waiting out the window
```

**submit-with-preview** (8-10, 24: read live fit, effective priority and restart budget before anything is spent)

```bash
inspire job availability --workspace gpu --resource 4xH200 --priority 6   # capacity + per-offer evidence
inspire job offers --workspace gpu                                       # same rows, copyable selectors
inspire job create -n pr-123 -r 4xH200 -c "bash train.sh" --group <lg-id> --quota 4,80,900 --priority 6 --fault-tolerance-max-retry 3 --dry-run
inspire job create -n pr-123 -r 4xH200 -c "bash train.sh" --group <lg-id> --quota 4,80,900 --priority 6 --fault-tolerance-max-retry 3
inspire job wait <id> --until running --timeout 1800    # running = an instance reported started_at
inspire job logs <id> --tail 200
```

The create carries the same group, quota row, resource, priority and nodes the dry-run showed; change one and
the preview no longer describes the submit. `inspire run` picks the row for you and is deprecated — name the row.

**when-the-wrapper-is-behind-the-console** (use the CLI's login, but keep the request under your control)

```bash
inspire --json api eval '(async () => {
  const response = await fetch("/api/v2/train?Action=ListJobs", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({page_num: 1, page_size: 10, workspace_id: "ws-..."})
  });
  return {http_status: response.status, body: await response.json()};
})()'
```

Replace `ws-...` with the selected workspace's full ID. The same request through raw `api` must put
`workspace_id`, `page_num` and `page_size` in `--data` and returns `.data.Result.jobs/.total`; these
are complete platform rows, not the wrapped command's projection, so never print the whole body.
Use an observed console request directly.
The authenticated Playwright page also allows custom headers, arbitrary bodies, DOM reads and several
calls in one script — use `@script.js` or `@-` instead of shell-quoting a large program. If the
contract is unknown, inspect the page DOM, resource URLs and frontend JavaScript with the same
`api eval` command, following the shared skill; there is no separate discovery command. Raw requests
and eval have no read-only route guard or automatic `--yes` gate; neither is a dry run. Run writes
only with explicit intent; after a timeout or disconnect, read the job back before repeating anything.

**attach-a-dataset** (23: prove the version mounts in this workspace before the job exists)

```bash
inspire dataset check pointworld@behavior --workspace-id <ws>          # ok + the platform's backend path
inspire job create -n exp-1 -r 1xH200 -c "bash train.sh" --dataset pointworld@behavior --dry-run
inspire job create -n exp-1 -r 1xH200 -c "bash train.sh" --dataset pointworld@behavior
# in the container: /inspire/dataset/pointworld/behavior
```

## Boundaries

- NOT for interactive instances, Jupyter URLs, notebook SSH — see the **notebook** skill.
- NOT for inference servings, endpoints, API keys — see the **model** skill.
- NOT for building, registering, saving or preheating images — see the **image** skill; only the job-side
  consequence of a cold image is in scope here.
- NOT for choosing or checking the datasets rule 23 mounts, nor for cluster file I/O (checkpoints, your own
  corpora) — catalogue and mount semantics belong to **dataset**, shared-filesystem work to **storage** and
  **bridge**; `job upload` is only a small-file exec-channel path.
- NOT for envelope shape, exit codes, `--yes`/`--dry-run` semantics, ID resolution — see the **shared** skill.
