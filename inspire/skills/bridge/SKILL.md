---
name: bridge
version: 1.3.0
description: "Bridge profiles and SSH tunnels over a notebook's proxy URL: why the technique works at all, why restart breaks it, which transport runs what, how to move a file, a whole tree, or a branch without deleting anything at the destination, where commands actually execute (INSPIRE_TARGET_DIR on the shared filesystem), and the compliance line around no-egress zones. Use for fast remote commands, file transfer, and repairing a notebook-backed tunnel. NOT for managing the notebook itself (notebook skill) or submitting training (job skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire bridge --help"
---

# bridge (+ tunnel)

**Read the shared skill first (`inspire skills read shared`)**: envelope, exit codes, ID
conventions. The egress-zone rules behind several entries here live in the **platform** skill.

## Platform Rules (what --help cannot tell you)

1. **A bridge is a community technique, not a platform feature.** **(platform docs)** SSH into
   notebooks is explicitly unsupported by the platform; the bridge is an rtunnel SSH server
   bootstrapped into the container, reached through the notebook's proxy URL. Everything "odd"
   about it follows from that: restart breaks it (new container, new keys, new URL), host-key
   verification stays off (no platform key anchor to pin), and repair means re-pointing the
   profile — not debugging OpenSSH.

2. **The stored URL is a live root credential.** It embeds the notebook's Jupyter token; whoever
   holds it is root in that container. The CLI tightens profile file permissions and redacts on
   display — never paste `tunnel list -v` output anywhere, and treat a leaked URL like a leaked
   password (rotate by restarting the notebook).

3. **Commands land on the shared filesystem, not a sandbox.** `bridge exec` runs in
   `INSPIRE_TARGET_DIR` on that runner — the same paths every job and notebook see. Without a
   resolved target dir some verbs (`--file`) refuse. You are executing and writing on shared
   state: treat it like shell access to a colleague-visible machine. `inspire sync --dest PATH`
   overrides that target dir for one run and `--local DIR` reads branch/commit from another git
   checkout, which changes only where local git runs — configuration still comes from the cwd.

4. **Transport `auto` prefers SSH and falls back to Jupyter — and the fallback is a downgrade.**
   `--transport jupyter` is slower and PTY-less; useful for diagnosing whether SSH is the broken
   layer, wrong as a permanent setting. `bridge benchmark` times one SSH round trip;
   `tunnel status` separates "profile configured" from "SSH alive".

5. **Size the channel to the payload.** Inline commands stay small; scripts go through `--file`
   (local file, remote cwd) or `--stdin` (no shell quoting); long-running work goes `--detach`
   (survives disconnect, SSH keepalive is on). `--explain` prints the resolved wrapper and runs
   nothing — use it before blaming a failure on the remote side.

6. **The no-egress line is not ours to cross.** **(platform docs)** A bridge into an H-card-zone
   notebook sits behind a deliberate network partition, and after a 2025-09-04 mining incident the
   platform **explicitly bans FRP-class tunneling tools** — using any tunnel *in reverse* to give
   that zone internet access is both partition-defeating and against that announcement; this CLI
   does not do it and nothing here should be built to. The bridge itself (SSH *into* a notebook,
   outbound from the platform's point of view through the proxy URL) remains a community technique
   the platform calls unsupported, not a banned one — keep it that way: no reverse proxies, no
   egress relays. If GPU-zone egress is genuinely needed, that request goes to the platform.
   Upload assets via `bridge scp` / `storage put` instead.

7. **Profiles are local state with platform consequences.** Profiles live under
   `~/.inspire/bridges*.json`; `tunnel remove` deletes only the profile — the notebook and its
   files are untouched. `tunnel ssh-config --install` appends Host entries so plain
   `ssh`/`scp`/`rsync`/`git` work outside the CLI; those entries die with the next notebook
   restart too (rule 1).

8. **Ship a tree with `bridge sync`, which cannot delete what it lands on.** `inspire sync` ends in
   a remote `git reset --hard`, so uncommitted work at the destination dies with it; `bridge sync`
   is the tree mover when that destination matters. Its invariant: every `rm` in the remote script
   targets a path that same script created, and an existing destination is only ever **moved** — to
   `<dest>.inspire-bak-<UTC>` — never removed, so the run is reversible from the reported backup path
   (rule 3: that path is colleague-visible). Gate the swap with `--require pyproject.toml` when a
   missing file would make the new tree worse than the old one, and pass `--swap` when a fixed path
   (a mount, a job's `--command` argument) has to end up holding the new content: the rsync engine
   then stages through a sibling and displaces in one move; merge mode is `rsync -az --partial` with
   no `--delete`, so files the destination has and the source lacks survive. Two engines behind
   `--engine auto|rsync|tar`: rsync for incremental re-syncs, tar (one archive, one upload,
   `sha256sum` checked before anything moves) which only needs what the pods already ship. `auto`
   probes the bridge and falls back to tar with a note
   in `engine_note`; `--engine rsync` installs rsync where it can, and the install script backs up
   and restores the distro source lists it touches, so it does not leave the container half-updated.
   Tar has no staging mode — `--engine tar --swap` still displaces to `<dest>.inspire-bak-<UTC>`, it
   just does not pre-stage. Only rsync genuinely requires SSH: on a bridge with no SSH ingress the
   tar engine still runs, staging the archive through the Jupyter Contents API and running the script
   through the notebook shell, so `--engine rsync` there is refused with exit 12 and a pointer to
   `--engine tar`. That staging has a catch worth knowing: the Contents API is rooted at the notebook
   server's own directory, so the archive arrives under `<server root>/tmp/<name>` rather than the
   container's `/tmp`, and the remote script resolves it **by name** under both roots (`/tmp/<name>`
   first) instead of trusting one absolute path. `--dry-run` reports the tree's file and byte counts
   and writes nothing to the bridge (the bridge is still probed for availability); over Jupyter it
   resolves no web session at all.

## Applying the rules

**Establish and verify (1, 2, 4):**

```bash
inspire notebook ssh <NB> --save-as mybridge     # bootstrap rtunnel + save the profile
inspire tunnel status                            # config + live SSH check
inspire tunnel test                              # one timed round trip
```

**Run work on the runner (3, 5):**

```bash
inspire bridge exec "df -h /inspire"
inspire bridge exec --file scripts/task.sh -- --out results/probe
cat check.sh | inspire bridge exec --stdin
inspire bridge exec --detach --log -- bash long.sh
inspire bridge exec --explain "python -V"        # resolve only, run nothing
```

**Move files (6, 8):**

```bash
inspire bridge scp ./train.py /inspire/hdd/project/<p>/public/train.py
inspire sync                                     # local repo -> shared FS (git bundle; --pull reverses)
inspire sync --dest /workspace/other             # ...into a path other than the configured target_dir
inspire bridge sync ./experiment --to /root/work/experiment  # whole tree, destination never deleted
inspire bridge sync ./pkg --to /root/work/pkg --require pyproject.toml --dry-run
```

**Repair after a restart (1):** `notebook doctor` → `notebook ssh <NB> --save-as NAME` (or
`tunnel update NAME --url <new proxy url>`).

## Boundaries

- NOT for the notebook lifecycle (create/stop/save/doctor internals) — **notebook** skill.
- NOT for pure shared-filesystem browsing without a runner — **storage** skill.
- NOT for job-side exec (per-pod, PTY-merged, no bridge involved) — **job** skill rules 13–16.
- Egress zones and why reverse-tunneling is off the table — **platform** skill.
