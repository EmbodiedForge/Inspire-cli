# Inspire CLI

Command-line interface for the Inspire HPC training platform.

> **Access Restricted**: This tool is only available to Inspire platform members.

> **Note**: This project was 99% vibe-coded with [Claude Code](https://claude.com/claude-code). Initial version by [Huaizz-shawen](https://github.com/Huaizz-shawen).

## Installation

```bash
uv tool install git+https://<your-gitea-host>/cyteena/inspire-cli.git
```

## Configuration

Set the required environment variables:

```bash
# Required
export INSPIRE_USERNAME="your_username"
export INSPIRE_PASSWORD="your_password"

# Required for sync/bridge/log operations (shared filesystem root)
export INSPIRE_TARGET_DIR="/path/to/shared/filesystem"

# Gitea Actions (required for sync/bridge exec/remote logs)
export INSP_GITEA_REPO="owner/repo"
export INSP_GITEA_TOKEN="..."          # Gitea personal access token
export INSP_GITEA_SERVER="https://gitea.example.com"

# Optional
export INSP_IMAGE="your_image:tag"  # Default Docker image for `inspire job create` (same as --image)
export INSPIRE_BASE_URL="https://qz.sii.edu.cn"  # default
export INSPIRE_LOG_PATTERN="training_master_*.log"  # default
export INSPIRE_JOB_CACHE="~/.inspire/jobs.json"  # default
export INSPIRE_TIMEOUT="30"  # API timeout in seconds
export INSPIRE_MAX_RETRIES="3"  # Max API retries
export INSPIRE_RETRY_DELAY="1.0"  # Retry delay in seconds
```

## Quick Start

```bash
# Check installation
inspire --version
inspire --help

# Check configuration and authentication
inspire config check

# Sync code to Bridge (before launching training)
inspire sync                    # Sync current branch via origin
inspire sync --remote upstream  # Sync via upstream remote

# List available resources
inspire resources list

# Create a training job (minimal)
inspire job create \
  --name "my-experiment" \
  --resource "4xH200" \
  --command "bash train.sh"

Defaults: `--framework` (pytorch), `--priority` (8), `--max-time` (100 hours). `--location` and `--image` are optional.

# Check job status
inspire job status <job-id>

# Wait for job completion
inspire job wait <job-id> --timeout 7200

# View logs
inspire job logs <job-id> --tail 100
```

Logs written during job execution (when `INSPIRE_TARGET_DIR` is set) are stored under `INSPIRE_TARGET_DIR/.inspire/` with pattern `training_master_*.log` and fetched via the Gitea Actions workflow.

## Command Reference

### Code Sync

| Command | Description |
|---------|-------------|
| `inspire sync` | Sync local branch to Bridge shared filesystem |
| `inspire sync --force` | Force sync, discarding any local changes on Bridge |

### Job Management

| Command | Description |
|---------|-------------|
| `inspire job create` | Create a new training job (options: `--name`, `--resource`, `--command`, `--framework`, `--priority`, `--max-time`, `--location`, `--image`) |
| `inspire job status <id>` | Check job status |
| `inspire job stop <id>` | Stop a running job |
| `inspire job wait <id>` | Wait for job completion |
| `inspire job list` | List recent jobs (from local cache) |
| `inspire job logs <id>` | View job logs |

### Resource Discovery

| Command | Description |
|---------|-------------|
| `inspire resources list` | List available GPU configurations |
| `inspire nodes list` | List cluster nodes |

### Configuration

| Command | Description |
|---------|-------------|
| `inspire config check` | Validate environment and API authentication |

### Global Options

| Option | Description |
|--------|-------------|
| `--json` | Output as JSON (machine-readable) |
| `--debug` | Enable debug logging |
| `--help` | Show help message |
| `--version` | Show version |

## Examples

### Create a debug training job

```bash
inspire job create \
  --name "pr-123-debug" \
  --resource "4xH200" \
  --command "bash train_debug.sh" \
  --priority 9 \
  --max-time 2
```

### Monitor job with JSON output (for automation)

```bash
inspire --json job status job-abc-123
```

### Stream logs in real-time

```bash
# Poll for latest logs while job is running
watch -n 30 "inspire job logs job-abc-123 --tail 100 --refresh"
```

### Wait for job and get exit code

```bash
inspire job wait job-abc-123 --timeout 14400 --interval 60
echo "Exit code: $?"  # 0 = success, non-zero = failure
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 10 | Configuration error (missing env vars) |
| 11 | Authentication failed |
| 12 | Validation error (invalid input) |
| 13 | API error |
| 14 | Timeout |
| 15 | Log not found |
| 16 | Job not found |

## For Claude Code Integration

The CLI is designed to work well with AI agents like Claude Code:

```bash
# Machine-readable JSON output
inspire --json job create --name "test" --resource "H200" --command "echo hello"

# Parse status programmatically
inspire --json job status job-abc-123 | jq '.data.status'

# Get log content as JSON
inspire --json job logs job-abc-123 --tail 50
```

## Troubleshooting

### "Missing INSPIRE_USERNAME environment variable"

Set your credentials:
```bash
export INSPIRE_USERNAME="your_username"
export INSPIRE_PASSWORD="your_password"
```

### "Missing INSPIRE_TARGET_DIR environment variable"

This is required for **local** log operations (when you have access to the shared filesystem):
```bash
export INSPIRE_TARGET_DIR="/inspire/hdd/global_user/..."
```

For **remote** log retrieval (from a laptop), see [Remote Log Retrieval](#remote-log-retrieval) below.

## Remote Log Retrieval

If you're running `inspire job logs` from a machine **without** access to the shared filesystem (e.g., your laptop), the CLI can fetch logs via Gitea Actions workflows.

### Setup

1. **Ensure the workflow file exists** in your training repo:
   - `.gitea/workflows/retrieve_job_log.yml`

2. **Set environment variables:**
   ```bash
   export INSP_GITEA_REPO="owner/repo"            # Your Gitea repo
   export INSP_GITEA_TOKEN="..."                  # Gitea personal access token
   export INSP_GITEA_SERVER="https://gitea.example.com"
   ```

3. **Ensure your repo has a Gitea Actions runner** with access to the shared filesystem and label `qz-selfhosted`.
   (You already have this if `act_runner` is registered on the Bridge machine.)
   Tips: if you need sudo to setup a self-hosted runner in inspire platform, you can just try
   ```bash
   export RUNNER_ALLOW_RUNASROOT=1
   ```


### How It Works

```
Laptop (inspire job logs)
    ↓
Gitea API (triggers workflow)
    ↓
Self-hosted Runner (reads log from shared filesystem)
    ↓
Artifact (uploads log)
    ↓
Laptop (downloads and caches locally)
```

### Usage

```bash
# Fetch log (first time: ~20-30 seconds, cached after)
inspire job logs <job-id>

# Force refresh while job is running
inspire job logs <job-id> --tail 100 --refresh

# Monitor continuously
watch -n 30 "inspire job logs <job-id> --tail 100 --refresh"
```

Logs are cached locally at `~/.inspire/logs/` and reused on subsequent calls.

## Code Sync

The `inspire sync` command pushes your local branch to Gitea and triggers a workflow on the Bridge runner to sync the code to the shared filesystem.

## Bridge Exec

Run shell commands on the Bridge self-hosted runner (in `INSPIRE_TARGET_DIR`), with optional denylist and artifact download.

### Setup
- Ensure the workflow file exists in your training repo:
  - `.gitea/workflows/run_bridge_action.yml`
- Env vars:
  - `INSP_GITEA_REPO` (owner/repo)
  - `INSP_GITEA_SERVER` (Gitea server URL)
  - `INSP_GITEA_TOKEN` (Gitea token)
  - `INSPIRE_TARGET_DIR` (target dir on Bridge — shared with sync and logs)
  - Optional: `INSPIRE_BRIDGE_ACTION_TIMEOUT` (seconds, default 300)
  - Optional: `INSPIRE_BRIDGE_DENYLIST` (comma/newline glob patterns for blocking commands)

### Usage
```bash
# Run a command (output is displayed in terminal)
inspire bridge exec "uv venv .venv && ./.venv/bin/pip install torch"

# With denylist to block dangerous patterns
inspire bridge exec "pip install numpy" \
  --denylist "rm*" --denylist "*sudo*"

# Download files created by the command
inspire bridge exec "uv venv .venv" \
  --artifact-path .venv --download ./local-venv

# Fire-and-forget (don't wait for completion)
inspire bridge exec "python train.py" --no-wait
```

Notes:
- **Command output is displayed** in your terminal after the command completes
- Denylist is optional (warning if none). Patterns use **glob-style** matching (like `.gitignore`):
  | Pattern | Matches |
  |---------|---------|
  | `rm` | exact command `rm` only |
  | `rm*` | `rm`, `rm -rf /`, `rmdir foo` |
  | `*sudo*` | any command containing `sudo` |
  | `*rm -rf*` | any command containing `rm -rf` |
- All commands run under `bash -lc` with `set -euo pipefail` and `cd $INSPIRE_TARGET_DIR`.
- To download files, specify `--artifact-path` relative to `INSPIRE_TARGET_DIR`, then `--download` local dir.

## Code Sync Setup

1. **Ensure the workflow file exists** in your training repo:
   - `.gitea/workflows/sync_code.yml`

2. **Set local environment variables:**
   ```bash
   export INSP_GITEA_REPO="owner/repo"             # Your Gitea repo
   export INSP_GITEA_SERVER="https://gitea.example.com"
   export INSP_GITEA_TOKEN="..."
   export INSPIRE_TARGET_DIR="/path/to/dir"        # Target directory on Bridge
   export INSPIRE_DEFAULT_REMOTE="origin"          # Optional, defaults to origin
   ```

### Sync Usage

```bash
# Sync current branch to origin, then to Bridge
inspire sync

# Sync to a different remote
inspire sync --remote upstream

# Sync a specific branch
inspire sync --branch feature/new-model

# Force sync (discard local changes on Bridge)
inspire sync --force

# Don't wait for completion
inspire sync --no-wait
```

### Typical Workflow

```bash
# 1. Make changes and commit
git add . && git commit -m "feat: improve model"

# 2. Sync to Bridge
inspire sync
# Output: ✓ Synced branch 'my-branch' (abc1234) to /shared/EBM_dev

# 3. Launch training
inspire job create --name "test-improve" --resource "4xH200" --command "bash train.sh"

# 4. Monitor logs
inspire job logs <job-id> --tail 100
```

### How It Works

```
Laptop (inspire sync)
    ↓
Git push to remote
    ↓
Gitea API (triggers sync_code workflow)
    ↓
Self-hosted Runner:
    - cd to target directory
    - git fetch && checkout branch
    - git pull (or git reset --hard if --force)
    ↓
Returns commit SHA to confirm sync
```

### Handling Sync Errors

If the Bridge has local changes or the branch has diverged, the sync will fail:

```
✗ Sync failed: failure
  See: https://gitea.example.com/owner/repo/actions
```

To resolve, use `--force` to discard local changes on Bridge:

```bash
inspire sync --force
```

**Warning:** `--force` will run `git reset --hard` on the Bridge, discarding any uncommitted changes there.

### "Authentication failed"

- Verify your username and password are correct
- Check if the Inspire platform is accessible
- Try with `--debug` flag for more details
- Run `inspire config check` to validate configuration and authentication

## License

Proprietary - Inspire Platform Members Only

This software is confidential and only authorized for use by Inspire platform members.
Unauthorized distribution or use is prohibited.
