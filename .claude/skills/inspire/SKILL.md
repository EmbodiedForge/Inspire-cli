---
name: inspire
description: Use when the user asks to interact with the Inspire HPC platform - submitting GPU jobs, syncing code, managing notebooks, monitoring status, and executing commands on the Bridge runner. Also use when the user asks about inspire-cli commands, workflows, or configuration.
allowed-tools: Bash(inspire *), Bash(uv run inspire *), Bash(ssh *)
---

# Inspire CLI

CLI for the Inspire HPC training platform. Use `inspire --help` and `inspire <command> --help` for full command/flag reference.

## Common Workflows

### Submit a training job

```bash
git add -A && git commit -m "ready to train"
inspire sync
inspire bridge exec "git log -1"              # Verify sync landed
inspire job create --name "exp-1" \
  --resource "8xH200" \
  --command "cd $TARGET_DIR && . .venv/bin/activate && bash scripts/train.sh"
inspire job logs <job-id> --tail 50 --refresh
```

### Quick run with auto-sync

```bash
git commit -am "experiment" && inspire run "bash train.sh" --sync --watch
```

### Set up SSH access to a notebook

```bash
inspire notebook list --workspace gpu
inspire notebook ssh <notebook-id> --save-as bridge
inspire tunnel status
ssh bridge
```

### Install packages (compute nodes have no internet)

```bash
ssh bridge-cpu "cd /path/to/project && pip install package-name"
```

## Platform Rules

1. **Commit before sync** — `inspire sync` pushes the current branch; uncommitted changes are not synced.
2. **Jobs start in an unknown directory** — Always prefix commands with `cd /path/to/project && ...`.
3. **Use `.` not `source`** for venv activation in job commands (POSIX compatibility).
4. **Compute nodes have NO internet** — Use a CPU workspace/bridge for `pip install`, `git clone`, downloads.
5. **Priority range is 1-10** — Values outside this range cause errors.
6. **SSH tunnel required** — `bridge exec`, `sync`, etc. need an active tunnel. Set one up with `notebook ssh --save-as` or `tunnel add`.
7. **No `tunnel start` command** — Create/refresh profiles with `notebook ssh --save-as` or `tunnel add/update`, verify via `tunnel status`.
8. **`--save-as` profiles are notebook-bound** — Reusing an alias on another notebook refreshes that alias before SSH.
9. **Python output buffering** — Use `print(..., flush=True)` or `PYTHONUNBUFFERED=1` for live log output.
10. **`git pull` fails on GPU nodes** — No internet. Use `inspire sync` via a bridge with internet access.
