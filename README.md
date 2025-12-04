# Inspire CLI

Command-line interface for the Inspire HPC training platform.

> **Access Restricted**: This tool is only available to Inspire platform members.

## Installation

```bash
pip install git+https://github.com/cyteena/inspire-cli.git
```

## Configuration

Set the required environment variables:

```bash
# Required
export INSPIRE_USERNAME="your_username"
export INSPIRE_PASSWORD="your_password"

# Required for log operations
export INSP_TARGET_DIR="/path/to/shared/filesystem"

# Optional
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

# List available resources
inspire resources list

# Create a training job
inspire job create \
  --name "my-experiment" \
  --resource "4xH200" \
  --command "bash train.sh"

# Check job status
inspire job status <job-id>

# Wait for job completion
inspire job wait <job-id> --timeout 7200

# View logs
inspire job logs <job-id> --tail 100
```

## Command Reference

### Job Management

| Command | Description |
|---------|-------------|
| `inspire job create` | Create a new training job |
| `inspire job status <id>` | Check job status |
| `inspire job stop <id>` | Stop a running job |
| `inspire job wait <id>` | Wait for job completion |
| `inspire job list` | List recent jobs (from local cache) |
| `inspire job logs <id>` | View job logs |

### Resource Discovery

| Command | Description |
|---------|-------------|
| `inspire resources list` | List available GPU configurations |
| `inspire resources check <type>` | Check GPU availability (H200, H100) |
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

### "Missing INSP_TARGET_DIR environment variable"

This is required for **local** log operations (when you have access to the shared filesystem):
```bash
export INSP_TARGET_DIR="/inspire/hdd/global_user/..."
```

For **remote** log retrieval (from a laptop), see [Remote Log Retrieval](#remote-log-retrieval) below.

## Remote Log Retrieval

If you're running `inspire job logs` from a machine **without** access to the shared filesystem (e.g., your laptop), the CLI can fetch logs via GitHub Actions.

### Setup

1. **Copy the workflow file** to your repository:
   ```bash
   # Copy from this repo's workflows/ directory
   cp workflows/retrieve_job_log.yml YOUR_REPO/.github/workflows/
   ```

   Or copy it from: [`workflows/retrieve_job_log.yml`](workflows/retrieve_job_log.yml)

2. **Set environment variables:**
   ```bash
   export INSP_GITHUB_REPO="owner/repo"      # Your GitHub repo
   export INSP_GITHUB_TOKEN="ghp_..."        # GitHub PAT (or use: gh auth login)
   ```

3. **Ensure your repo has a self-hosted runner** with access to the shared filesystem and label `qizhi-self-hosted`.

### How It Works

```
Laptop (inspire job logs)
    ↓
GitHub API (triggers workflow)
    ↓
Self-hosted Runner (reads log from shared filesystem)
    ↓
GitHub Artifact (uploads log)
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

### "Authentication failed"

- Verify your username and password are correct
- Check if the Inspire platform is accessible
- Try with `--debug` flag for more details
- Run `inspire config check` to validate configuration and authentication

## License

Proprietary - Inspire Platform Members Only

This software is confidential and only authorized for use by Inspire platform members.
Unauthorized distribution or use is prohibited.
