---
name: inspire
description: Use when the user asks to interact with the Inspire HPC platform - submitting GPU jobs, syncing code, managing notebooks, monitoring status, and executing commands on the Bridge runner. Also use when the user asks about inspire-cli commands, workflows, or configuration.
allowed-tools: Bash(inspire *), Bash(uv run inspire *), Bash(ssh *)
---

# Inspire CLI

CLI for the Inspire HPC training platform. Manages GPU training jobs, interactive notebooks, code sync, and SSH tunnels to remote compute.

## Command Reference

### Jobs

```bash
inspire job create --name "exp-1" --resource "4xH200" --command "bash train.sh"
inspire job status <job-id>
inspire job logs <job-id> --tail 100 --refresh    # --refresh follows live
inspire job list                                   # List from local cache
inspire job update                                 # Refresh cache from API
inspire job command <job-id>                       # Show training command
inspire job stop <job-id>
inspire job wait <job-id>                          # Block until completion
```

**Quick run** (auto-selects compute group with most capacity):

```bash
inspire run "python train.py"
inspire run "bash train.sh" --gpus 4 --type h200
inspire run "python train.py" --sync --watch       # Sync + run + follow logs
inspire run "python train.py" --name "my-job" --priority 6 --max-time 24
inspire run "python train.py" --nodes 2            # Multi-node training
```

### Code Sync

```bash
git commit -am "msg" && inspire sync               # Always commit first
inspire sync --branch feature/new                  # Sync specific branch
inspire sync --remote upstream                     # Push to different remote
inspire sync --via-action                          # Fall back to workflow if tunnel is down
inspire sync --force                               # git reset --hard on Bridge
inspire bridge exec "git log -1"                   # Verify sync landed
```

Sync requires an active SSH tunnel by default. Pass `--via-action` to allow fallback to Gitea/GitHub Actions workflow.

### Bridge (Remote Execution)

```bash
inspire bridge exec "ls /path/to/output"           # Run command on Bridge
inspire bridge exec --no-tunnel "cmd"              # Force workflow path
inspire bridge ssh --bridge mybridge               # Interactive SSH shell
inspire bridge scp ./model.py /tmp/model.py --bridge mybridge
inspire bridge scp -d /tmp/results.tar.gz ./results.tar.gz --bridge mybridge
```

Commands execute in INSPIRE_TARGET_DIR on the Bridge runner.
If `--bridge` is missing/incorrect, use `inspire tunnel list`. If profile exists but SSH fails, use
`inspire tunnel status` and refresh via `inspire notebook ssh <notebook-id> --save-as <name>`.

### Notebooks

```bash
inspire notebook list                              # List CPU workspace notebooks
inspire notebook list --workspace gpu              # List GPU notebooks
inspire notebook create -n "dev" -r 1xH200 -i "pytorch25.06-py3:25.06"
inspire notebook status <id>
inspire notebook start <id>
inspire notebook stop <id>
inspire notebook ssh <id> --save-as mybridge       # Set up SSH tunnel to notebook
```

### SSH Tunnels

```bash
inspire tunnel add mybridge "https://notebook-url.../proxy/31337/"
inspire tunnel list                                # List configured bridges
inspire tunnel status                              # Check SSH connectivity
inspire tunnel test                                # Test connection + timing
inspire tunnel set-default mybridge
inspire tunnel update mybridge "https://new-url..."
inspire tunnel remove mybridge
inspire tunnel ssh-config --install                # Add shell-quoted ProxyCommand to ~/.ssh/config
ssh mybridge                                       # Direct SSH after ssh-config
```

### Images

```bash
inspire image list                                 # Official images
inspire image list --source private                # Custom images
inspire image detail <image-name>
inspire image save <notebook-id> -n my-image       # Save notebook as image
inspire image register -n my-img -v v1.0           # Register external image
inspire image set-default --job my-pytorch         # Set default for jobs
inspire image set-default --notebook my-pytorch    # Set default for notebooks
inspire image delete <image-name>
```

### Resources & Projects

```bash
inspire resources list                             # GPU availability
inspire resources nodes                            # Free 8-GPU nodes per group
inspire project list                               # Project quota table
```

### Configuration

```bash
inspire config show                                # Merged config with sources
inspire config check                               # Validate config + API auth
inspire config env                                 # Generate .env template
inspire init                                       # Initialize configuration
```

### Global Options

```bash
inspire --json <command>                           # Machine-readable JSON output
inspire --debug <command>                          # Enable debug logging
inspire --profile staging <command>                # Use env profile
```

## Configuration

**Layered config** (lowest to highest priority):

1. Defaults
2. Global: `~/.config/inspire/config.toml`
3. Project: `.inspire/config.toml`
4. Environment variables

**Key config sections:**

```toml
[context]
project = "project-name"       # Default project

[workspaces]
gpu = "workspace-id"           # GPU workspace
cpu = "workspace-id"           # CPU workspace
internet = "workspace-id"      # Workspace with internet

[job]
priority = 6                   # Default priority (1-10)
image = "pytorch25.06-py3:25.06"

[notebook]
image = "pytorch25.06-py3:25.06"

[paths]
target_dir = "/path/on/bridge"

[remote_env]
WANDB_API_KEY = "key"          # Auto-exported to remote commands
TORCH_HOME = "/path/to/cache"
```

## Resource Specs

| Spec | GPUs | CPUs | RAM |
|------|------|------|-----|
| `1xH200` | 1 | 15 | 200GB |
| `4xH200` | 4 | 60 | 800GB |
| `8xH200` | 8 | 120 | 1600GB |
| `8xH100` | 8 | - | - |

## Common Workflows

### Submit a training job

```bash
# 1. Commit and sync code
git add -A && git commit -m "ready to train"
inspire sync

# 2. Verify sync
inspire bridge exec "git log -1"

# 3. Submit job
inspire job create --name "experiment-1" \
  --resource "8xH200" \
  --command "cd $TARGET_DIR && . .venv/bin/activate && bash scripts/train.sh"

# 4. Monitor
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

`--save-as` profiles are notebook-bound. Reusing the same name for a different notebook refreshes the
tunnel mapping instead of reusing stale settings.

### Install packages (compute nodes have no internet)

```bash
# Use a CPU bridge/notebook that has internet access
ssh bridge-cpu "cd /path/to/project && pip install package-name"
```

## Platform Rules

1. **Commit before sync** -- `inspire sync` pushes the current branch; uncommitted changes are not synced.
2. **Jobs start in an unknown directory** -- Always prefix commands with `cd /path/to/project && ...`.
3. **Use `.` not `source`** for venv activation in job commands (POSIX compatibility).
4. **Compute nodes have NO internet** -- Use a CPU workspace/bridge for `pip install`, `git clone`, downloads.
5. **Priority range is 1-10** -- Values outside this range cause errors.
6. **SSH tunnel = instant execution** -- Without a tunnel, `bridge exec` falls back to a workflow (~30-60s).
7. **No `tunnel start` command** -- Create/refresh bridge profiles with `notebook ssh --save-as` or `tunnel add/update`, then verify via `tunnel status`.
8. **Notebook `--save-as` profiles are notebook-bound** -- Reusing an alias on another notebook refreshes that alias before SSH.
9. **Python output buffering** -- Use `print(..., flush=True)` or `PYTHONUNBUFFERED=1` for live log output.
10. **`[remote_env]` vars are auto-injected** into `bridge exec`, `bridge ssh`, `job create`, and `run` commands.
11. **`git pull` fails on GPU nodes** -- No internet. Use `inspire sync` via a bridge with internet access instead.
