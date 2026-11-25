---
name: notebook
version: 1.2.0
description: "Inspire notebooks: the two transports and when each exists, why the tunnel URL is a credential, why a restarted notebook breaks SSH by design, what idle reclamation does to an unattended box, and what stop/save actually preserve. Use for an interactive GPU box, Jupyter URL, exec/upload/download, or repairing the SSH tunnel. NOT for batch training (job skill), inference servings (model skill), or snapshot mechanics (image skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire notebook --help"
---

# notebook

**Read the shared skill first (`inspire skills read shared`): envelope, exit codes, the `--yes`
gate, ID resolution.** Platform-wide rules (egress zones, storage pools, reclamation, billing) are
in the **platform** skill; only the notebook-facing consequences are here.

## Platform Rules (what --help cannot tell you)

1. **Two transports, and they fail differently.** `exec`/`upload`/`download`/`url` ride the
   notebook's Jupyter channel over HTTPS — always available, no setup. `ssh`/`top` ride an rtunnel
   SSH path we bootstrap into the container. **(platform docs)** SSH into a notebook is explicitly
   *not a platform-supported feature*; rtunnel is the community technique. So a broken tunnel is a
   thing to repair (`notebook doctor`), not a platform regression to report.

2. **A notebook restart breaks SSH by design, and host-key checks stay off.** The SSH server and its
   keys live in the container and are recreated per start; every tunnel dials `root@localhost`
   through the proxy, so there is no platform key anchor to pin (measured; accepted consequence).
   After a restart: re-point with `notebook ssh <NB> --save-as NAME`
   or `tunnel update NAME --url ...` — see the bridge skill.

3. **The proxy URL is a live credential.** It embeds the Jupyter token, which is root inside that
   container; `tunnel list -v` and `proxy-url`-style output must never land in issues or chats.
   The CLI redacts on display and tightens profile file permissions — do not route around that.

4. **exec is a Jupyter terminal: non-interactive, one stream.** It is for commands, not shells/TUIs
   (use `notebook ssh`). As on every PTY-style channel here, never branch on "stderr was empty" and
   never treat a missing exit code as "the command failed" — re-read state first (job skill rules
   13–16 describe the same failure grammar).

5. **A notebook on an internal compute pool cannot download from the public internet.** GPU class and
   workspace name do not establish reachability. Inspect the parent pool and its raw `cluster_name`
   with `workspace list --pools`, then follow the workspace skill's explicit declaration rule. Prepare
   environments on a pool with verified internet access, then share via the filesystem or an image.
   Internal mirrors (nexus) usually still work — configure and verify them in the target notebook.

6. **An unattended notebook can be reclaimed under you.** **(platform docs)** In fair-scheduled GPU
   spaces, reclamation is judged on GPU utilization — a 15% threshold is the documented example,
   public-resource notebooks are reclaimed after 18 h up, and there is no one-connected exemption.
   Exact windows and thresholds are workspace-specific: query the current one with
   `inspire resources policy --workspace <name> --workload notebook` rather than applying historical
   numbers elsewhere. Long CPU-side phases belong in a CPU workspace, and anything worth keeping
   belongs on `/inspire/...`, not the container disk.

7. **stop loses the container; only the shared filesystem survives.** Local state, installed
   packages, shell history — gone. Persistence choices, in order of preference: shared filesystem
   paths, a baked image (`image save`, which captures only the writable layer — image skill rule 6),
   or a `--post-start-script` that rebuilds the environment on `start`.

8. **Names are not unique platform-side; the CLI resolves by name.** A duplicate notebook name turns
   every later command into an ambiguity to disambiguate. Create with a distinct name; resolve by
   `nb-<uuid>` or unambiguous prefix when scripting.

9. **`upload` verifies and refuses to clobber.** Transfers are SHA-256-checked and an existing
   remote path is rejected without `--overwrite` — deliberate, because a notebook's working
   directory is often a shared checkout others see.

10. **`notebook update` is a full-body replace.** It posts a platform-shaped payload
    (`--payload-json`); fields you omit may reset. Prefer `status` + recreate over blind partial
    updates.

11. **Capacity first, create second.** `notebook offers` and `notebook availability` require an
    explicit `--workspace` or `--workspace-id` and name that workspace in their output. Offers are
    requestable quota/price shapes and aggregate counters are only capacity; neither proves
    placement. Workspace aliases are user naming only: inspect them with `workspace list`. Find
    resource classes with account-wide `inspire resources list --resource <class>`. The workspace
    skill owns pool identity and declarations: `workspace list --pools` shows each logical group's
    parent `cg-` pool and raw `cluster_name`; `nat`/`int` is only a heuristic, and `compute_net`
    is only a network ID. Declarations are stored
    per parent `cg-` pool, shared by its logical groups, and never inferred automatically. Undeclared
    means unknown. `create --dry-run` previews the exact request. Copy each quota row whole.

12. **"Works in a notebook" licenses nothing about a job pod.** `/dev/shm` is a creation-time
    parameter (`shm_size` on notebooks, `shm_gi` on jobs) whose platform default varies by image and
    over time. Re-measure with `df -h /dev/shm`; do not remember an old default. Prototype here, but
    re-verify the workload's shared-memory appetite before moving it to a job, and set the size
    explicitly at create when it matters (job skill rule 20).

13. **The container's own memory report is the host's, and an OOM kill leaves no trace inside it.**
    Measured 2026-11-06 on a CPU notebook: `free -g` and `/proc/meminfo` said 1511 GiB while
    `/sys/fs/cgroup/memory.max` capped it at 400 GiB (the platform's own `notebook status` agreed with
    the cgroup), and `/proc/cpuinfo` said 112 CPUs against a `cpu.max` of 40 cores. When the cgroup
    refuses the overspend the kernel OOM-killer takes the largest process, and the container keeps
    silent: `dmesg` is refused (no `CAP_SYSLOG`), there is no syslog, the dead process leaves an empty
    log, and the live per-process view answers only over a saved notebook tunnel. The one surviving
    record is `oom_kill N` in
    `/sys/fs/cgroup/memory.events`. `inspire notebook resource <NB>` reads that tree from inside over the
    Jupyter path and exits non-zero on a recorded kill, or on a declaration that disagrees with the
    cgroup. Size a fan-out from `memory.max`, never from
    `MemAvailable`, and treat a child that "looks alive" after a stall as suspect until an artifact says
    it advanced (rule 4; job skill on liveness).

14. **`exec` runs the command off that terminal, in every mode, because giving up on the terminal used to
    kill the command.** The WebSocket can close mid-command, and until the transport's `exitReason` is read
    there is no way to tell that from a slow command: measured 2026-11-07, a *silent* command was abandoned
    after 267s of a `--timeout 600` and a chatty one after 327s, so chattiness bought nothing — and the exec
    2s later showed the process gone rather than still running, because giving up deletes the terminal and
    the command ran in that terminal's shell. No mode does that any more. A plain `notebook exec` starts the
    command in its own session on the notebook (detached work survives the channel: `setsid nohup …`
    outlived the terminal that launched it), tails `/tmp/inspire-exec/<exec-id>/stdout` from a byte offset
    **until the record says the command is over**, and prints one stderr line when the channel dies and the
    read resumes. So a long command needs no flag at all to get through: `--timeout` is for a caller that
    cannot wait that long, and spending it exits 124 with the offset to resume from, `--attach <exec-id>
    --after N --follow`, leaving the command alone. That 124 says which of the two ended the watch — this
    process's own budget, or reads that brought back no record — because only one of them is helped by a
    bigger `--timeout`, and `observation_error` is filled for the second alone. Omit it and there is no
    patience here to run out. So
    exit 14 now means the call meant to *start* it never answered, which leaves it unknown whether it
    started: read the record before re-running a side effect. `--detach` is the same launch without the
    watching, and `--attach <exec-id> --follow` reads a record back from any later process. The command
    keeps the exit code either way (the record's `status` file), and that record directory does not survive
    a stop (rule 7).
    The default path asks the image for a far-side PTY, so `isatty()` stays true and stdout/stderr stay one
    ordered stream; an image without util-linux `script` gets a plain pipe instead, which the run says out loud
    and `stream_model` reports as `merged` rather than `pty-merged`.

## Applying the rules

**Get a box and prove it (2, 5, 11):**

```bash
inspire notebook availability --workspace-id <ws>  # what this workspace can give now
inspire notebook create -n dev -r 1xH200 --image <img> --dry-run
inspire notebook create -n dev -r 1xH200 --image <img>
inspire notebook wait <NB> --until running --timeout 600
```

**Run and move files without SSH (1, 4, 9, 14):**

```bash
inspire notebook exec -n <NB> -- 'nvidia-smi'                             # off the terminal, to its end (14)
inspire notebook exec -n <NB> --timeout 300 -- 'make -j8'                 # leaves at 300s: 124 + cursor
inspire notebook exec -n <NB> --detach -- 'python train.py --epochs 90'   # start it, return in seconds
inspire notebook exec -n <NB> --attach <exec-id> --follow                 # read it back; 124 = still running
inspire notebook resource <NB>                        # the cap the kernel enforces, not what free says
inspire notebook upload -n <NB> ./local.py /root/remote.py --dry-run   # where would it land?
inspire notebook download -n <NB> /root/out.tar ./out.tar
```

**Fast SSH via a bridge profile (1, 2, 3):**

```bash
inspire notebook ssh <NB> --save-as mybridge       # establishes rtunnel + named profile
inspire bridge exec -b mybridge "hostname"
inspire notebook doctor                            # tunnel broken? diagnose before re-doing setup
```

**Park it safely (6, 7):**

```bash
inspire notebook stop <NB> --yes                   # high-risk gate; container state is lost
inspire image save <NB> -n my-env -v v1 --dry-run  # persist the environment first, if worth it
```

The top-level shortcut `inspire stop notebook <NB>` is not this command: the CLI now rejects a
resource type word used as a reference, and older versions could resolve that word to an unrelated
notebook. Use `inspire notebook stop`, and `inspire resolve <id> --type notebook` first when the id
is uncertain.

## Boundaries

- NOT for batch training, queueing, per-attempt forensics — **job** skill.
- NOT for snapshot internals, registries, visibility — **image** skill (this skill only decides
  *when* a snapshot is the right persistence move).
- NOT for tunnel profile mechanics, `bridge exec`/`scp`/`sync` — **bridge** skill.
- Dataset catalogue search and mount-verdict semantics (`--dataset CODE@VERSION` mounts
  `/inspire/dataset/<code>/<version>`) — **dataset** skill.
- Egress zones, storage pools, reclamation numbers, billing — **platform** skill.
