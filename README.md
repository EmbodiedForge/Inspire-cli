# Inspire CLI

Command-line interface for the Inspire HPC training platform.

> **Access Restricted**: This tool is only available to Inspire platform members.

> **Note**: This project was 99% vibe-coded with [Claude Code](https://claude.com/claude-code). Initial version by [Huaizz-shawen](https://github.com/Huaizz-shawen).

## Installation

```bash
# Via HTTPS (requires token)
uv tool install git+https://<your-gitea-host>/cyteena/inspire-cli.git

# Via SSH (recommended - no token needed)
uv tool install git+ssh://git@<your-gitea-host>/cyteena/inspire-cli.git
```

> **Tip**: If you use SSH keys for Git, the SSH method is simpler - no need to set up access tokens.

### Local Checkout (No Venv Activation)

If you are working from a git clone and do not want to `source .venv/bin/activate`:

```bash
uv tool install -e .
inspire --help
```

Alternative (repo-local wrapper):

```bash
uv venv .venv
uv pip install -e .
./bin/inspire --help
```

## Configuration

Set the required environment variables:

```bash
# Required for API access
export INSPIRE_USERNAME="your_username"
export INSPIRE_PASSWORD="your_password"

# Required for sync/bridge/log operations (shared filesystem root) 
export INSPIRE_TARGET_DIR="/path/to/shared/filesystem"

# Gitea Actions (required for sync/bridge exec/remote logs, if SSH works, ignore it)
export INSP_GITEA_REPO="owner/repo"
export INSP_GITEA_TOKEN="..."          # Gitea personal access token
export INSP_GITEA_SERVER="https://gitea.example.com"

# Optional
export INSP_IMAGE="your_image:tag"  # Default Docker image
export INSP_PRIORITY="6"            # Default job priority (1-10)
export INSPIRE_BASE_URL="https://qz.sii.edu.cn"  # default
export INSPIRE_LOG_PATTERN="training_master_*.log"  # default
export INSPIRE_JOB_CACHE="~/.inspire/jobs.json"  # default
export INSPIRE_TIMEOUT="30"  # API timeout in seconds
export INSPIRE_MAX_RETRIES="3"  # Max API retries
export INSPIRE_RETRY_DELAY="1.0"  # Retry delay in seconds

# Notebook SSH helpers (for notebook instances without internet access)
export INSPIRE_RTUNNEL_BIN="/inspire/hdd/global_user/.../tools/rtunnel"
export INSPIRE_DROPBEAR_DEB_DIR="/inspire/hdd/global_user/.../tools/debs/dropbear"

# Optional profile shortcuts (env-only)
# Use: inspire --profile 4090 <command>
export INSPIRE_PROFILE_4090_WORKSPACE_ID="ws-..."
export INSPIRE_PROFILE_4090_PROJECT_ID="project-..."
export INSPIRE_PROFILE_4090_NOTEBOOK_RESOURCE="1x4090"
export INSPIRE_PROFILE_4090_IMAGE="pytorch:tag"
export INSPIRE_PROFILE_4090_TARGET_DIR="/inspire/hdd/global_user/..."
export INSPIRE_PROFILE_4090_PRIORITY="6"
export INSPIRE_PROFILE_4090_RTUNNEL_BIN="/inspire/hdd/global_user/.../rtunnel"
export INSPIRE_PROFILE_4090_APT_MIRROR_URL="http://nexus.sii.shaipower.online/repository/ubuntu/"
export INSPIRE_PROFILE_4090_PIP_INDEX_URL="http://nexus.sii.shaipower.online/repository/pypi/simple"
export INSPIRE_PROFILE_4090_PIP_TRUSTED_HOST="nexus.sii.shaipower.online"
```

## Quick Start

```bash
# Check installation
inspire --version
inspire --help

# Check configuration and authentication
inspire config check

# Check GPU availability
inspire resources list

# Quick job submission (recommended)
inspire run "python train.py"                    # 8xH200, auto-select location
inspire run "bash train.sh" --gpus 4 --type H100 # 4xH100
inspire run "python train.py" --watch            # Sync, run, follow logs

# Traditional job creation
inspire job create \
  --name "my-experiment" \
  --resource "4xH200" \
  --command "bash train.sh"

# Check job status
inspire job status <job-id>

# Show the command used by a job
inspire job command <job-id>

# View logs
inspire job logs <job-id> --tail 100 --follow
```

## Command Reference

### Quick Run (Recommended)

The `inspire run` command provides smart resource allocation - automatically selects the compute group with most available GPUs.

```bash
# Basic usage (8xH200, auto-select best location)
inspire run "python train.py"

# Specify GPU count and type
inspire run "python train.py" --gpus 4 --type H100

# Full workflow: sync code, run job, follow logs
inspire run "python train.py" --watch

# With custom options
inspire run "bash train.sh" \
  --gpus 8 \
  --type H200 \
  --name "my-experiment" \
  --priority 8 \
  --max-time 24
```

| Option | Description |
|--------|-------------|
| `-g, --gpus` | Number of GPUs (default: 8) |
| `--type` | GPU type: H100 or H200 (default: H200) |
| `-n, --name` | Job name (auto-generated if not specified) |
| `-s, --sync` | Sync code before running |
| `-w, --watch` | Sync, run, then follow logs until completion |
| `--priority` | Task priority 1-10 (default: 6, env: INSP_PRIORITY) |
| `--location` | Preferred datacenter (overrides auto-selection) |
| `--max-time` | Max runtime in hours (default: 100) |
| `--image` | Custom Docker image |

### Resource Discovery

```bash
# List GPU availability (browser API, accurate)
inspire resources list

# Watch availability continuously
inspire resources list --watch

# Node-level availability (workspace-scoped)
inspire resources list --workspace

# Include all compute groups
inspire resources list --all
```

| Option | Description |
|--------|-------------|
| `--workspace, -ws` | Show per-node availability (workspace-scoped) |
| `--watch, -w` | Continuously watch availability (refreshes every 30s) |
| `--interval, -i` | Watch refresh interval in seconds (default: 30) |
| `--all` | Show all accessible compute groups |
| `--no-cache` | Bypass cached node availability (workspace view only) |

### Code Sync

`inspire sync` automatically uses SSH tunnel when available (fast), otherwise falls back to Gitea Actions.

```bash
inspire sync                    # Sync current branch via origin
inspire sync --remote upstream  # Sync via upstream remote
inspire sync --force            # Force sync, discard local changes on Bridge
```

**With SSH tunnel running**: Sync completes in ~2-3 seconds
**Without SSH tunnel**: Falls back to Gitea Actions (~30 seconds)

### Job Management

| Command | Description |
|---------|-------------|
| `inspire job create` | Create a training job |
| `inspire job status <id>` | Check job status |
| `inspire job command <id>` | Show job start command |
| `inspire job stop <id>` | Stop a running job |
| `inspire job wait <id>` | Wait for job completion |
| `inspire job list` | List recent jobs |
| `inspire job list --watch` | Watch job status with live updates |
| `inspire job logs <id>` | View job logs |
| `inspire job logs <id> --follow` | Stream logs in real-time |

### Notebook Management

| Command | Description |
|---------|-------------|
| `inspire notebook list` | List all notebook instances |
| `inspire notebook status <id>` | Get detailed notebook status |
| `inspire notebook create` | Create a new notebook instance |
| `inspire notebook stop <id>` | Stop a running notebook |
| `inspire notebook ssh <id>` | SSH into notebook *(experimental)* |

### SSH Tunnel (Fast Bridge Access)

The SSH tunnel provides ~100x faster command execution compared to Gitea Actions.

```bash
# Set up tunnel URL (from Bridge notebook's Ports tab, port 31337)
inspire tunnel set-url "https://nat-notebook-inspire.../proxy/31337/"

# Start tunnel
inspire tunnel start

# Check status
inspire tunnel status

# Stop tunnel
inspire tunnel stop
```

Once the tunnel is running, `bridge exec`, `job logs`, and `sync` automatically use it for faster execution.

### Notebook SSH (Experimental)

SSH into notebook instances via browser automation. Sets up rtunnel automatically:

```bash
inspire notebook ssh <notebook-id> --save-as my-notebook
```

**Options:** `--save-as`, `--command`, `--rtunnel-bin`, `--timeout`, `--debug-playwright`

#### Proxy Configuration (WSL Users)

If using WSL with a proxy (e.g., aTrust + Karing), ensure your `http_proxy` and `HTTP_PROXY` environment variables match. The tunnel will fail fast with a clear error if they mismatch:

```bash
# If you see "Proxy env mismatch" error:
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
```

#### Reliability Features

- **10-second SSH timeout**: Handles slow network conditions
- **Automatic retry**: Retries once on transient SSH failures (helpful for parallel commands)
- **Graceful fallback**: Falls back to Gitea Actions if tunnel is unavailable

### Bridge Exec

Run shell commands on the Bridge self-hosted runner:

```bash
# Run a command
inspire bridge exec "pip install torch"

# With artifact download
inspire bridge exec "python generate.py" \
  --artifact-path outputs --download ./local-outputs

# Fire-and-forget
inspire bridge exec "python train.py" --no-wait
```

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

### Quick Training Workflow

```bash
# 1. Check available GPUs
inspire resources list

# 2. Run training with auto-allocation
inspire run "python train.py" --watch

# Or step by step:
inspire sync
inspire run "python train.py"
inspire job logs <job-id> --follow
```

### Monitor Job with Watch Mode

```bash
# Watch job list with live updates
inspire job list --watch

# Watch resource availability
inspire resources list --watch
```

### Create Job with Full Options

```bash
inspire job create \
  --name "pr-123-debug" \
  --resource "4xH200" \
  --command "bash train_debug.sh" \
  --priority 9 \
  --max-time 2 \
  --location "H200 机房3"
```

By default, `inspire job create` auto-selects a compute group using node-level
browser API data (same source as `inspire resources list --workspace` and
`inspire resources nodes`). Use `--no-auto` to skip auto-selection.

### JSON Output for Automation

```bash
inspire --json job status job-abc-123
inspire --json job list
inspire --json resources list
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

## Remote Log Retrieval

If running `inspire job logs` from a machine without access to the shared filesystem, the CLI can fetch logs via Gitea Actions workflows.

### Setup

1. **Ensure the workflow file exists** in your training repo:
   - `.gitea/workflows/retrieve_job_log.yml`

2. **Set environment variables:**
   ```bash
   export INSP_GITEA_REPO="owner/repo"
   export INSP_GITEA_TOKEN="..."
   export INSP_GITEA_SERVER="https://gitea.example.com"
   ```

3. **Ensure your repo has a Gitea Actions runner** with access to the shared filesystem.

### How It Works

```
Laptop (inspire job logs)
    ↓
Gitea API (triggers workflow)
    ↓
Self-hosted Runner (reads log)
    ↓
Artifact (uploads log)
    ↓
Laptop (downloads and caches)
```

## Code Sync Setup

1. **Ensure the workflow file exists:**
   - `.gitea/workflows/sync_code.yml`

2. **Set environment variables:**
   ```bash
   export INSP_GITEA_REPO="owner/repo"
   export INSP_GITEA_SERVER="https://gitea.example.com"
   export INSP_GITEA_TOKEN="..."
   export INSPIRE_TARGET_DIR="/path/to/dir"
   ```

### Typical Workflow

```bash
# 1. Make changes and commit
git add . && git commit -m "feat: improve model"

# 2. Sync and run
inspire run "bash train.sh" --watch

# Or manually:
inspire sync
inspire job create --name "test" --resource "4xH200" --command "bash train.sh"
inspire job logs <job-id> --follow
```

## Troubleshooting

### "Missing INSPIRE_USERNAME environment variable"

Set your credentials:
```bash
export INSPIRE_USERNAME="your_username"
export INSPIRE_PASSWORD="your_password"
```

### "Authentication failed"

- Verify your username and password are correct
- Check if the Inspire platform is accessible
- Run `inspire config check` to validate configuration

### "Missing INSPIRE_TARGET_DIR"

Required for local log access:
```bash
export INSPIRE_TARGET_DIR="/inspire/hdd/global_user/..."
```

For remote access, configure Gitea Actions (see Remote Log Retrieval).

## License

Proprietary - Inspire Platform Members Only

This software is confidential and only authorized for use by Inspire platform members.
Unauthorized distribution or use is prohibited.
